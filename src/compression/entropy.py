import heapq
import logging
import math
import os
import random
import zlib
import lz4.block
from collections import Counter, deque
from pathlib import Path
from typing import Optional, Sequence

from ..config import (
    ENTROPY_BASE_SAMPLE_WINDOWS,
    ENTROPY_DYNAMIC_WINDOWS_MAX,
    ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE,
    ENTROPY_DYNAMIC_WINDOWS_MIN,
    ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE,
    ENTROPY_MAX_FILE_BUDGET,
    ENTROPY_TARGET_WINDOW_SIZE,
)

_LZ4_INCOMPRESSIBLE_THRESHOLD = 0.95


def sample_directory_entropy(
    path: Path,
    max_files: int = 45,
    chunk_size: Optional[int] = None,
    max_bytes: int = 4 * 1024 * 1024,
    *,
    skip_root_files: bool = False,
    include_subdirectories: bool = True,
) -> tuple[Optional[float], int, int, int]:
    if chunk_size is None:
        chunk_size = ENTROPY_MAX_FILE_BUDGET
    if max_files <= 0 or max_bytes <= 0:
        return None, 0, 0, 0

    files, root_files_skipped = _reservoir_sample_files(
        path,
        max_files=max_files,
        include_subdirectories=include_subdirectories,
        skip_root_files=skip_root_files,
    )

    if not files and skip_root_files and root_files_skipped:
        return sample_directory_entropy(
            path,
            max_files=max_files,
            chunk_size=chunk_size,
            max_bytes=max_bytes,
            skip_root_files=False,
            include_subdirectories=include_subdirectories,
        )

    random.shuffle(files)

    sampled_files = 0
    sampled_bytes = 0
    weighted_entropy = 0.0
    lz4_certain_incompressible_files = 0
    total_budget = max_bytes

    for file_path in files:
        remaining = total_budget - sampled_bytes
        if remaining <= 0:
            break

        per_file_budget = min(chunk_size, remaining)
        if per_file_budget <= 0:
            break

        file_entropy, file_bytes, lz4_certain = sample_file_entropy(
            file_path,
            byte_budget=per_file_budget,
        )
        if file_bytes == 0:
            continue

        sampled_files += 1
        sampled_bytes += file_bytes
        weighted_entropy += file_entropy
        if lz4_certain:
            lz4_certain_incompressible_files += 1

        if sampled_bytes >= total_budget:
            break

    if sampled_bytes == 0:
        if skip_root_files and root_files_skipped:
            return sample_directory_entropy(
                path,
                max_files=max_files,
                chunk_size=chunk_size,
                max_bytes=max_bytes,
                skip_root_files=False,
                include_subdirectories=include_subdirectories,
            )
        return None, sampled_files, sampled_bytes, lz4_certain_incompressible_files

    average_entropy = weighted_entropy / sampled_bytes
    return average_entropy, sampled_files, sampled_bytes, lz4_certain_incompressible_files


def _reservoir_sample_files(
    root: Path,
    *,
    max_files: int,
    include_subdirectories: bool,
    skip_root_files: bool,
) -> tuple[list[Path], bool]:
    if max_files <= 0:
        return [], False

    # Use a min-heap to keep the k items with the largest keys
    # Heap elements: (key, path)
    reservoir: list[tuple[float, Path]] = []
    
    pending = deque([root])
    root_files_skipped = False

    while pending:
        current = pending.popleft()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if include_subdirectories:
                                pending.append(Path(entry.path))
                            continue

                        if not entry.is_file(follow_symlinks=False):
                            continue

                        if skip_root_files and current == root:
                            root_files_skipped = True
                            continue

                        try:
                            file_size = entry.stat().st_size
                        except OSError:
                            continue

                        if file_size <= 0:
                            continue

                        # Weighted reservoir sampling (Efraimidis-Spirakis)
                        # Key = u^(1/w) -> log(Key) = log(u) / w
                        # the intent is to keep items with largest keys.
                        # u is random in (0, 1]
                        u = random.random()
                        while u == 0:
                            u = random.random()

                        key = math.log(u) / file_size
                        file_path = Path(entry.path)

                        if len(reservoir) < max_files:
                            heapq.heappush(reservoir, (key, file_path))
                        elif key > reservoir[0][0]:
                            heapq.heapreplace(reservoir, (key, file_path))
                    except OSError:
                        continue
        except OSError as exc:
            logging.debug("Unable to inspect %s for entropy: %s", current, exc)
            continue

    return [path for _, path in reservoir], root_files_skipped


def get_sample_window_count(file_size: int) -> int:
    if file_size <= ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE:
        return ENTROPY_BASE_SAMPLE_WINDOWS
    if file_size >= ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE:
        return ENTROPY_DYNAMIC_WINDOWS_MAX
    
    ratio = (file_size - ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE) / (ENTROPY_DYNAMIC_WINDOWS_MAX_FILE_SIZE - ENTROPY_DYNAMIC_WINDOWS_MIN_FILE_SIZE)
    return int(ENTROPY_DYNAMIC_WINDOWS_MIN + ratio * (ENTROPY_DYNAMIC_WINDOWS_MAX - ENTROPY_DYNAMIC_WINDOWS_MIN))


def sample_file_entropy(path: Path, *, byte_budget: int) -> tuple[float, int, bool]:
    if byte_budget <= 0:
        return 0.0, 0, False

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
        max_start = max(0, file_size - window_size)
        step = max_start / (num_windows - 1) if num_windows > 1 else 0.0
        for i in range(num_windows):
            start = int(i * step)
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

    # zlib (DEFLATE = LZ77 + Huffman) matches NTFS compression algorithms,
    # giving accurate ratio estimates for LZX/XPRESS
    try:
        compressed = zlib.compress(sample, level=2)
    except Exception as exc:
        logging.debug("Probe failure: %s", exc)
        return 8.0, False

    compressed_length = len(compressed) or 1
    ratio = compressed_length / len(sample)
    return max(0.0, min(8.0, ratio * 8.0)), False