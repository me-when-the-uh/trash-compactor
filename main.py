from __future__ import annotations

import argparse
import contextlib
import io
import logging
import multiprocessing
import os
import sys
from datetime import datetime
from textwrap import dedent
from typing import Optional, Sequence

from colorama import Fore, Style, init

from src import config
from src.console import EscapeExit, allocate_console, attach_to_parent_console, display_banner, prompt_exit, read_user_input
from src.launch import acquire_directory, interactive_configure, confirm_hdd_usage, configure_lzx
from src.file_utils import describe_protected_path, is_admin, validate_target_path
from src.skip_logic import discard_staged_incompressible_cache, log_directory_skips
from src.i18n import _, load_translations
from src.version import BUILD_DATE, VERSION
from pathlib import Path


def setup_logging(verbosity: int) -> None:
    debug_enabled = verbosity >= 3

    class _Formatter(logging.Formatter):
        def __init__(self, debug: bool) -> None:
            super().__init__()
            self._debug = debug

        def format(self, record: logging.LogRecord) -> str:
            if record.levelno == logging.DEBUG:
                if self._debug:
                    return f"DEBUG: {record.getMessage()}"
                return ""
            if record.levelno == logging.INFO:
                return record.getMessage()
            if record.levelno >= logging.WARNING:
                return f"{record.levelname}: {record.getMessage()}"
            return ""

    handler = logging.StreamHandler()
    handler.setFormatter(_Formatter(debug_enabled))

    root_logger = logging.getLogger()
    root_logger.handlers = []
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG if debug_enabled else logging.INFO)


def _detect_language_override(argv: Sequence[str]) -> Optional[str]:
    for i, arg in enumerate(argv):
        if arg in ("--language", "-l") and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--language="):
            return arg.split("=", 1)[1]
    return None


def build_parser() -> argparse.ArgumentParser:
    description = dedent(
        _("""
        Trash-Compactor applies Windows NTFS compression with guardrails that avoid
        low-yield cache folders. Run without arguments to launch the interactive 
        window, or supply flags if you want to automate your run.
        """)
    ).strip()

    epilog = dedent(
        """
        Examples:
          trash-compactor.exe                         Launch interactive configuration
          trash-compactor.exe C:\\Games               Compress immediately using defaults

        Verbosity levels:
          -v    Summarise cache exclusions and entropy sampling
          -vv   Include per-stage progress updates
          -vvv  Enable full debug logging (developer focus)
        """
    ).rstrip()

    parser = argparse.ArgumentParser(
        prog="trash-compactor",
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawTextHelpFormatter,
    )

    parser.add_argument(
        "directory",
        nargs="?",
        help=_("Target directory to compress. Omit to start the interactive walkthrough."),
    )

    parser.add_argument(
        "--one-click",
        action="store_true",
        help=argparse.SUPPRESS,
        dest="one_click",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help=_("Increase logging verbosity"),
    )
    parser.add_argument(
        "-x",
        "--no-lzx",
        action="store_true",
        help=_("Disable LZX compression for better performance on low-end CPUs"),
    )
    parser.add_argument(
        "-f",
        "--force-lzx",
        action="store_true",
        help=_("Force LZX compression even if the CPU is deemed less capable for peak compression"),
    )
    parser.add_argument(
        "-s",
        "--single-worker",
        action="store_true",
        help=_("Throttle compression to a single worker to reduce disk fragmentation"),
    )
    parser.add_argument(
        "-d",
        "--dry-run",
        action="store_true",
        help=_("Analyse directory entropy without compressing files"),
    )
    parser.add_argument(
        "-y",
        "--yes",
        "--no-prompt",
        action="store_true",
        dest="yes",
        help=_("Proceed with compression after dry-run analysis without prompting"),
    )
    parser.add_argument(
        "-m",
        "--min-savings",
        type=float,
        default=None,
        help=_("Skip directories when estimated savings fall below this percentage (0-90, default {default:.0f})").format(default=config.DEFAULT_MIN_SAVINGS_PERCENT),
    )
    parser.add_argument(
        "-l",
        "--language",
        help=_("Force a specific language (e.g., 'en', 'ru')"),
    )
    parser.add_argument(
        "--debug-scan-all",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    return parser


def announce_mode(args: argparse.Namespace) -> None:
    notices: list[str] = []
    if getattr(args, "dry_run", False):
        notices.append(_("Dry run: analysing entropy without compressing files."))
    if getattr(args, "single_worker", False):
        notices.append(_("Single-worker mode: queue batches sequentially to minimise disk head contention."))

    if not notices:
        return

    print()
    for line in notices:
        print(Fore.YELLOW + line + Style.RESET_ALL)


def run_compression(directory: str, verbosity: int, min_savings: float, debug_scan_all: bool = False) -> None:
    from src.compression_module import compress_directory
    from src.stats import print_compression_summary

    logging.info(_("Starting compression of directory: %s"), directory)
    stats, monitor = compress_directory(
        directory,
        verbosity=verbosity,
        min_savings_percent=min_savings,
        debug_scan_all=debug_scan_all,
    )
    print_compression_summary(stats)
    monitor.print_summary()
    from src.launch import print_defrag_hint

    print_defrag_hint(stats.compressed_files)


def run_entropy_dry_run(directory: str, verbosity: int, min_savings: float, debug_scan_all: bool = False) -> tuple[CompressionStats, PerformanceMonitor, list[tuple[str, int, str]]]:
    from src.compression_module import entropy_dry_run
    from src.stats import CompressionStats, print_entropy_dry_run
    from src.timer import PerformanceMonitor

    logging.info(_("Starting entropy dry run for directory: %s"), directory)
    stats, monitor, plan = entropy_dry_run(
        directory,
        verbosity=verbosity,
        min_savings_percent=min_savings,
        debug_scan_all=debug_scan_all,
    )
    print_entropy_dry_run(stats, min_savings, verbosity)
    log_directory_skips(stats, verbosity, min_savings)
    monitor.stats.print_dry_run_metrics(min_percent=0.5)
    return stats, monitor, plan


def _prepare_arguments(argv: Sequence[str]) -> tuple[argparse.Namespace, bool]:
    args = build_parser().parse_args(argv)
    args.min_savings = (
        config.DEFAULT_MIN_SAVINGS_PERCENT
        if args.min_savings is None
        else config.clamp_savings_percent(args.min_savings)
    )

    interactive_launch = not args.directory

    from src import benchmark
    benchmark_ok: Optional[bool] = None
    if not interactive_launch and not args.no_lzx:
        benchmark_ok = benchmark.run_benchmark()
        if not benchmark_ok and not args.force_lzx:
            args.no_lzx = True
            setattr(args, 'lzx_disabled_reason', 'benchmark')
            print(Fore.YELLOW + _("\nNotice: LZX compression has been disabled to prevent slowdowns for compressed apps.\n(Your CPU is too slow)") + Style.RESET_ALL)
    setattr(args, 'benchmark_ok', benchmark_ok)

    return args, interactive_launch


def _apply_lzx_choice(args: argparse.Namespace) -> None:
    configure_lzx(
        choice_enabled=not args.no_lzx,
        force_lzx=args.force_lzx,
        benchmark_ok=getattr(args, 'benchmark_ok', None),
        disabled_reason=getattr(args, 'lzx_disabled_reason', None),
    )


def _validate_modes(args: argparse.Namespace) -> bool:
    if args.no_lzx and args.force_lzx:
        print(Fore.RED + _("Error: Cannot disable and force LZX compression at the same time.") + Style.RESET_ALL)
        return False
    return True


def _emit_verbosity_banner(level: int) -> None:
    if not level:
        return
    verbose_labels = {
        1: _("Verbosity level 1: entropy decisions and summary stats"),
        2: _("Verbosity level 2: include stage-level progress and verification warnings"),
        3: _("Verbosity level 3: full debug logging enabled"),
    }
    label = verbose_labels.get(level, _("Verbosity level 3: full debug logging enabled"))
    print(Fore.BLUE + label + Style.RESET_ALL)


def _configure_runtime(args: argparse.Namespace, interactive_launch: bool) -> Optional[str]:
    from src.workers import set_worker_cap

    set_worker_cap(1 if getattr(args, "single_worker", False) else None)

    if is_admin():
        logging.info(_("Running with administrator privileges."))
    else:
        logging.warning(
            _("Running without administrator privileges. Some protected files may be skipped.")
        )

    announce_mode(args)

    _apply_lzx_choice(args)

    directory, updated_args = acquire_directory(args, interactive_launch)
    args.directory = directory
    for key, value in vars(updated_args).items():
        setattr(args, key, value)

    protection_reason = describe_protected_path(directory) or validate_target_path(directory)
    if protection_reason:
        logging.error(_("Cannot compress target: %s"), protection_reason)
        if 'Windows' in protection_reason:
            logging.error(_("To compress Windows system files, use 'compact.exe /compactos:always' instead"))
        return None

    if not confirm_hdd_usage(directory, force_serial=args.single_worker):
        return None

    return directory


def main() -> int:
    override_lang = _detect_language_override(sys.argv[1:])
    load_translations(override_lang)

    # Console handling for the GUI-subsystem exe:
    # - CLI mode (any argv): attach to the invoking terminal (cmd/PowerShell/
    #   Windows Terminal) so output appears there and no second window spawns.
    #   If there is no parent console (double-click), allocate one.
    # - GUI mode (no argv): no console at all - Windows never allocated one.
    is_cli_mode = len(sys.argv) > 1
    if is_cli_mode:
        if not attach_to_parent_console():
            allocate_console()

    args, interactive_launch = _prepare_arguments(sys.argv[1:])

    if os.getenv("TRASH_COMPACTOR_DIAGNOSTIC", "").strip().lower() in {"1", "true", "yes"}:
        from src.compression.file_scan import fast_walk_available

        probe_available = False
        wheel_version = ""
        if fast_walk_available():
            try:
                import fast_walk

                wheel_version = getattr(fast_walk, "__version__", "")
                probe_available = callable(getattr(fast_walk, "probe_directories_parallel", None))
            except Exception:
                probe_available = False
        print(
            f"[diag] frozen={getattr(sys, 'frozen', False)} "
            f"meipass={getattr(sys, '_MEIPASS', '')!r} "
            f"fast_walk={fast_walk_available()} "
            f"fast_walk_version={wheel_version or 'unknown'} "
            f"probe_directories_parallel={probe_available}",
            flush=True,
        )

    init(autoreset=True)
    display_banner(VERSION, BUILD_DATE)

    if not _validate_modes(args):
        prompt_exit()
        sys.exit(1)

    setup_logging(args.verbose)

    _emit_verbosity_banner(args.verbose)

    if interactive_launch:
        try:
            from src.benchmark import run_benchmark

            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                benchmark_ok = run_benchmark()

            from src.gui.backend import run_gui
            run_gui(benchmark_ok=benchmark_ok)
            os._exit(0)
        except (ImportError, ModuleNotFoundError) as exc:
            if getattr(sys, "frozen", False):
                print(
                    Fore.RED
                    + _(
                        "GUI failed to start: pywebview is not bundled in this executable. "
                        "Rebuild after running: python -m pip install -r requirements.txt"
                    )
                    + Style.RESET_ALL,
                    file=sys.stderr,
                )
                print(f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
                prompt_exit()
                sys.exit(1)
            args = interactive_configure(args)
            args.min_savings = config.clamp_savings_percent(args.min_savings)
            if not getattr(args, "one_click", False) and not args.directory:
                prompt_exit()
                return 1

    if getattr(args, 'one_click', False) and not args.directory:
        _apply_lzx_choice(args)

        from src.one_click import run_one_click_mode

        run_one_click_mode(
            verbosity=args.verbose,
            min_savings=args.min_savings,
            allow_compactos=is_admin(),
        )
        print(_("\nOperation completed."))
        prompt_exit()
        return 0

    directory = _configure_runtime(args, interactive_launch)
    if directory is None:
        prompt_exit()
        return 1

    try:
        if getattr(args, "dry_run", False):
            stats, monitor, plan = run_entropy_dry_run(
                directory,
                verbosity=args.verbose,
                min_savings=args.min_savings,
                debug_scan_all=getattr(args, "debug_scan_all", False),
            )

            proceed = getattr(args, "yes", False)
            if plan and not proceed:
                print()
                try:
                    response = read_user_input(_("Do you want to proceed with compression? [y/N]: ")).strip().lower()
                except EscapeExit:
                    discard_staged_incompressible_cache()
                    print(Fore.CYAN + _("\nOperation cancelled by user.") + Style.RESET_ALL)
                    return 130
                except KeyboardInterrupt:
                    discard_staged_incompressible_cache()
                    print(Fore.CYAN + _("\nOperation cancelled by user.") + Style.RESET_ALL)
                    return 130
                proceed = response in ('y', 'yes')

            if plan and proceed:
                print(_("\nStarting compression..."))
                monitor.start_operation()
                from src.compression_module import execute_compression_plan_wrapper
                from src.stats import print_compression_summary

                stats, monitor = execute_compression_plan_wrapper(
                    stats,
                    monitor,
                    plan,
                    verbosity_level=args.verbose,
                    interactive_output=True,
                    min_savings_percent=args.min_savings
                )
                print_compression_summary(stats)
                monitor.print_summary()
                from src.launch import print_defrag_hint

                print_defrag_hint(stats.compressed_files)
            else:
                discard_staged_incompressible_cache()
                if plan:
                    print(_("Compression cancelled."))
        else:
            run_compression(
                directory,
                verbosity=args.verbose,
                min_savings=args.min_savings,
                debug_scan_all=getattr(args, "debug_scan_all", False),
            )
    except KeyboardInterrupt:
        discard_staged_incompressible_cache()
        print(Fore.CYAN + _("\nOperation cancelled by user.") + Style.RESET_ALL)
        return 130
    except Exception:
        discard_staged_incompressible_cache()
        raise

    print(_("\nOperation completed."))
    prompt_exit()
    return 0


if __name__ == "__main__":
    from multiprocessing.spawn import freeze_support as spawn_freeze_support

    spawn_freeze_support()
    multiprocessing.freeze_support()
    sys.exit(main())
