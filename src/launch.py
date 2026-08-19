import logging
import os
import shlex
import subprocess
from argparse import Namespace
from typing import Optional

from colorama import Fore, Style

from . import config, benchmark
from .console import EscapeExit, announce_cancelled, read_user_input
from .drive_inspector import DRIVE_FIXED, DRIVE_REMOTE, get_volume_details
from .file_utils import sanitize_path
from .launch_flags import (
    FLAG_HELP_COMMANDS,
    LaunchState,
    START_COMMANDS,
    apply_flag_string,
    format_active_flags,
    print_flag_reference,
    split_path_and_flags,
)
from .workers import set_worker_cap
from .i18n import _

def pick_directory_dialog() -> Optional[str]:
    ps_script = """
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    Add-Type -AssemblyName System.Windows.Forms
    $f = New-Object System.Windows.Forms.FolderBrowserDialog
    $f.Description = 'Select directory to compress'
    $f.ShowNewFolderButton = $true
    if ($f.ShowDialog() -eq 'OK') {
        Write-Output $f.SelectedPath
    }
    """
    
    cmd = ["powershell", "-NoProfile", "-Command", ps_script]
    
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True, 
            encoding='utf-8',
            errors='replace',
            startupinfo=startupinfo
        )
        path = result.stdout.strip()
        if not path:
            print(Fore.YELLOW + _("Folder picker returned no selection; you can type a path instead.") + Style.RESET_ALL)
        return path if path else None
    except FileNotFoundError:
        print(Fore.YELLOW + _("PowerShell is required for the folder picker; type a path instead.") + Style.RESET_ALL)
        return None


def configure_lzx(
    choice_enabled: bool,
    force_lzx: bool,
    benchmark_ok: Optional[bool] = None,
    disabled_reason: Optional[str] = None,
    announce: bool = True,
) -> bool:
    def _disable_with_benchmark_warning() -> None:
        if not announce:
            return
        print(Fore.YELLOW + _("LZX compression disabled because startup benchmark exceeded the safe limit."))
        print(_("Use -f flag to force LZX anyway."))

    if not choice_enabled:
        if force_lzx and announce:
            logging.info(_("Ignoring -f because -x disables LZX explicitly"))
        if disabled_reason == 'benchmark':
            _disable_with_benchmark_warning()
        else:
            if announce:
                print(Fore.YELLOW + _("-x: LZX compression disabled via command line flag."))
        config.COMPRESSION_ALGORITHMS['large'] = 'XPRESS16K'
        return False

    if benchmark_ok is None:
        benchmark_ok = benchmark.run_benchmark()

    if benchmark_ok or force_lzx:
        config.COMPRESSION_ALGORITHMS['large'] = 'LZX'
        if announce and force_lzx and not benchmark_ok:
            logging.info(_("Forcing LZX compression despite startup benchmark timeout."))
        elif announce:
            logging.info(_("Using LZX compression."))
        return True

    config.COMPRESSION_ALGORITHMS['large'] = 'XPRESS16K'
    _disable_with_benchmark_warning()
    return False


def confirm_hdd_usage(directory: str, force_serial: bool) -> bool:
    from .drive_inspector import get_volume_details_fast

    details = get_volume_details_fast(directory)
    throttle_requested = force_serial  # Carry over manual single-worker overrides
    target_label = details.drive_letter or directory

    if details.anchor is None:
        logging.error(_("Unable to resolve volume for %s"), directory)
        print(Fore.RED + _("Unable to resolve the target volume. Please verify the path.") + Style.RESET_ALL)
        return False

    if details.drive_type == DRIVE_REMOTE:
        logging.error(_("Network shares are not supported for compression targets: %s"), directory)
        print(Fore.RED + _("Network shares are not supported targets for compression.") + Style.RESET_ALL)
        print(_("Please select a local NTFS volume instead."))
        return False

    if details.filesystem and details.filesystem != 'NTFS':
        logging.error(
            _("Compression requires NTFS, but %s reports %s"),
            details.drive_letter or directory,
            details.filesystem,
        )
        print(Fore.RED + _("Windows compression requires NTFS.") + Style.RESET_ALL)
        print(_("Detected filesystem: {filesystem}").format(filesystem=details.filesystem or 'unknown'))
        return False

    if details.drive_type != DRIVE_FIXED:
        logging.info(
            _("Volume %s is not a fixed disk (type=%s); skipping HDD warning."),
            target_label,
            details.drive_type,
        )
        if throttle_requested:
            set_worker_cap(1)
            logging.info(_("Single-worker mode honored even though the drive is not fixed media."))
        return True

    # Only fixed drives need the full probe (seek penalty / media type)
    details = get_volume_details(directory)

    if details.rotational is not True:
        if details.rotational is None:
            logging.debug(
                _("Drive %s did not report seek penalty; treating as non-HDD. Flash controllers such as eMMC and SD readers may often omit this flag. method=%s media=%s"),
                target_label,
                getattr(details, 'detection_method', ''),
                getattr(details, 'media_type', None),
            )
        if throttle_requested:
            set_worker_cap(1)
            logging.info(_("Single-worker mode requested explicitly for %s."), target_label)
        return True

    print(Fore.YELLOW + _("Detected a traditional spinning hard drive for this path.") + Style.RESET_ALL)
    print(_("Sustained compression can thrash the disk heads, fragment files, and slow app/game launches.") + Style.RESET_ALL)
    print(Fore.YELLOW + _("\nRecommendation:") + Style.RESET_ALL)
    print(_("• Run the task during idle hours and use the single-worker mode (-s)"))
    print(_("• Defragment the drive once compression finishes"))
    print(_("• Prefer compressing rarely modified folders on HDDs"))


    print("\n" + Fore.YELLOW + _("\nDo you want to proceed anyway? (y/n): ") + Style.RESET_ALL, end="")
    try:
        response = read_user_input("").strip().lower()
    except (KeyboardInterrupt, EscapeExit):
        announce_cancelled()
        return False
    if response not in {"y", "yes"}:
        print(Fore.CYAN + _("Operation cancelled.") + Style.RESET_ALL)
        return False

    if not throttle_requested:
        print(Fore.YELLOW + _("\nThrottle compression to a single worker to avoid disk fragmentation? (Y/n): ") + Style.RESET_ALL, end="")
        try:
            throttle_response = read_user_input("").strip().lower()
        except (KeyboardInterrupt, EscapeExit):
            announce_cancelled()
            return False
        throttle_requested = throttle_response in {"", "y", "yes"}

    from .workers import set_hdd_mode, set_worker_cap
    set_hdd_mode(True)
    logging.info(_("HDD mode engaged for %s: sequential scan/entropy/compression, smaller batches."), target_label)
    if throttle_requested:
        set_worker_cap(1)
        logging.info(_("Single-worker mode engaged for %s due to HDD safeguards."), target_label)
        if not force_serial:
            print(Fore.YELLOW + _("Running sequentially to keep fragmentation in check.") + Style.RESET_ALL)
    else:
        print(Fore.YELLOW + _("HDD mode: scanning, entropy sampling, and compression all run sequentially so the disk head moves in order instead of jumping.") + Style.RESET_ALL)

    print(Fore.YELLOW + _("\nProceeding with compression on HDD. This may impact system performance.") + Style.RESET_ALL)
    return True


def print_defrag_hint(compressed_files: int) -> None:
    """Suggest defragmenting after compression on a spinning drive."""
    from .workers import hdd_mode

    if hdd_mode() and compressed_files > 0:
        print(
            Fore.YELLOW
            + _(
                "\nNTFS compression fragmented files on this hard drive. "
                "Consider defragmenting the drive now: defrag.exe /C"
            )
            + Style.RESET_ALL
        )


def _print_interactive_status(state: LaunchState) -> None:
    active_flags = format_active_flags(state)
    current_directory = state.directory or _("<not set>")
    print(
        Fore.CYAN
        + _("\nCurrent directory: {directory}\nActive flags: {flags}\nMin savings threshold: {savings:.1f}%").format(
            directory=current_directory,
            flags=active_flags,
            savings=state.min_savings
        )
        + Style.RESET_ALL
    )


def _apply_composite_command(parts: list[str], state: LaunchState) -> bool:
    # Returns True if a path was supplied, so the function caller can short-circuit the default handler
    if not parts:
        return False
    path_tokens, flag_tokens = split_path_and_flags(parts)
    if flag_tokens:
        apply_flag_string(" ".join(flag_tokens), state)
    if path_tokens:
        state.directory = sanitize_path(" ".join(path_tokens))
        return True
    return False


def _read_interactive_command() -> str:
    try:
        return read_user_input("> ").strip()
    except (KeyboardInterrupt, EscapeExit):
        announce_cancelled()
        raise SystemExit(0)


def _display_flag_help() -> None:
    print(
        _("Toggle flags by entering their short forms together (e.g. -vx) or separately (e.g. -d). Re-enter a flag to disable it.")
    )
    print_flag_reference()


def _can_start(state: LaunchState) -> bool:
    if not state.directory:
        print(Fore.RED + _("Directory is required before starting.") + Style.RESET_ALL)
        return False
    if not os.path.exists(state.directory):
        print(
            Fore.RED
            + _("Directory '{directory}' was not found.").format(directory=state.directory)
            + Style.RESET_ALL
        )
        return False
    return True


def _tokenize_command(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return []


def _process_command(command: str, state: LaunchState) -> None:
    if command.startswith('-'):
        apply_flag_string(command, state)
        return

    parts = _tokenize_command(command)
    if _apply_composite_command(parts, state):
        return

    state.directory = sanitize_path(command)


def _run_interactive_session(state: LaunchState) -> None:
    while True:
        _print_interactive_status(state)
        print(
            _("Enter a directory path (optionally add flags like '-vx'), or use [S]tart to proceed, [C]hoose directory, and [F]lag help for tips.")
        )
        print(_("Press '1' then Enter to compress necessary directories in one click."))

        command = _read_interactive_command().strip()
        if not command:
            continue
        lowered = command.lower()

        if lowered == '1':
            state.one_click = True
            state.directory = ""
            return

        if lowered in START_COMMANDS:
            if _can_start(state):
                return
            continue

        if lowered == 'c':
            selected = pick_directory_dialog()
            if selected:
                state.directory = sanitize_path(selected)
            continue

        if lowered in FLAG_HELP_COMMANDS:
            _display_flag_help()
            continue

        _process_command(command, state)


def _apply_state_to_args(args: Namespace, state: LaunchState) -> Namespace:
    args.directory = state.directory
    setattr(args, 'one_click', getattr(state, 'one_click', False))
    args.verbose = state.verbose
    args.no_lzx = state.no_lzx
    args.force_lzx = state.force_lzx
    setattr(args, 'dry_run', state.dry_run)
    args.single_worker = state.single_worker
    args.min_savings = config.clamp_savings_percent(state.min_savings)
    return args


def interactive_configure(args: Namespace) -> Namespace:
    state = LaunchState(
        directory=sanitize_path(args.directory) if args.directory else "",
        one_click=getattr(args, 'one_click', False),
        verbose=args.verbose,
        no_lzx=args.no_lzx,
        force_lzx=args.force_lzx,
        dry_run=getattr(args, 'dry_run', False),
        single_worker=getattr(args, 'single_worker', False),
        min_savings=config.clamp_savings_percent(getattr(args, 'min_savings', config.DEFAULT_MIN_SAVINGS_PERCENT)),
    )

    print(Fore.YELLOW + _("\nInteractive launch detected.") + Style.RESET_ALL)
    quick = read_user_input(_("Press '1' for 1-click unattended mode, or Enter for custom setup: ")).strip().lower()
    if quick == '1':
        state.one_click = True
        state.directory = ""
        return _apply_state_to_args(args, state)

    print_flag_reference()

    _run_interactive_session(state)
    return _apply_state_to_args(args, state)


def acquire_directory(args: Namespace, interactive_launch: bool) -> tuple[str, Namespace]:
    while True:
        candidate = sanitize_path(args.directory) if args.directory else ""
        if candidate and os.path.exists(candidate):
            return candidate, args

        if candidate:
            print(Fore.RED + _("Directory '{candidate}' does not exist.").format(candidate=candidate) + Style.RESET_ALL)
        else:
            print(Fore.RED + _("No directory provided.") + Style.RESET_ALL)

        # Without a console there is no user to ask; fail instead of looping.
        from .console import _interactive_console

        if not _interactive_console():
            return "", args

        # Force interactive mode if directory is missing/invalid
        args.directory = ""
        args = interactive_configure(args)
        if getattr(args, "one_click", False):
            return "", args
