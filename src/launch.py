import logging
import os
import shlex
import subprocess
import threading
from argparse import Namespace
from dataclasses import dataclass
from typing import ClassVar, Optional

from colorama import Fore, Style

from . import config, benchmark
from .console import EscapeExit, announce_cancelled, read_user_input
from .file_utils import (
    DRIVE_FIXED,
    DRIVE_REMOTE,
    get_volume_details,
    sanitize_path,
)
from .workers import set_worker_cap
from .i18n import _

def pick_directory_dialog() -> Optional[str]:
    ps_script = """
    Add-Type -AssemblyName System.Windows.Forms
    $f = New-Object System.Windows.Forms.FolderBrowserDialog
    $f.Description = 'Select directory to compress'
    $f.ShowNewFolderButton = $true
    if ($f.ShowDialog() -eq 'OK') {
        Write-Host $f.SelectedPath
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
            startupinfo=startupinfo
        )
        path = result.stdout.strip()
        return path if path else None
    except FileNotFoundError:
        return None


def configure_lzx(choice_enabled: bool, force_lzx: bool, cpu_capable: bool, physical: int, logical: int) -> bool:
    if not choice_enabled:
        if force_lzx:
            logging.info(_("Ignoring -f because -x disables LZX explicitly"))
        print(Fore.YELLOW + _("-x: LZX compression disabled via command line flag."))
        config.COMPRESSION_ALGORITHMS['large'] = 'XPRESS16K'
        return False

    if cpu_capable or force_lzx:
        config.COMPRESSION_ALGORITHMS['large'] = 'LZX'
        if force_lzx and not cpu_capable:
            logging.info(
                _("Forcing LZX compression despite CPU having only {physical} cores and {logical} threads").format(
                    physical=physical, logical=logical
                )
            )
        else:
            logging.info(
                _("Using LZX compression (CPU deemed capable - it has {physical} cores and {logical} threads)").format(
                    physical=physical, logical=logical
                )
            )
        return True

    config.COMPRESSION_ALGORITHMS['large'] = 'XPRESS16K'
    print(Fore.YELLOW + _("\nNotice: Your CPU has {physical} cores and {logical} threads.").format(physical=physical, logical=logical))
    print(_("LZX compression requires at least {min_physical} cores and {min_logical} threads.").format(
        min_physical=config.MIN_PHYSICAL_CORES_FOR_LZX, min_logical=config.MIN_LOGICAL_CORES_FOR_LZX
    ))
    print(_("LZX compression has been disabled for better system responsiveness."))
    print(_("Use -f flag to force LZX if you're feeling adventurous."))
    return False


def confirm_hdd_usage(directory: str, force_serial: bool) -> bool:
    details = get_volume_details(directory)
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

    if details.rotational is not True:
        if details.rotational is None:
            logging.debug(
                _("Drive %s did not report seek penalty; treating as non-HDD. Flash controllers such as eMMC and SD readers may often omit this flag."),
                target_label,
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

    if throttle_requested:
        set_worker_cap(1)
        logging.info(_("Single-worker mode engaged for %s due to HDD safeguards."), target_label)
        if not force_serial:
            print(Fore.YELLOW + _("Running sequentially to keep fragmentation in check.") + Style.RESET_ALL)

    print(Fore.YELLOW + _("\nProceeding with compression on HDD. This may impact system performance.") + Style.RESET_ALL)
    return True


FLAG_METADATA: dict[str, tuple[str, str]] = {
    'verbose': ('-v', 'Set verbosity level (-v/-vvvv); repeat same level to disable'),
    'no_lzx': ('-x', 'Disable LZX compression'),
    'force_lzx': ('-f', 'Force LZX compression'),
    'dry_run': ('-d', 'Dry-run entropy analysis'),
    'single_worker': ('-s', 'Throttle for HDDs'),
    'min_savings': (
        '-m/--min-savings=<percent>',
        f"Set minimum expected savings percentage ({config.MIN_SAVINGS_PERCENT:.0f}-{config.MAX_SAVINGS_PERCENT:.0f})",
    ),
}

SHORT_FLAG_KEYS: dict[str, str] = {
    'x': 'no_lzx',
    'f': 'force_lzx',
    't': 'thorough',
    'b': 'brand_files',
    'd': 'dry_run',
    's': 'single_worker',
}

LONG_FLAG_KEYS: dict[str, str] = {
    'verbose': 'verbose',
    'no-verbose': 'verbose_off',
    'quiet': 'verbose_off',
    'no-lzx': 'no_lzx',
    'force-lzx': 'force_lzx',
    'thorough': 'thorough',
    'brand-files': 'brand_files',
    'dry-run': 'dry_run',
    'single-worker': 'single_worker',
    'min-savings': 'min_savings',
}

_START_COMMANDS: set[str] = {'s', 'start'}
_FLAG_HELP_COMMANDS: set[str] = {'f', 'flags'}

_MUTUALLY_EXCLUSIVE: tuple[tuple[str, str], ...] = (
    ('thorough', 'brand_files'),
    ('no_lzx', 'force_lzx'),
    ('dry_run', 'brand_files'),
)


@dataclass
class LaunchState:
    directory: str = ""
    verbose: int = 0
    no_lzx: bool = False
    force_lzx: bool = False
    thorough: bool = False
    brand_files: bool = False
    dry_run: bool = False
    single_worker: bool = False
    min_savings: float = config.DEFAULT_MIN_SAVINGS_PERCENT

    MAX_VERBOSITY: ClassVar[int] = 4

    def reset_verbose(self) -> None:
        self.verbose = 0

    def set_verbose_level(self, level: int) -> None:
        level = max(0, min(level, self.MAX_VERBOSITY))
        self.verbose = 0 if level == 0 or self.verbose == level else level

    def set_min_savings(self, percent: float) -> None:
        self.min_savings = config.clamp_savings_percent(percent)

    def _silence_conflicts(self, key: str) -> None:
        for primary, secondary in _MUTUALLY_EXCLUSIVE:
            if key == primary and getattr(self, secondary):
                setattr(self, secondary, False)
            elif key == secondary and getattr(self, primary):
                setattr(self, primary, False)

    def toggle(self, key: str) -> None:
        if key == 'min_savings':
            return
        if key == 'verbose':
            self.set_verbose_level(1)
            return
        enabled = not getattr(self, key)
        setattr(self, key, enabled)
        if enabled:
            self._silence_conflicts(key)


def _format_active_flags(state: LaunchState) -> str:
    items: list[str] = []
    if state.verbose:
        items.append(_("Verbose level {level} (-{flags})").format(level=state.verbose, flags='v' * state.verbose))

    for key, (flag, description) in FLAG_METADATA.items():
        if key == 'verbose' or key == 'min_savings':
            continue
        if getattr(state, key):
            items.append(f"{_(description)} ({flag})")
    return ", ".join(items) if items else _("<none>")


def _print_flag_reference() -> None:
    print(Fore.YELLOW + _("\nAvailable flags:") + Style.RESET_ALL)
    for key, (flag, description) in FLAG_METADATA.items():
        print(f"  {flag:<6} {_(description)}")


def _coerce_verbose_value(raw: Optional[str]) -> int:
    if raw is None or raw == "":
        return 1
    try:
        return int(raw)
    except ValueError:
        return 1


def _parse_min_savings(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    stripped = value.strip().rstrip('%')
    try:
        return float(stripped)
    except ValueError:
        return None


def _handle_long_option(option: str, value: Optional[str], state: LaunchState) -> None:
    key = LONG_FLAG_KEYS.get(option)
    if key == 'verbose_off':
        state.reset_verbose()
    elif key == 'verbose':
        state.set_verbose_level(_coerce_verbose_value(value))
    elif key == 'min_savings':
        parsed = _parse_min_savings(value)
        if parsed is None:
            print(
                Fore.RED
                + _("Invalid value for --min-savings. Provide a number between {min} and {max}.").format(
                    min=config.MIN_SAVINGS_PERCENT, max=config.MAX_SAVINGS_PERCENT
                )
                + Style.RESET_ALL
            )
            return
        state.set_min_savings(parsed)
    elif key:
        state.toggle(key)


def _handle_short_bundle(bundle: str, state: LaunchState) -> None:
    index = 1
    upper = len(bundle)
    while index < upper:
        char = bundle[index].lower()
        if char == 'v':
            length = 1
            while index + length < upper and bundle[index + length].lower() == 'v':
                length += 1
            state.set_verbose_level(length)
            index += length
            continue

        mapped = SHORT_FLAG_KEYS.get(char)
        if mapped:
            state.toggle(mapped)
        index += 1


def _apply_flag_string(raw: str, state: LaunchState) -> None:
    tokens = shlex.split(raw, posix=False)
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith('--'):
            option = token[2:]
            value: Optional[str] = None
            if '=' in option:
                option, value = option.split('=', 1)
            elif index + 1 < len(tokens) and not tokens[index + 1].startswith('-'):
                value = tokens[index + 1]
                index += 1
            _handle_long_option(option, value, state)
        elif token.startswith('-') and len(token) > 1:
            lowered = token.lower()
            if lowered.startswith('-m'):
                raw_value = token[2:]
                if raw_value.startswith('='):
                    raw_value = raw_value[1:]
                value = raw_value or None
                if value is None and index + 1 < len(tokens) and not tokens[index + 1].startswith('-'):
                    value = tokens[index + 1]
                    index += 1
                _handle_long_option('min-savings', value, state)
            else:
                _handle_short_bundle(token, state)
        index += 1


def _split_path_and_flags(tokens: list[str]) -> tuple[list[str], list[str]]:
    # Treat leading flags and trailing toggles uniformly; Windows paths don't start with '-'
    path_tokens: list[str] = []
    flag_tokens: list[str] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith('-'):
            flag_tokens.append(token)
            lowered = token.lower()
            if lowered.startswith('-m'):
                raw_value = token[2:]
                if raw_value.startswith('='):
                    raw_value = raw_value[1:]
                if raw_value == "" and index + 1 < len(tokens) and not tokens[index + 1].startswith('-'):
                    flag_tokens.append(tokens[index + 1])
                    index += 1
            index += 1
            continue
        path_tokens.append(token)
        index += 1
    return path_tokens, flag_tokens


def _print_interactive_status(state: LaunchState) -> None:
    active_flags = _format_active_flags(state)
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
    path_tokens, flag_tokens = _split_path_and_flags(parts)
    if flag_tokens:
        _apply_flag_string(" ".join(flag_tokens), state)
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
        _("Toggle flags by entering their short forms together (e.g. -vx) or separately (e.g. -t). Re-enter a flag to disable it.")
    )
    _print_flag_reference()


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
        _apply_flag_string(command, state)
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

        command = _read_interactive_command() or 's'
        lowered = command.lower()

        if lowered in _START_COMMANDS:
            if _can_start(state):
                return
            continue

        if lowered == 'c':
            selected = pick_directory_dialog()
            if selected:
                state.directory = sanitize_path(selected)
            continue

        if lowered in _FLAG_HELP_COMMANDS:
            _display_flag_help()
            continue

        _process_command(command, state)


def _apply_state_to_args(args: Namespace, state: LaunchState) -> Namespace:
    args.directory = state.directory
    args.verbose = state.verbose
    args.no_lzx = state.no_lzx
    args.force_lzx = state.force_lzx
    args.thorough = state.thorough
    args.brand_files = state.brand_files
    setattr(args, 'dry_run', state.dry_run)
    args.single_worker = state.single_worker
    args.min_savings = config.clamp_savings_percent(state.min_savings)
    return args


def interactive_configure(args: Namespace) -> Namespace:
    state = LaunchState(
        directory=sanitize_path(args.directory) if args.directory else "",
        verbose=args.verbose,
        no_lzx=args.no_lzx,
        force_lzx=args.force_lzx,
        thorough=args.thorough,
        brand_files=args.brand_files,
        dry_run=getattr(args, 'dry_run', False),
        single_worker=getattr(args, 'single_worker', False),
        min_savings=config.clamp_savings_percent(getattr(args, 'min_savings', config.DEFAULT_MIN_SAVINGS_PERCENT)),
    )

    print(Fore.YELLOW + _("\nInteractive launch detected. Configure your run before starting.") + Style.RESET_ALL)
    _print_flag_reference()

    # Run benchmark to gauge system performance
    if not benchmark.run_benchmark():
        state.no_lzx = True
        print(Fore.YELLOW + _("\nNotice: LZX compression has been disabled to prevent slow startup times for compressed applications.") + Style.RESET_ALL)

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

        # Force interactive mode if directory is missing/invalid
        args.directory = ""
        args = interactive_configure(args)
