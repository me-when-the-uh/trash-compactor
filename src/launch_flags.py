import shlex
from dataclasses import dataclass
from typing import ClassVar, Optional

from colorama import Fore, Style

from . import config
from .i18n import _

FLAG_METADATA: dict[str, tuple[str, str]] = {
    'verbose': ('-v', 'Set verbosity level (-v/-vvv); repeat same level to disable'),
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
    'd': 'dry_run',
    's': 'single_worker',
}

LONG_FLAG_KEYS: dict[str, str] = {
    'verbose': 'verbose',
    'no-verbose': 'verbose_off',
    'quiet': 'verbose_off',
    'no-lzx': 'no_lzx',
    'force-lzx': 'force_lzx',
    'dry-run': 'dry_run',
    'single-worker': 'single_worker',
    'min-savings': 'min_savings',
}

START_COMMANDS: set[str] = {'s', 'start'}
FLAG_HELP_COMMANDS: set[str] = {'f', 'flags'}

_MUTUALLY_EXCLUSIVE: tuple[tuple[str, str], ...] = (
    ('no_lzx', 'force_lzx'),
)


@dataclass
class LaunchState:
    directory: str = ""
    one_click: bool = False
    verbose: int = 0
    no_lzx: bool = False
    force_lzx: bool = False
    dry_run: bool = False
    single_worker: bool = False
    min_savings: float = config.DEFAULT_MIN_SAVINGS_PERCENT

    MAX_VERBOSITY: ClassVar[int] = 3

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


def format_active_flags(state: LaunchState) -> str:
    items: list[str] = []
    if state.verbose:
        items.append(_("Verbose level {level} (-{flags})").format(level=state.verbose, flags='v' * state.verbose))

    for key, (flag, description) in FLAG_METADATA.items():
        if key == 'verbose' or key == 'min_savings':
            continue
        if getattr(state, key):
            items.append(f"{_(description)} ({flag})")
    return ", ".join(items) if items else _("<none>")


def print_flag_reference() -> None:
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


def apply_flag_string(raw: str, state: LaunchState) -> None:
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


def split_path_and_flags(tokens: list[str]) -> tuple[list[str], list[str]]:
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