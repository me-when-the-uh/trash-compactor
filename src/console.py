import sys
from colorama import Fore, Style
from .i18n import _


BANNER = r"""
 _____               _             ___                                 _             
/__   \_ __ __ _ ___| |__         / __\___  _ __ ___  _ __   __ _  ___| |_ ___  _ __ 
  / /\/ '__/ _` / __| '_ \ _____ / /  / _ \| '_ ` _ \| '_ \ / _` |/ __| __/ _ \| '__|
 / /  | | | (_| \__ \ | | |_____/ /__| (_) | | | | | | |_) | (_| | (__| || (_) | |   
 \/   |_|  \__,_|___/_| |_|     \____/\___/|_| |_| |_| .__/ \__,_|\___|\__\___/|_|   
                                                     |_|                             
"""


class EscapeExit(Exception):
    """Raised when the user exits by pressing Esc twice"""


def announce_cancelled() -> None:
    print(Fore.CYAN + _("\nOperation cancelled by user.") + Style.RESET_ALL)


def _read_msvcrt_input(prompt: str) -> str:
    import msvcrt

    sys.stdout.write(prompt)
    sys.stdout.flush()

    buffer: list[str] = []
    escape_count = 0

    while True:
        key = msvcrt.getwch()

        if key == '\x03':  # Ctrl+C
            sys.stdout.write('\n')
            sys.stdout.flush()
            raise KeyboardInterrupt()

        if key == '\x1b':  # Escape
            escape_count += 1
            if escape_count >= 2:
                sys.stdout.write('\n')
                sys.stdout.flush()
                raise EscapeExit()
            continue

        escape_count = 0

        if key in {'\r', '\n'}:
            sys.stdout.write('\n')
            sys.stdout.flush()
            return ''.join(buffer)

        if key in {'\b', '\x08'}:
            if buffer:
                buffer.pop()
                sys.stdout.write('\b \b')
                sys.stdout.flush()
            continue

        if key in {'\x00', '\xe0'}:
            # Swallow extended key prefix (arrow keys, etc.)
            msvcrt.getwch()
            continue

        buffer.append(key)
        sys.stdout.write(key)
        sys.stdout.flush()


def read_user_input(prompt: str) -> str:
    try:
        return _read_msvcrt_input(prompt)
    except ImportError:
        return input(prompt)


def display_banner(version: str, build_date: str) -> None:
    print(Fore.CYAN + Style.BRIGHT + BANNER)
    print(Fore.GREEN + _("Version: {version}    Build Date: {build_date}\n").format(version=version, build_date=build_date))


def prompt_exit() -> None:
    try:
        import msvcrt
    except ImportError:
        try:
            input(_("\nPress Enter twice to exit..."))
            input()
        except KeyboardInterrupt:
            pass
        return

    print(Fore.YELLOW + _("\nPress Esc twice to exit, or use Ctrl+C.") + Style.RESET_ALL)
    escape_count = 0
    try:
        while True:
            key = msvcrt.getwch()
            if key == '\x1b':
                escape_count += 1
                if escape_count >= 2:
                    print(Fore.CYAN + _("Exiting...") + Style.RESET_ALL)
                    return
                continue
            if key == '\x03':
                announce_cancelled()
                return
            if key in {'\r', '\n'}:
                escape_count = 0
                continue
            escape_count = 0
    except KeyboardInterrupt:
        announce_cancelled()
        return
