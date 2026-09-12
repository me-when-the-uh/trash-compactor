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
from typing import Optional, Sequence, TYPE_CHECKING

from colorama import Fore, Style, init

from src import config
from src.cli_log import CliLog, _NullCliLog, get_cli_log, set_cli_log
from src.console import EscapeExit, allocate_console, attach_to_parent_console, cprint, display_banner, prompt_exit, read_user_input
from src.launch import acquire_directory, interactive_configure, confirm_hdd_usage, configure_lzx
from src.file_utils import get_protection_reason, is_admin, validate_target_path
from src.skip_logic import discard_staged_incompressible_cache, log_directory_skips
from src.i18n import _, load_translations
from src.version import BUILD_DATE, VERSION
from pathlib import Path

if TYPE_CHECKING:
    from src.stats import CompressionStats
    from src.timer import PerformanceMonitor


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
        "--exclude",
        action="append",
        default=None,
        metavar="PATH",
        help=_("Directory to skip (repeatable). Also TRASH_COMPACTOR_EXCLUDE."),
    )
    parser.add_argument(
        "--log-file",
        nargs="?",
        const=None,
        default=False,
        metavar="PATH",
        help=_(
            "Write a structured run log to PATH (or to ./trash-compactor.log if PATH is omitted). "
            "Truncated at the start of each run; UTF-8."
        ),
    )
    parser.add_argument(
        "--compactos-always",
        action="store_true",
        help=_(
            "In 1-click mode, compress Windows system binaries with "
            "'compact.exe /compactos:always'. Requires Administrator privileges. "
            "No effect on a non-interactive console when executed from Task Scheduler, "
            "or .bat or .ps1 scripts."
        ),
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
        cprint(Fore.YELLOW, line)


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
    from src.stats import CompressionStats, print_dry_run_summary
    from src.timer import PerformanceMonitor

    logging.info(_("Starting entropy dry run for directory: %s"), directory)
    stats, monitor, plan = entropy_dry_run(
        directory,
        verbosity=verbosity,
        min_savings_percent=min_savings,
        debug_scan_all=debug_scan_all,
    )
    print_dry_run_summary(
        min_savings_percent=min_savings,
        projected_original_bytes=stats.entropy_projected_original_bytes,
        projected_compressed_lzx_bytes=stats.entropy_projected_size,
        projected_compressed_xpress_bytes=stats.entropy_projected_size_conservative,
    )
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
            cprint(Fore.YELLOW, _("\nNotice: LZX compression has been disabled to prevent slow startup times for compressed applications."))
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
        cprint(Fore.RED, _("Error: Cannot disable and force LZX compression at the same time."))
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
    cprint(Fore.BLUE, label)


def _apply_session_excludes(args: argparse.Namespace) -> None:
    from src.exclusions import set_session_excludes

    set_session_excludes(getattr(args, "exclude", None) or [])


def _configure_runtime(args: argparse.Namespace, interactive_launch: bool) -> Optional[str]:
    from src.workers import set_worker_cap

    set_worker_cap(1 if getattr(args, "single_worker", False) else None)
    _apply_session_excludes(args)

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

    protection_reason = get_protection_reason(directory) or validate_target_path(directory)
    if protection_reason:
        logging.error(_("Cannot compress target: %s"), protection_reason)
        if 'Windows' in protection_reason:
            logging.error(_("To compress Windows system files, use 'compact.exe /compactos:always' instead"))
        return None

    if not confirm_hdd_usage(directory, force_serial=args.single_worker, yes=getattr(args, "yes", False)):
        return None

    return directory


def main() -> int:
    override_lang = _detect_language_override(sys.argv[1:])
    load_translations(override_lang)

    # Force UTF-8 before any translated string is printed.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    # CLI mode (argv present) attaches to the invoking terminal. GUI mode gets
    # no console at all. --log-file skips allocate_console() since stdout is redirected.
    is_cli_mode = len(sys.argv) > 1
    log_file_requested = "--log-file" in sys.argv
    if is_cli_mode and not log_file_requested:
        if not attach_to_parent_console():
            allocate_console()

    args, interactive_launch = _prepare_arguments(sys.argv[1:])
    _apply_session_excludes(args)

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
        if getattr(args, "log_file", False) is not False:
            logging.info("--log-file is ignored in GUI mode")
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
            _apply_session_excludes(args)
            if not getattr(args, "one_click", False) and not args.directory:
                prompt_exit()
                return 1

    # Build the CliLog for the CLI run.
    log_path: Optional[Path] = None
    if getattr(args, "log_file", False) is not False:
        raw = args.log_file
        if raw is None:
            log_path = Path.cwd() / "trash-compactor.log"
        else:
            log_path = Path(raw)
            if not log_path.is_absolute():
                log_path = Path.cwd() / log_path
        target_dir_str = args.directory
        if target_dir_str:
            try:
                target_resolved = Path(target_dir_str).resolve()
                log_resolved = log_path.resolve()
                target_prefix = str(target_resolved)
                if (
                    str(log_resolved) == target_prefix
                    or str(log_resolved).startswith(target_prefix + os.sep)
                ):
                    logging.error(
                        _("--log-file cannot live inside the target directory: %s"),
                        log_path,
                    )
                    prompt_exit()
                    return 2
            except OSError:
                pass

    cli_log = CliLog.enable(log_path)
    set_cli_log(cli_log)
    if isinstance(cli_log, CliLog):
        cli_log.header(VERSION)
        logging.info(
            _("Running in CLI log mode. Output is also being written to %s"), log_path
        )

    exit_code = 0
    try:
        if getattr(args, 'one_click', False) and not args.directory:
            _apply_lzx_choice(args)

            from src.one_click import run_one_click_mode

            one_click_targets = run_one_click_mode(
                verbosity=args.verbose,
                min_savings=args.min_savings,
                compactos_requested=is_admin() and getattr(args, "compactos_always", False),
                yes=getattr(args, "yes", False),
            )

            if isinstance(cli_log, CliLog) and one_click_targets:
                _emit_cli_log_mode_and_settings(
                    cli_log,
                    mode_name="one-click",
                    target=one_click_targets,
                    args=args,
                )

            print(_("\nOperation completed."))
            return 0

        directory = _configure_runtime(args, interactive_launch)
        if directory is None:
            return 1

        mode_name = "dry-run" if getattr(args, "dry_run", False) else "compress"
        _emit_cli_log_mode_and_settings(
            cli_log,
            mode_name=mode_name,
            target=directory,
            args=args,
        )

        if getattr(args, "dry_run", False):
            exit_code = _run_cli_dry_run(
                args=args, directory=directory, cli_log=cli_log
            )
        else:
            exit_code = _run_cli_compress(
                args=args, directory=directory, cli_log=cli_log
            )
    except KeyboardInterrupt:
        discard_staged_incompressible_cache()
        cprint(Fore.CYAN, _("\nOperation cancelled by user."))
        exit_code = 130
    except Exception:
        discard_staged_incompressible_cache()
        raise
    finally:
        cli_log.finish(exit_code)
        set_cli_log(_NullCliLog())

    print(_("\nOperation completed."))
    prompt_exit()
    return exit_code


def _emit_cli_log_mode_and_settings(cli_log, mode_name, target, args) -> None:
    if not isinstance(cli_log, CliLog):
        return
    lzx_status = (
        "disabled (--no-lzx)" if args.no_lzx
        else "disabled (benchmark)" if getattr(args, "lzx_disabled_reason", None) == "benchmark"
        else "enabled"
    )
    hdd_status = "forced (-s)" if getattr(args, "single_worker", False) else "auto"
    cli_log.mode(mode_name, target)
    cli_log.settings(args.min_savings, lzx_status, hdd_status)


def _run_cli_dry_run(args, directory, cli_log) -> int:
    from src.config import COMPRESSION_ALGORITHMS
    from src.stats import log_by_algorithm

    stats, monitor, plan = run_entropy_dry_run(
        directory,
        verbosity=args.verbose,
        min_savings=args.min_savings,
        debug_scan_all=getattr(args, "debug_scan_all", False),
    )

    active_large = COMPRESSION_ALGORITHMS.get("large", "LZX")
    lzx_disabled = active_large != "LZX"
    lzx_reason = (
        "benchmark" if getattr(args, "lzx_disabled_reason", None) == "benchmark"
        else "--no-lzx" if args.no_lzx
        else None
    )

    if isinstance(cli_log, CliLog):
        cli_log.dry_run_summary(stats, args.min_savings, active_large)
        cli_log.skipped_directories(stats, args.verbose)
        cli_log.timing(monitor)
        cli_log.by_algorithm(stats, lzx_disabled, lzx_reason)

    log_by_algorithm(stats, lzx_disabled, lzx_reason)

    proceed = getattr(args, "yes", False)
    if plan and not proceed:
        print()
        try:
            response = read_user_input(_("Do you want to proceed with compression? [y/N]: ")).strip().lower()
        except EscapeExit:
            discard_staged_incompressible_cache()
            cprint(Fore.CYAN, _("\nOperation cancelled by user."))
            return 130
        except KeyboardInterrupt:
            discard_staged_incompressible_cache()
            cprint(Fore.CYAN, _("\nOperation cancelled by user."))
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

        if isinstance(cli_log, CliLog):
            cli_log.compression_summary(stats)
            cli_log.timing(monitor)
            cli_log.by_algorithm(stats, lzx_disabled, lzx_reason)
            cli_log.errors(stats)
    else:
        discard_staged_incompressible_cache()
        if plan:
            print(_("Compression cancelled."))
        if isinstance(cli_log, CliLog):
            cli_log.errors(stats)

    return 0


def _run_cli_compress(args, directory, cli_log) -> int:
    from src.config import COMPRESSION_ALGORITHMS
    from src.stats import log_by_algorithm

    active_large = COMPRESSION_ALGORITHMS.get("large", "LZX")
    lzx_disabled = active_large != "LZX"
    lzx_reason = (
        "benchmark" if getattr(args, "lzx_disabled_reason", None) == "benchmark"
        else "--no-lzx" if args.no_lzx
        else None
    )

    stats, monitor = _compress_with_log(args, directory)

    if isinstance(cli_log, CliLog):
        cli_log.compression_summary(stats)
        cli_log.skipped_directories(stats, args.verbose)
        cli_log.timing(monitor)
        cli_log.by_algorithm(stats, lzx_disabled, lzx_reason)
        cli_log.errors(stats)

    log_by_algorithm(stats, lzx_disabled, lzx_reason)
    return 0


def _compress_with_log(args, directory):
    """Run compression and return the (stats, monitor) pair.

    ``run_compression`` doesn't return them. The body runs inline here
    rather than refactoring it to return.
    """
    from src.compression_module import compress_directory
    from src.stats import print_compression_summary
    from src.launch import print_defrag_hint

    logging.info(_("Starting compression of directory: %s"), directory)
    stats, monitor = compress_directory(
        directory,
        verbosity=args.verbose,
        min_savings_percent=args.min_savings,
        debug_scan_all=getattr(args, "debug_scan_all", False),
    )
    print_compression_summary(stats)
    monitor.print_summary()
    print_defrag_hint(stats.compressed_files)
    return stats, monitor


if __name__ == "__main__":
    from multiprocessing.spawn import freeze_support as spawn_freeze_support

    spawn_freeze_support()
    multiprocessing.freeze_support()
    sys.exit(main())
