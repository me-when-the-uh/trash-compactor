import os
from collections.abc import Iterable
from typing import Final, Set, Tuple

import psutil


def _flatten(groups: Iterable[Iterable[str]]) -> Set[str]:
    return {ext for group in groups for ext in group}


_ARCHIVES = ('.zip', '.rar', '.7z', '.gz', '.xz', '.bz2', '.tar')
_DISK_IMAGES = ('.iso', '.img', '.squashfs', '.appimage', '.vdi', '.vmdk', '.vhd', '.vhdx', '.qcow2', '.qed', '.vpc', '.hdd', '.raw')
_IMAGES = ('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.heic', '.heif', '.avif', '.jxl', '.tiff')
_VIDEO = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.hevc', '.h264', '.h265', '.vp8', '.vp9', '.av1', '.wmv', '.flv', '.3gp')
_AUDIO = ('.mp3', '.aac', '.ogg', '.m4a', '.opus', '.flac', '.wav', '.wma', '.ac3', '.dts', '.alac', '.ape', '.aiff', '.pcm', '.vgz', '.vgm')
_ML = ('.gguf', '.h5', '.onnx', '.pb', '.tflite', '.safetensors', '.torch', '.pt')
_OFFICE = ('.docx', '.xlsx', '.pptx', '.odt', '.ods', '.pdf')

SKIP_EXTENSIONS: Final[Set[str]] = _flatten((
    _ARCHIVES,
    _DISK_IMAGES,
    _IMAGES,
    _VIDEO,
    _AUDIO,
    _ML,
    _OFFICE,
))

MIN_SAVINGS_PERCENT: Final[float] = 0.0
MAX_SAVINGS_PERCENT: Final[float] = 90.0
DEFAULT_MIN_SAVINGS_PERCENT: Final[float] = 10.0


def clamp_savings_percent(value: float) -> float:
    return max(MIN_SAVINGS_PERCENT, min(MAX_SAVINGS_PERCENT, value))


def entropy_from_savings(percent: float) -> float:
    clamped = clamp_savings_percent(percent)
    return max(0.0, 8.0 * (1 - clamped / 100.0))


def savings_from_entropy(entropy: float) -> float:
    entropy = max(0.0, min(8.0, entropy))
    return max(0.0, (1 - entropy / 8.0) * 100.0)


ENTROPY_SKIP_THRESHOLD: Final[float] = entropy_from_savings(DEFAULT_MIN_SAVINGS_PERCENT)

MIN_COMPRESSIBLE_SIZE: Final[int] = 8 * 1024  # 8KB minimum
SIZE_THRESHOLDS: Final[Tuple[Tuple[int, str], ...]] = (
    (64 * 1024, 'tiny'),
    (256 * 1024, 'small'),
    (1024 * 1024, 'medium'),
)


def _default_excluded_directories() -> Tuple[str, ...]:
    system_drive = os.environ.get('SystemDrive', 'C:')
    drive_root = system_drive if system_drive.endswith(('\\', '/')) else f"{system_drive}\\"

    def _drive_path(segment: str) -> str:
        return os.path.join(drive_root, segment)

    entries = [
        os.environ.get('SystemRoot') or _drive_path('Windows'),
        _drive_path('$Recycle.Bin'),
        _drive_path('System Volume Information'),
        _drive_path('Recovery'),
        _drive_path('PerfLogs'),
        _drive_path('Windows.old'),
    ]

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

MIN_LOGICAL_CORES_FOR_LZX: Final[int] = 5
MIN_PHYSICAL_CORES_FOR_LZX: Final[int] = 3

DEFAULT_EXCLUDE_DIRECTORIES: Final[Tuple[str, ...]] = _default_excluded_directories()


def get_cpu_info() -> Tuple[int | None, int | None]:
    physical = psutil.cpu_count(logical=False)
    logical = psutil.cpu_count(logical=True)
    return physical, logical


def is_cpu_capable_for_lzx() -> bool:
    physical, logical = get_cpu_info()
    if physical is None or logical is None:
        return False
    return physical >= MIN_PHYSICAL_CORES_FOR_LZX and logical >= MIN_LOGICAL_CORES_FOR_LZX


COMPRESSION_ALGORITHMS: Final[dict[str, str]] = {
    'tiny': 'XPRESS4K',
    'small': 'XPRESS8K',
    'medium': 'XPRESS16K',
    'large': 'LZX',
}