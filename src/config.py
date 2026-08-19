import os
from collections.abc import Iterable
from dataclasses import dataclass
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


def _entropy_env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default


@dataclass(frozen=True)
class EntropySamplingParams:
    dynamic_windows_min_file_size: int
    dynamic_windows_max_file_size: int
    huge_windows_file_size: int
    base_sample_windows: int
    dynamic_windows_min: int
    dynamic_windows_max: int
    huge_windows_max: int
    target_window_size: int


ENTROPY_SAMPLING_PARAMS: Final[EntropySamplingParams] = EntropySamplingParams(
    dynamic_windows_min_file_size=2 * 1024 * 1024,  # 2MB
    dynamic_windows_max_file_size=100 * 1024 * 1024,
    huge_windows_file_size=256 * 1024 * 1024,
    base_sample_windows=3,
    dynamic_windows_min=4,
    dynamic_windows_max=20,
    huge_windows_max=40,
    target_window_size=_entropy_env_int("TRASH_COMPACTOR_ENTROPY_SAMPLE_WINDOW_SIZE", 16 * 1024),
)

# Backwards-compatible module-level constants
ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE: Final[int] = ENTROPY_SAMPLING_PARAMS.dynamic_windows_min_file_size
ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE: Final[int] = ENTROPY_SAMPLING_PARAMS.dynamic_windows_max_file_size
ENTROPY_HUGE_WINDOWS_FILE_SIZE: Final[int] = ENTROPY_SAMPLING_PARAMS.huge_windows_file_size
ENTROPY_BASE_SAMPLE_WINDOWS: Final[int] = ENTROPY_SAMPLING_PARAMS.base_sample_windows
ENTROPY_DYNAMIC_WINDOWS_MIN: Final[int] = ENTROPY_SAMPLING_PARAMS.dynamic_windows_min
ENTROPY_DYNAMIC_WINDOWS_MAX: Final[int] = ENTROPY_SAMPLING_PARAMS.dynamic_windows_max
ENTROPY_HUGE_WINDOWS_MAX: Final[int] = ENTROPY_SAMPLING_PARAMS.huge_windows_max
ENTROPY_TARGET_WINDOW_SIZE: Final[int] = ENTROPY_SAMPLING_PARAMS.target_window_size

ENTROPY_MAX_FILE_BUDGET: Final[int] = ENTROPY_DYNAMIC_WINDOWS_MAX * ENTROPY_TARGET_WINDOW_SIZE


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


BENCHMARK_DURATION_LIMIT: Final[float] = 0.25
BENCHMARK_WORKLOAD_ITERATIONS: Final[int] = 125_000

from .file_utils import DEFAULT_EXCLUDE_DIRECTORIES


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
    'LZX': 0.90,
}