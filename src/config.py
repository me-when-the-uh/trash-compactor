import os
from collections.abc import Iterable
from typing import Final, Set, Tuple

import psutil


def _flatten(groups: Iterable[Iterable[str]]) -> Set[str]:
    return {ext for group in groups for ext in group}


_ARCHIVES = ('.zip', '.rar', '.7z', '.gz', '.xz', '.bz2')
_DISK_IMAGES = ('.squashfs', '.appimage', '.vdi', '.vmdk', '.vhd', '.vhdx', '.qcow2', '.qed', '.vpc', '.hdd', '.iso')
_IMAGES = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.heic', '.heif', '.avif', '.jxl')
_VIDEO = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.hevc', '.h264', '.h265', '.vp8', '.vp9', '.av1', '.wmv', '.flv', '.3gp')
_AUDIO = ('.mp3', '.aac', '.ogg', '.m4a', '.opus', '.flac', '.wma', '.ac3', '.dts', '.alac', '.ape', '.vgz')
_ML = ('.gguf', '.h5', '.pb', '.tflite', '.safetensors', '.torch', '.pt')
_OFFICE = ('.docx', '.xlsx', '.pptx', '.odt', '.ods', '.pdf')
_DATABASES = ('.mdf', '.ldf', '.sqlite', '.sqlite3', '.db', '.db3', '.mdb', '.accdb', '.pst', '.ost', '.edb')
_INCOMPLETE = ('.crdownload', '.part', '.tmp')

SKIP_EXTENSIONS: Final[Set[str]] = _flatten((
    _ARCHIVES,
    _DISK_IMAGES,
    _IMAGES,
    _VIDEO,
    _AUDIO,
    _ML,
    _OFFICE,
    _DATABASES,
    _INCOMPLETE,
))

MIN_SAVINGS_PERCENT: Final[float] = 0.0
MAX_SAVINGS_PERCENT: Final[float] = 90.0
DEFAULT_MIN_SAVINGS_PERCENT: Final[float] = 15.0


def clamp_savings_percent(value: float) -> float:
    return max(MIN_SAVINGS_PERCENT, min(MAX_SAVINGS_PERCENT, value))


def entropy_from_savings(percent: float) -> float:
    clamped = clamp_savings_percent(percent)
    return max(0.0, 8.0 * (1 - clamped / 100.0))


def savings_from_entropy(entropy: float) -> float:
    entropy = max(0.0, min(8.0, entropy))
    return max(0.0, (1 - entropy / 8.0) * 100.0)


ENTROPY_SKIP_THRESHOLD: Final[float] = entropy_from_savings(DEFAULT_MIN_SAVINGS_PERCENT)

ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE: Final[int] = 2 * 1024 * 1024  # 2MB
ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE: Final[int] = 100 * 1024 * 1024 # 100MB
ENTROPY_BASE_SAMPLE_WINDOWS: Final[int] = 3
ENTROPY_DYNAMIC_WINDOWS_MIN: Final[int] = 4
ENTROPY_DYNAMIC_WINDOWS_MAX: Final[int] = 20
ENTROPY_TARGET_WINDOW_SIZE: Final[int] = 16 * 1024

ENTROPY_MAX_FILE_BUDGET: Final[int] = ENTROPY_DYNAMIC_WINDOWS_MAX * ENTROPY_TARGET_WINDOW_SIZE


def _entropy_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


ENTROPY_MAX_FILES: Final[int] = _entropy_env_int("TRASH_COMPACTOR_ENTROPY_MAX_FILES", 50)
ENTROPY_MAX_BYTES: Final[int] = _entropy_env_int(
    "TRASH_COMPACTOR_ENTROPY_MAX_BYTES",
    8 * 1024 * 1024,
)

MIN_COMPRESSIBLE_SIZE: Final[int] = 8 * 1024  # 8KB minimum
SIZE_THRESHOLDS: Final[Tuple[Tuple[int, str], ...]] = (
    (64 * 1024, 'tiny'),
    (256 * 1024, 'small'),
    (1024 * 1024, 'medium'),
)


def _fixed_drive_roots() -> Tuple[str, ...]:
    """Return 'X:\\' roots of all fixed (non-removable) drives.

    Uses ctypes GetLogicalDrives + GetDriveTypeW (no extra dependencies).
    Falls back to the system drive only when the API is unavailable.
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _get_drive_type = kernel32.GetDriveTypeW
        _get_drive_type.argtypes = [wintypes.LPCWSTR]
        _get_drive_type.restype = wintypes.UINT
        _logical_drives = kernel32.GetLogicalDrives
        _logical_drives.restype = wintypes.DWORD

        DRIVE_FIXED = 3
        bitmask = _logical_drives()
        roots: list[str] = []
        for index in range(26):
            if bitmask & (1 << index):
                root = f"{chr(ord('A') + index)}:\\"
                if _get_drive_type(root) == DRIVE_FIXED:
                    roots.append(root)
        if roots:
            return tuple(roots)
    except Exception:
        pass

    # Fallback: only the system drive.
    system_drive = os.environ.get('SystemDrive', 'C:')
    return (system_drive if system_drive.endswith(('\\', '/')) else f"{system_drive}\\",)


def _default_excluded_directories() -> Tuple[str, ...]:
    system_root = os.environ.get('SystemRoot') or ''
    system_root_norm = os.path.normcase(os.path.normpath(system_root)) if system_root else ''

    def _drive_path(root: str, segment: str) -> str:
        return os.path.join(root, segment)

    entries: list[str] = []

    for drive_root in _fixed_drive_roots():
        # The active Windows install may live on any fixed drive; always exclude
        # it, even if SystemDrive does not point at it.
        windows_root = _drive_path(drive_root, 'Windows')
        if not system_root_norm or os.path.normcase(os.path.normpath(windows_root)) == system_root_norm:
            entries.append(windows_root)

        # Stale installs: Windows.old is always protected at the drive root, and
        # Windows.old.NNN (e.g. Windows.old.000) via the dotted-prefix entry.
        # Nonexistent entries are harmless; matching is a prefix check.
        entries.append(_drive_path(drive_root, 'Windows.old'))
        entries.append(_drive_path(drive_root, 'Windows.old.'))
        try:
            with os.scandir(drive_root) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    if name.lower().startswith('windows.old') and (
                        len(name) == len('windows.old') or name[len('windows.old')] == '.'
                    ):
                        entries.append(entry.path)
        except OSError:
            pass

        # Always-protected system directories on every fixed drive.
        for segment in ('$Recycle.Bin', 'System Volume Information', 'Recovery', 'PerfLogs'):
            entries.append(_drive_path(drive_root, segment))

    if not entries:
        # Absolute fallback: protect the system root even if drive probing failed.
        entries.append(os.environ.get('SystemRoot') or system_root)

    seen: set[str] = set()
    cleaned: list[str] = []
    for entry in entries:
        if not entry:
            continue
        normalized = os.path.normcase(os.path.normpath(entry))
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(os.path.normpath(entry))
    return tuple(cleaned)

BENCHMARK_DURATION_LIMIT: Final[float] = 0.25
BENCHMARK_WORKLOAD_ITERATIONS: Final[int] = 125_000

DEFAULT_EXCLUDE_DIRECTORIES: Final[Tuple[str, ...]] = _default_excluded_directories()


def get_cpu_info() -> Tuple[int | None, int | None]:
    physical = psutil.cpu_count(logical=False)
    logical = psutil.cpu_count(logical=True)
    return physical, logical


COMPRESSION_ALGORITHMS: Final[dict[str, str]] = {
    'tiny': 'XPRESS4K',
    'small': 'XPRESS8K',
    'medium': 'XPRESS16K',
    'large': 'LZX',
}
DRY_RUN_CONSERVATIVE_FACTORS: Final[dict[str, float]] = {
    'XPRESS4K': 1.05,
    'XPRESS8K': 1.02,
    'XPRESS16K': 1.0,
    'LZX': 0.95,
}