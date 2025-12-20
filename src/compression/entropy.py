import heapq
import logging
import math
import os
import random
import zlib
from collections import Counter, deque
from pathlib import Path
from typing import Optional, Sequence


def shannon_entropy(sample: bytes) -> float:
    if not sample:
        return 0.0
    total = len(sample)
    frequencies = Counter(sample)
    entropy = 0.0
    for count in frequencies.values():
        probability = count / total
        entropy -= probability * math.log2(probability)
    return entropy


MAX_SAMPLE_WINDOWS = 3
TARGET_WINDOW_SIZE = 16 * 1024


def sample_directory_entropy(
    path: Path,
    max_files: int = 50,
    chunk_size: int = 65536,
    max_bytes: int = 4 * 1024 * 1024,
    *,
    skip_root_files: bool = False,
    include_subdirectories: bool = True,
) -> tuple[Optional[float], int, int]:
    if max_files <= 0 or max_bytes <= 0:
        return None, 0, 0

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
    total_budget = max_bytes

    for file_path in files:
        remaining = total_budget - sampled_bytes
        if remaining <= 0:
            break

        per_file_budget = min(chunk_size, remaining)
        if per_file_budget <= 0:
            break

        file_entropy, file_bytes = _sample_file_entropy(
            file_path,
            byte_budget=per_file_budget,
        )
        if file_bytes == 0:
            continue

        sampled_files += 1
        sampled_bytes += file_bytes
        weighted_entropy += file_entropy

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
        return None, sampled_files, sampled_bytes

    average_entropy = weighted_entropy / sampled_bytes
    return average_entropy, sampled_files, sampled_bytes


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
                entries = list(it)
        except OSError as exc:
            logging.debug("Unable to inspect %s for entropy: %s", current, exc)
            continue

        for entry in entries:
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
            while u == 0:  # Avoid log(0)
                u = random.random()
            
            key = math.log(u) / file_size
            file_path = Path(entry.path)

            if len(reservoir) < max_files:
                heapq.heappush(reservoir, (key, file_path))
            elif key > reservoir[0][0]:
                heapq.heapreplace(reservoir, (key, file_path))

    return [path for _, path in reservoir], root_files_skipped


def _sample_file_entropy(path: Path, *, byte_budget: int) -> tuple[float, int]:
    if byte_budget <= 0:
        return 0.0, 0

    try:
        file_size = path.stat().st_size
    except OSError as exc:
        logging.debug("Unable to stat %s for entropy sampling: %s", path, exc)
        return 0.0, 0

    if file_size == 0:
        return 0.0, 0

    window_size = _derive_window_size(byte_budget)
    windows = _plan_sample_windows(file_size, window_size)
    if not windows:
        return 0.0, 0

    weighted_entropy = 0.0
    sampled_bytes = 0

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

                entropy = _compression_probe_entropy(data)
                chunk_len = len(data)
                weighted_entropy += entropy * chunk_len
                sampled_bytes += chunk_len
    except OSError as exc:
        logging.debug("Unable to sample %s for entropy: %s", path, exc)
        return 0.0, 0

    return weighted_entropy, sampled_bytes


def _derive_window_size(byte_budget: int) -> int:
    if byte_budget <= 0:
        return 0
    window = min(TARGET_WINDOW_SIZE, byte_budget)
    if window * MAX_SAMPLE_WINDOWS > byte_budget and byte_budget >= MAX_SAMPLE_WINDOWS:
        window = byte_budget // MAX_SAMPLE_WINDOWS
    return max(1, window)


def _plan_sample_windows(file_size: int, window_size: int) -> Sequence[tuple[int, int]]:
    if file_size <= 0 or window_size <= 0:
        return []

    if file_size <= window_size:
        return [(0, file_size)]

    windows: list[tuple[int, int]] = []
    head = (0, window_size)
    windows.append(head)

    if file_size <= 2 * window_size:
        tail_offset = max(0, file_size - window_size)
        windows.append((tail_offset, window_size))
    else:
        mid_offset = max(0, (file_size // 2) - window_size // 2)
        tail_offset = max(0, file_size - window_size)
        windows.extend([(mid_offset, window_size), (tail_offset, window_size)])

    deduped: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for offset, length in windows[:MAX_SAMPLE_WINDOWS]:
        clamped_offset = max(0, min(offset, file_size - length))
        clamped_length = min(length, file_size - clamped_offset)
        if clamped_length <= 0:
            continue
        key = (clamped_offset, clamped_length)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)
    return deduped


def _compression_probe_entropy(sample: bytes) -> float:
    if not sample:
        return 0.0
    try:
        compressed = zlib.compress(sample, level=2)
    except Exception as exc:  # pragma: no cover - extremely rare
        logging.debug("Falling back to Shannon entropy for probe failure: %s", exc)
        return shannon_entropy(sample)

    compressed_length = len(compressed) or 1
    ratio = compressed_length / len(sample)
    estimated_entropy = ratio * 8.0
    return max(0.0, min(8.0, estimated_entropy))