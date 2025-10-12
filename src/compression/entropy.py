import logging
import math
from collections import Counter, deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Iterator, Optional, Sequence


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


def sample_directory_entropy(
    path: Path,
    max_files: int = 50,
    chunk_size: int = 65536,
    max_bytes: int = 4 * 1024 * 1024,
    *,
    skip_root_files: bool = False,
    include_subdirectories: bool = True,
) -> tuple[Optional[float], int, int]:
    pending = deque([path])
    root = path
    sampled_files = 0
    sampled_bytes = 0
    weighted_entropy = 0.0
    root_files_skipped = False

    while pending and sampled_files < max_files and sampled_bytes < max_bytes:
        current = pending.popleft()
        try:
            entries = list(current.iterdir())
        except OSError as exc:
            logging.debug("Unable to inspect %s for entropy: %s", current, exc)
            continue

        for entry in entries:
            if entry.is_dir():
                if include_subdirectories:
                    pending.append(entry)
                continue

            if skip_root_files and current == root:
                root_files_skipped = True
                continue

            try:
                with entry.open('rb') as stream:
                    data = stream.read(chunk_size)
            except OSError as exc:
                logging.debug("Unable to sample %s for entropy: %s", entry, exc)
                continue

            if not data:
                continue

            entropy = shannon_entropy(data)
            length = len(data)
            sampled_files += 1
            sampled_bytes += length
            weighted_entropy += entropy * length

            if sampled_files >= max_files or sampled_bytes >= max_bytes:
                break

        if sampled_files >= max_files or sampled_bytes >= max_bytes:
            break

    if sampled_bytes == 0 and skip_root_files and root_files_skipped:
        return sample_directory_entropy(
            path,
            max_files=max_files,
            chunk_size=chunk_size,
            max_bytes=max_bytes,
            skip_root_files=False,
            include_subdirectories=include_subdirectories,
        )

    if sampled_bytes == 0:
        return None, sampled_files, sampled_bytes

    average_entropy = weighted_entropy / sampled_bytes
    return average_entropy, sampled_files, sampled_bytes