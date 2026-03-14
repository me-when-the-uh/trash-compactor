import argparse
import logging
import sys
from datetime import datetime
from textwrap import dedent
from typing import Optional, Sequence

from colorama import Fore, Style, init

from src import (
    compress_directory,
    compress_directory_legacy,
    config,
    entropy_dry_run,
    execute_compression_plan_wrapper,
    get_cpu_info,
    print_compression_summary,
    print_entropy_dry_run,
    set_worker_cap,
)
from src.console import display_banner, prompt_exit, read_user_input
from src.launch import acquire_directory, interactive_configure, confirm_hdd_usage, configure_lzx
from src.file_utils import describe_protected_path, is_admin
from src.skip_logic import log_directory_skips
from src.i18n import _, load_translations
from src.stats import CompressionStats
from src.timer import PerformanceMonitor
from src.one_click import run_one_click_mode
from pathlib import Path

VERSION = "0.5.1"
BUILD_DATE = "who cares"


def setup_logging(verbosity: int) -> None:
    debug_enabled = verbosity >= 4

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
          trash-compactor.exe -t -v C:\\Projects      Thorough mode with verbose reporting
          trash-compactor.exe -b C:\\Archive          Branding pass after initial compression

        Verbosity levels:
          -v    Summarise cache exclusions and entropy sampling
          -vv   Include per-stage progress updates
          -vvv  Add additional diagnostics for skipped files
          -vvvv Enable full debug logging (developer focus)
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

    # Not part of the advertised CLI surface yet; used by the interactive launcher.
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

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "-b",
        "--brand-files",
        action="store_true",
        help=_("Branding mode. Runs compact.exe for Windows to mark files as compressed, if some files are reported as not"),
    )
    mode_group.add_argument(
        "-t",
        "--thorough",
        action="store_true",
        help=_("Thorough mode. Performs slower but has more accurate compression state checks for scheduled runs"),
    )
    return parser


def announce_mode(args: argparse.Namespace) -> None:
    notices: list[str] = []
    if getattr(args, "dry_run", False):
        notices.append(_("Dry run: analyse entropy without compressing files."))
    if args.brand_files:
        notices.append(_("Branding mode: ensure Windows records compressed attributes after the initial pass."))
    elif args.thorough:
        notices.append(_("Thorough mode: perform deeper compression status checks suited for scheduled runs."))
    if getattr(args, "single_worker", False):
        notices.append(_("Single-worker mode: queue batches sequentially to minimise disk head contention."))

    if not notices:
        return

    print()
    for line in notices:
        print(Fore.YELLOW + line + Style.RESET_ALL)


def run_branding(directory: str, thorough: bool) -> None:
    print(Fore.CYAN + _("\nStarting file branding process...") + Style.RESET_ALL)
    legacy_stats = compress_directory_legacy(directory, thorough_check=thorough)

    print(Fore.CYAN + _("\nFile Branding Summary:") + Style.RESET_ALL)
    print(_("Total files processed: {total}").format(total=legacy_stats.total_files))
    print(_("Files branded as compressed: {branded}").format(branded=legacy_stats.branded_files))
    if legacy_stats.branded_files:
        percentage = (legacy_stats.branded_files / legacy_stats.total_files) * 100 if legacy_stats.total_files else 0
        print(_("Percentage of files branded: {percentage:.2f}%").format(percentage=percentage))
    if legacy_stats.still_unmarked:
        print(Fore.YELLOW + _("Warning: {count} files are still not properly marked as compressed.").format(count=legacy_stats.still_unmarked))
        print(_("These files may be repeatedly processed in future runs."))


def run_compression(directory: str, verbosity: int, thorough: bool, min_savings: float, debug_scan_all: bool = False) -> None:
    logging.info(_("Starting compression of directory: %s"), directory)
    stats, monitor = compress_directory(
        directory,
        verbosity=verbosity,
        thorough_check=thorough,
        min_savings_percent=min_savings,
        debug_scan_all=debug_scan_all,
    )
    print_compression_summary(stats)
    monitor.print_summary()


def run_entropy_dry_run(directory: str, verbosity: int, min_savings: float, debug_scan_all: bool = False) -> tuple[CompressionStats, PerformanceMonitor, list[tuple[Path, int, str]]]:
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
    if not hasattr(args, 'one_click'):
        setattr(args, 'one_click', False)
    if args.min_savings is None:
        args.min_savings = config.DEFAULT_MIN_SAVINGS_PERCENT
    else:
        args.min_savings = config.clamp_savings_percent(args.min_savings)
    
    interactive_launch = not args.directory
    if interactive_launch:
        args = interactive_configure(args)
        args.min_savings = config.clamp_savings_percent(args.min_savings)
    return args, interactive_launch


def _validate_modes(args: argparse.Namespace) -> bool:
    if args.no_lzx and args.force_lzx:
        print(Fore.RED + _("Error: Cannot disable and force LZX compression at the same time.") + Style.RESET_ALL)
        return False
    if getattr(args, "dry_run", False) and args.brand_files:
        print(Fore.RED + _("Error: Cannot combine dry-run mode with branding.") + Style.RESET_ALL)
        return False
    return True


def _emit_verbosity_banner(level: int) -> None:
    if not level:
        return
    verbose_labels = {
        1: _("Verbosity level 1: cache decisions and summary stats"),
        2: _("Verbosity level 2: include stage-level progress and verification warnings"),
        3: _("Verbosity level 3: extended diagnostics for skipped files"),
    }
    label = verbose_labels.get(level, _("Verbosity level 4: full debug logging enabled"))
    print(Fore.BLUE + label + Style.RESET_ALL)


def _configure_runtime(args: argparse.Namespace, interactive_launch: bool) -> Optional[str]:
    set_worker_cap(1 if getattr(args, "single_worker", False) else None)

    if not is_admin():
        logging.error(_("This script requires administrator privileges"))
        return None

    physical_cores, logical_cores = get_cpu_info()
    announce_mode(args)

    configure_lzx(
        choice_enabled=not args.no_lzx,
        force_lzx=args.force_lzx,
        cpu_capable=config.is_cpu_capable_for_lzx(),
        physical=physical_cores,
        logical=logical_cores,
    )

    directory, updated_args = acquire_directory(args, interactive_launch)
    args.directory = directory
    for key, value in vars(updated_args).items():
        setattr(args, key, value)

    protection_reason = describe_protected_path(directory)
    if protection_reason:
        logging.error(_("Cannot compress protected path: %s"), protection_reason)
        if 'Windows' in protection_reason:
            logging.error(_("To compress Windows system files, use 'compact.exe /compactos:always' instead"))
        return None

    if not confirm_hdd_usage(directory, force_serial=args.single_worker):
        return None

    return directory


def main() -> None:
    # Detect language override before anything else to ensure banner and help text are translated
    override_lang = _detect_language_override(sys.argv[1:])
    load_translations(override_lang)
    
    init(autoreset=True)
    display_banner(VERSION, BUILD_DATE)

    args, interactive_launch = _prepare_arguments(sys.argv[1:])

    if not _validate_modes(args):
        prompt_exit()
        sys.exit(1)

    setup_logging(args.verbose)

    _emit_verbosity_banner(args.verbose)

    if getattr(args, 'one_click', False) and not args.directory:
        if not is_admin():
            logging.error(_("This script requires administrator privileges"))
            prompt_exit()
            return

        physical_cores, logical_cores = get_cpu_info()
        configure_lzx(
            choice_enabled=not args.no_lzx,
            force_lzx=args.force_lzx,
            cpu_capable=config.is_cpu_capable_for_lzx(),
            physical=physical_cores,
            logical=logical_cores,
        )

        run_one_click_mode(verbosity=args.verbose, min_savings=args.min_savings)
        print(_("\nOperation completed."))
        prompt_exit()
        return

    directory = _configure_runtime(args, interactive_launch)
    if directory is None:
        prompt_exit()
        return

    try:
        if getattr(args, "dry_run", False):
            stats, monitor, plan = run_entropy_dry_run(
                directory,
                verbosity=args.verbose,
                min_savings=args.min_savings,
                debug_scan_all=getattr(args, "debug_scan_all", False),
            )

            if plan:
                print()
                try:
                    response = read_user_input(_("Do you want to proceed with compression? [y/N]: ")).strip().lower()
                except KeyboardInterrupt:
                    print(Fore.CYAN + _("\nOperation cancelled by user.") + Style.RESET_ALL)
                    sys.exit(130)

                if response in ('y', 'yes'):
                    print(_("\nStarting compression..."))
                    monitor.start_operation()
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
                else:
                    print(_("Compression cancelled."))
        elif args.brand_files:
            run_branding(directory, thorough=args.thorough)
        else:
            run_compression(
                directory,
                verbosity=args.verbose,
                thorough=args.thorough,
                min_savings=args.min_savings,
                debug_scan_all=getattr(args, "debug_scan_all", False),
            )
    except KeyboardInterrupt:
        print(Fore.CYAN + _("\nOperation cancelled by user.") + Style.RESET_ALL)
        sys.exit(130)

    print(_("\nOperation completed."))
    prompt_exit()


if __name__ == "__main__":
    main()
