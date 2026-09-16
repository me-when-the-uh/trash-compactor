import logging
import zlib
import lz4.block
from pathlib import Path
from typing import Optional

from ..config import (
    ENTROPY_SAMPLING_PARAMS,
)

_P = ENTROPY_SAMPLING_PARAMS
ENTROPY_BASE_SAMPLE_WINDOWS = _P.base_sample_windows
ENTROPY_DYNAMIC_WINDOWS_MAX = _P.dynamic_windows_max
ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE = _P.dynamic_windows_max_file_size
ENTROPY_DYNAMIC_WINDOWS_MIN = _P.dynamic_windows_min
ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE = _P.dynamic_windows_min_file_size
ENTROPY_HUGE_WINDOWS_FILE_SIZE = _P.huge_windows_file_size
ENTROPY_HUGE_WINDOWS_MAX = _P.huge_windows_max
ENTROPY_TARGET_WINDOW_SIZE = _P.target_window_size

_LZ4_INCOMPRESSIBLE_THRESHOLD = 0.95


def get_sample_window_count(file_size: int) -> int:
    if file_size <= ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE:
        return ENTROPY_BASE_SAMPLE_WINDOWS
    if file_size >= ENTROPY_HUGE_WINDOWS_FILE_SIZE:
        return ENTROPY_HUGE_WINDOWS_MAX

    if file_size >= ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE:
        ratio = (file_size - ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE) / (ENTROPY_HUGE_WINDOWS_FILE_SIZE - ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE)
        return int(ENTROPY_DYNAMIC_WINDOWS_MAX + ratio * (ENTROPY_HUGE_WINDOWS_MAX - ENTROPY_DYNAMIC_WINDOWS_MAX))

    ratio = (file_size - ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE) / (ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE - ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE)
    return int(ENTROPY_DYNAMIC_WINDOWS_MIN + ratio * (ENTROPY_DYNAMIC_WINDOWS_MAX - ENTROPY_DYNAMIC_WINDOWS_MIN))


def get_file_probe_budget(file_size: int) -> int:
    """
    Total bytes to sample from one file: one target window per window slot.
    """
    return get_sample_window_count(file_size) * ENTROPY_TARGET_WINDOW_SIZE


def sample_file_entropy(
    path: Path,
    *,
    byte_budget: int,
    file_size: Optional[int] = None,
) -> tuple[float, int, bool]:
    if byte_budget <= 0:
        return 0.0, 0, False

    if file_size is None:
        try:
            file_size = path.stat().st_size
        except OSError as exc:
            logging.debug("Unable to stat %s for entropy sampling: %s", path, exc)
            return 0.0, 0, False

    if file_size == 0:
        return 0.0, 0, False

    num_windows = get_sample_window_count(file_size)
    window_size = _derive_window_size(byte_budget, num_windows)
    windows = _plan_sample_windows(file_size, window_size, num_windows)
    if not windows:
        return 0.0, 0, False

    weighted_entropy = 0.0
    sampled_bytes = 0
    sampled_chunks = 0
    lz4_shortcircuit_chunks = 0

    try:
        with path.open('rb') as stream:
            for offset, length in windows:
                remaining = byte_budget - sampled_bytes
                if remaining <= 0:
                    break

                read_len = min(length, remaining)
                if read_len <= 0:
                    break

                stream.seek(offset)
                data = stream.read(read_len)
                if not data:
                    continue

                entropy, lz4_shortcircuit = _compression_probe_entropy(data)
                chunk_len = len(data)
                weighted_entropy += entropy * chunk_len
                sampled_bytes += chunk_len
                sampled_chunks += 1
                if lz4_shortcircuit:
                    lz4_shortcircuit_chunks += 1
    except OSError as exc:
        logging.debug("Unable to sample %s for entropy: %s", path, exc)
        return 0.0, 0, False

    lz4_certain_incompressible = sampled_chunks > 0 and lz4_shortcircuit_chunks == sampled_chunks
    return weighted_entropy, sampled_bytes, lz4_certain_incompressible


def _derive_window_size(byte_budget: int, num_windows: int) -> int:
    if byte_budget <= 0:
        return 0
    window = min(ENTROPY_TARGET_WINDOW_SIZE, byte_budget)
    if window * num_windows > byte_budget and byte_budget >= num_windows:
        window = byte_budget // num_windows
    return max(1, window)


def _plan_sample_windows(file_size: int, window_size: int, num_windows: int) -> list[tuple[int, int]]:
    if file_size <= 0 or window_size <= 0 or num_windows <= 0:
        return []

    if file_size <= window_size:
        return [(0, file_size)]

    raw_windows = []

    if num_windows == 3 and ENTROPY_BASE_SAMPLE_WINDOWS == 3:
        p10 = int(file_size * 0.10)
        p45 = int(file_size * 0.45)
        p80 = int(file_size * 0.80)

        w1_start = p10
        w2_start = max(0, p45 - (window_size // 2))
        w3_start = max(0, p80 - window_size)

        for start in (w1_start, w2_start, w3_start):
            start = min(start, file_size)
            end = min(start + window_size, file_size)
            if end > start:
                raw_windows.append((start, end))
    else:
        # Spread over [10%, 90%] of the file the way the three-window case
        # does. Starting at offset 0 reads format headers and under-reports
        # entropy for exactly the formats worth compressing.
        max_start = max(0, file_size - window_size)
        lo = min(int(file_size * 0.10), max_start)
        hi = max(min(int(file_size * 0.90), max_start), lo)
        step = (hi - lo) / (num_windows - 1) if num_windows > 1 else 0.0
        for i in range(num_windows):
            start = int(lo + i * step)
            end = min(start + window_size, file_size)
            if end > start:
                raw_windows.append((start, end))

    if not raw_windows:
        return []

    # Merge overlaps
    raw_windows.sort()
    merged = []
    current_start, current_end = raw_windows[0]

    for next_start, next_end in raw_windows[1:]:
        if next_start <= current_end:
            # Overlap or adjacent, extend current
            current_end = max(current_end, next_end)
        else:
            merged.append((current_start, current_end - current_start))
            current_start, current_end = next_start, next_end
    
    merged.append((current_start, current_end - current_start))
    
    return merged


def _compression_probe_entropy(sample: bytes) -> tuple[float, bool]:
    if not sample:
        return 0.0, False

    # LZ4 is ~5-10x faster than zlib.
    # if LZ4 can't compress the sample meaningfully, zlib won't either,
    # so both algorithms agree on incompressible data w/ entropy ≈ 8.0
    try:
        lz4_len = len(lz4.block.compress(sample, store_size=False))
        if lz4_len / len(sample) >= _LZ4_INCOMPRESSIBLE_THRESHOLD:
            return 8.0, True
    except Exception:
        pass  # Fall through to zlib

    # zlib matches NTFS compression algorithms,
    # giving accurate ratio estimates for LZX/XPRESS
    try:
        compressed = zlib.compress(sample, level=2)
    except Exception as exc:
        logging.debug("Probe failure: %s", exc)
        return 8.0, False

    compressed_length = len(compressed) or 1
    ratio = compressed_length / len(sample)
    return max(0.0, min(8.0, ratio * 8.0)), False


def _entropy_params():
    """One fast_walk.EntropyParams per process; the PyO3 object is read-only."""
    global _ENTROPY_PARAMS
    if _ENTROPY_PARAMS is None:
        import fast_walk

        _ENTROPY_PARAMS = fast_walk.EntropyParams(
            dynamic_windows_min_file_size=_P.dynamic_windows_min_file_size,
            dynamic_windows_max_file_size=_P.dynamic_windows_max_file_size,
            huge_windows_file_size=_P.huge_windows_file_size,
            base_sample_windows=_P.base_sample_windows,
            dynamic_windows_min=_P.dynamic_windows_min,
            dynamic_windows_max=_P.dynamic_windows_max,
            huge_windows_max=_P.huge_windows_max,
            target_window_size=_P.target_window_size,
        )
    return _ENTROPY_PARAMS


_ENTROPY_PARAMS = None