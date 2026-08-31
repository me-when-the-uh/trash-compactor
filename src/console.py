import os
import sys
from colorama import Fore, Style
from .i18n import _

_ATTACH_PARENT_PROCESS = 0xFFFFFFFF


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


def display_banner(version: str, build_date: str) -> None:
    print(Fore.CYAN + Style.BRIGHT + BANNER)
    print(Fore.GREEN + _("Version: {version}    Build Date: {build_date}\n").format(version=version, build_date=build_date))


def _interactive_console() -> bool:
    """True when stdin is attached to a real interactive console.

    sys.stdin.isatty() alone is unreliable on Windows (NUL reports True), so
    probe the console handle directly with GetConsoleMode.
    """
    try:
        if not sys.stdin.isatty():
            return False
        if os.name != "nt":
            return True
    except (AttributeError, ValueError):
        return False

    import ctypes
    from ctypes import wintypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        ok = kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        return bool(ok)
    except Exception:
        return False


def _std_handle_valid() -> bool:
    """True when stdout is a real non-console handle (file/pipe redirect)."""
    if os.name != "nt" or sys.stdout is None:
        try:
            return sys.stdout is not None and sys.stdout.fileno() >= 0
        except (OSError, ValueError, AttributeError):
            return False

    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
        file_type = kernel32.GetFileType(handle)
        # FILE_TYPE_CHAR (2) = console or NUL; treat NUL as not a redirect.
        # FILE_TYPE_DISK (1) / FILE_TYPE_PIPE (3) = genuine redirection.
        if file_type in (1, 3):
            return True
        return False
    except Exception:
        return False


def _stdin_handle_valid() -> bool:
    """True when stdin is a real file/pipe (redirected). Mirror of `_std_handle_valid`."""
    if os.name != "nt" or sys.stdin is None:
        try:
            return sys.stdin is not None and sys.stdin.fileno() >= 0
        except (OSError, ValueError, AttributeError):
            return False

    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
        file_type = kernel32.GetFileType(handle)
        # FILE_TYPE_DISK (1) / FILE_TYPE_PIPE (3) = genuine redirection.
        # FILE_TYPE_CHAR (2) = console or NUL; FILE_TYPE_UNKNOWN (0) = no stdin.
        return file_type in (1, 3)
    except Exception:
        return False


def cprint(color: str, message: str) -> None:
    if not _interactive_console():
        print(message)
        return
    print(color + message + Style.RESET_ALL)


def _policy_default_for_prompt(prompt: str) -> str:
    p = prompt.lower()
    if "throttle" in p:
        return "y"
    if "proceed anyway" in p:
        return "n"
    if "proceed" in p or "compression" in p:
        return "n"
    return ""


def read_user_input(prompt: str) -> str:
    if not _interactive_console():
        try:
            return input(prompt)
        except (EOFError, OSError):
            return _policy_default_for_prompt(prompt)
    try:
        return _read_msvcrt_input(prompt)
    except ImportError:
        try:
            return input(prompt)
        except (EOFError, OSError):
            return _policy_default_for_prompt(prompt)


def attach_to_parent_console() -> bool:
    """Attach to the parent process console (cmd/PowerShell/terminal).

    Redirected std handles (e.g. `exe ... > out.txt`) stay redirected.
    Only missing or console-less handles are rebound to the attached console.
    """
    if _interactive_console():
        return True
    if os.name != "nt":
        return True

    stdout_redirected = _std_handle_valid()
    stdin_redirected = _stdin_handle_valid()

    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.AttachConsole(_ATTACH_PARENT_PROCESS):
            # ERROR_INVALID_HANDLE (6): the process already has a console.
            err = ctypes.get_last_error()
            if err == 6:
                return True
            return False

        # Rebind whichever side is missing, but never overwrite a real redirect
        if not stdout_redirected:
            try:
                stdout_fd = os.open("CONOUT$", os.O_WRONLY)
                stderr_fd = os.open("CONOUT$", os.O_WRONLY)
                sys.stdout = os.fdopen(stdout_fd, "w", encoding="utf-8", errors="replace")
                sys.stderr = os.fdopen(stderr_fd, "w", encoding="utf-8", errors="replace")
            except OSError:
                pass
        if not stdin_redirected:
            try:
                stdin_fd = os.open("CONIN$", os.O_RDWR)
                sys.stdin = os.fdopen(stdin_fd, "r", encoding="utf-8", errors="replace")
            except OSError:
                pass
        return True
    except Exception:
        return False


def allocate_console() -> bool:
    """Allocate a fresh console window (double-clicked CLI fallback)."""
    if _interactive_console():
        return True
    if os.name != "nt":
        return True
    if _stdin_handle_valid():
        return False

    import ctypes

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if not kernel32.AllocConsole():
            return False
        try:
            stdin_fd = os.open("CONIN$", os.O_RDWR)
            stdout_fd = os.open("CONOUT$", os.O_WRONLY)
            stderr_fd = os.open("CONOUT$", os.O_WRONLY)
            sys.stdin = os.fdopen(stdin_fd, "r", encoding="utf-8", errors="replace")
            sys.stdout = os.fdopen(stdout_fd, "w", encoding="utf-8", errors="replace")
            sys.stderr = os.fdopen(stderr_fd, "w", encoding="utf-8", errors="replace")
        except OSError:
            pass
        return True
    except Exception:
        return False


def prompt_exit() -> None:
    # Skip the keypress wait when stdin is not interactive (piped/scripted runs).
    if not _interactive_console():
        return

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
