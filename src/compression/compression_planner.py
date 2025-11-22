import logging
import os
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from ..config import COMPRESSION_ALGORITHMS, savings_from_entropy
from ..file_utils import CompressionDecision, should_compress_file
from ..skip_logic import append_directory_skip_record, evaluate_entropy_directory, maybe_skip_directory, sample_directory_entropy
from ..stats import CompressionStats, DirectorySkipRecord
from ..timer import PerformanceMonitor
from ..workers import entropy_worker_count


def iter_files(
    root: Path,
    stats: CompressionStats,
    verbosity: int,
    min_savings_percent: float,
    collect_entropy: bool,
    skipped_file_callback: Optional[Callable[[Path], None]] = None,
) -> Iterator[os.DirEntry]:
    skip_root = maybe_skip_directory(
        root,
        root,
        stats,
        collect_entropy,
        min_savings_percent,
        verbosity,
    ).skip
    if skip_root:
        if skipped_file_callback:
            _traverse_skipped(root, skipped_file_callback)
        return

    stack = [root]
    
    while stack:
        current_dir = stack.pop()
        
        try:
            with os.scandir(current_dir) as it:
                entries = list(it)
        except (OSError, PermissionError):
            continue

        dirs = []
        files = []
        
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                dirs.append(entry)
            elif entry.is_file(follow_symlinks=False):
                files.append(entry)
        
        for entry in files:
            yield entry

        valid_dirs = []
        for entry in dirs:
            candidate = Path(entry.path)
            decision = maybe_skip_directory(
                candidate,
                root,
                stats,
                collect_entropy,
                min_savings_percent,
                verbosity,
            )
            if decision.skip:
                if skipped_file_callback:
                    _traverse_skipped(candidate, skipped_file_callback)
                continue
            valid_dirs.append(candidate)
        stack.extend(reversed(valid_dirs))


def _traverse_skipped(root: Path, callback: Callable[[Path], None]) -> None:
    for current_root, _, files in os.walk(root):
        current_base = Path(current_root)
        for name in files:
            callback(current_base / name)


@dataclass(frozen=True)
class _ScanPayload:
    index: int
    path: Path
    file_size: int
    decision: Optional[CompressionDecision]
    error: Optional[str] = None


def _scan_single(index: int, entry: os.DirEntry, thorough_check: bool) -> _ScanPayload:
    file_path = Path(entry.path)
    try:
        file_size = entry.stat().st_size
    except OSError as exc:
        return _ScanPayload(index, file_path, 0, None, f"Error processing {file_path}: {exc}")

    decision = should_compress_file(file_path, thorough_check, file_size=file_size)
    return _ScanPayload(index, file_path, file_size, decision)


def _iter_scanned_files(files: Iterable[os.DirEntry], thorough_check: bool) -> Iterator[_ScanPayload]:
    # Scanning is metadata-heavy and extremely fast with os.DirEntry.
    # Using threads introduces GIL contention and overhead that outweighs the
    # benefits of parallelism for such lightweight tasks.
    # Single-threaded execution is forced for the scanning phase to maximize throughput.
    for index, entry in enumerate(files):
        yield _scan_single(index, entry, thorough_check)


def plan_compression(
    files: Iterable[os.DirEntry],
    stats: CompressionStats,
    monitor: PerformanceMonitor,
    thorough_check: bool,
    *,
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
    progress_callback: Optional[Callable[[Path, int, bool, Optional[str]], None]] = None,
    file_observer: Optional[Callable[[Path, int, CompressionDecision], None]] = None,
    apply_entropy_filter: bool = True,
) -> list[tuple[Path, int, str]]:
    ordered_candidates: list[tuple[int, Path, int, str]] = []
    with monitor.time_file_scan():
        processed = 0
        for payload in _iter_scanned_files(files, thorough_check):
            processed += 1
            file_path = payload.path
            decision = payload.decision

            if decision is None:
                reason = payload.error or "Error processing file"
                stats.errors.append(reason)
                stats.record_file_skip(
                    file_path,
                    reason,
                    payload.file_size,
                    payload.file_size,
                    category='error',
                )
                logging.error(reason)
                if progress_callback:
                    progress_callback(file_path, processed, False, reason)
                continue

            file_size = payload.file_size
            if file_observer:
                file_observer(file_path, file_size, decision)
            stats.total_original_size += file_size

            if decision.should_compress:
                algorithm = COMPRESSION_ALGORITHMS[get_size_category(file_size)]
                ordered_candidates.append((payload.index, file_path, file_size, algorithm))
                if progress_callback:
                    progress_callback(file_path, processed, True, None)
            else:
                reason = decision.reason
                resolved_size = decision.size_hint or file_size
                category = None
                lowered = reason.lower()
                if 'extension' in lowered:
                    category = 'extension'
                stats.record_file_skip(
                    file_path,
                    reason,
                    resolved_size,
                    file_size,
                    already_compressed="already compressed" in lowered,
                    category=category,
                )
                logging.debug("Skipping %s: %s", file_path, reason)
                if progress_callback:
                    progress_callback(file_path, processed, False, reason)

    ordered_candidates.sort(key=lambda item: item[0])
    candidates = [(path, file_size, algorithm) for _, path, file_size, algorithm in ordered_candidates]
    if apply_entropy_filter:
        candidates = _filter_high_entropy_directories(
            candidates,
            base_dir=base_dir,
            stats=stats,
            min_savings_percent=min_savings_percent,
            verbosity=verbosity,
        )
    return candidates


def get_size_category(file_size: int) -> str:
    from ..config import SIZE_THRESHOLDS
    from bisect import bisect_right

    breaks, labels = zip(*SIZE_THRESHOLDS)
    index = bisect_right(breaks, file_size)
    return labels[index] if index < len(labels) else 'large'


def _filter_high_entropy_directories(
    candidates: list[tuple[Path, int, str]],
    *,
    base_dir: Path,
    stats: CompressionStats,
    min_savings_percent: float,
    verbosity: int,
) -> list[tuple[Path, int, str]]:
    if not candidates or min_savings_percent <= 0:
        return candidates

    directories = {path.parent for path, _, _ in candidates}
    directories.add(base_dir)

    root_skip_record: Optional[DirectorySkipRecord] = None
    if any(path.parent == base_dir for path, _, _ in candidates):
        average_entropy, sampled_files, sampled_bytes = sample_directory_entropy(
            base_dir,
            include_subdirectories=False,
        )
        if average_entropy is not None and sampled_files > 0 and sampled_bytes >= 1024:
            estimated_savings = savings_from_entropy(average_entropy)
            logging.debug(
                "Root entropy sample for %s: %.2f bits/byte (~%.1f%% savings) across %s files (%s bytes)",
                base_dir,
                average_entropy,
                estimated_savings,
                sampled_files,
                sampled_bytes,
            )
            if estimated_savings < min_savings_percent:
                reason = f"High entropy (est. {estimated_savings:.1f}% savings)"
                root_skip_record = DirectorySkipRecord(
                    path=str(base_dir),
                    relative_path='.',
                    reason=reason,
                    category='high_entropy',
                    average_entropy=average_entropy,
                    estimated_savings=estimated_savings,
                    sampled_files=sampled_files,
                    sampled_bytes=sampled_bytes,
                )
                append_directory_skip_record(stats, root_skip_record)
                if verbosity >= 2:
                    logging.info(
                        "Skipping root-level files; estimated savings %.1f%% is below threshold %.1f%%",
                        estimated_savings,
                        min_savings_percent,
                    )

    skipped_directories: dict[Path, DirectorySkipRecord] = {}
    entropy_records = _evaluate_directories_parallel(
        (directory for directory in directories if directory != base_dir),
        base_dir,
        min_savings_percent,
        verbosity,
    )

    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item).casefold())):
        if _has_skipped_ancestor(directory, base_dir, skipped_directories):
            continue

        record = entropy_records.get(directory)
        if record:
            append_directory_skip_record(stats, record)
            skipped_directories[directory] = record

    if not skipped_directories:
        return candidates

    filtered: list[tuple[Path, int, str]] = []
    for path, file_size, algorithm in candidates:
        if root_skip_record is not None and path.parent == base_dir:
            stats.record_file_skip(
                path,
                root_skip_record.reason,
                file_size,
                file_size,
                category=root_skip_record.category,
            )
            logging.debug("Skipping %s due to %s", path, root_skip_record.reason)
            continue
        skip_record = _locate_skip_record(path.parent, base_dir, skipped_directories)
        if skip_record is not None:
            stats.record_file_skip(
                path,
                skip_record.reason,
                file_size,
                file_size,
                category=skip_record.category,
            )
            logging.debug("Skipping %s due to %s", path, skip_record.reason)
            continue
        filtered.append((path, file_size, algorithm))

    return filtered


def _has_skipped_ancestor(
    directory: Path,
    base_dir: Path,
    skipped: dict[Path, DirectorySkipRecord],
) -> bool:
    for ancestor in _ancestors_including_base(directory, base_dir):
        record = skipped.get(ancestor)
        if record is None:
            continue
        if ancestor == base_dir and directory != base_dir:
            continue
        return True
    return False


def _locate_skip_record(
    directory: Path,
    base_dir: Path,
    skipped: dict[Path, DirectorySkipRecord],
) -> Optional[DirectorySkipRecord]:
    for ancestor in _ancestors_including_base(directory, base_dir):
        record = skipped.get(ancestor)
        if record is None:
            continue
        if ancestor == base_dir and directory != base_dir:
            continue
        return record
    return None


def _ancestors_including_base(path: Path, base_dir: Path) -> list[Path]:
    ancestors: list[Path] = []
    current = path
    while True:
        ancestors.append(current)
        if current == base_dir:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return ancestors


def _evaluate_directories_parallel(
    directories: Iterable[Path],
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
) -> dict[Path, DirectorySkipRecord]:
    directory_list = list(directories)
    if not directory_list:
        return {}

    worker_count = entropy_worker_count()
    if worker_count <= 1 or len(directory_list) == 1:
        results: dict[Path, DirectorySkipRecord] = {}
        for directory in directory_list:
            record = evaluate_entropy_directory(directory, base_dir, min_savings_percent, verbosity)
            if record:
                results[directory] = record
        return results

    results: dict[Path, DirectorySkipRecord] = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                evaluate_entropy_directory,
                directory,
                base_dir,
                min_savings_percent,
                verbosity,
            ): directory
            for directory in directory_list
        }

        for future in as_completed(future_map):
            directory = future_map[future]
            record = future.result()
            if record:
                results[directory] = record

    return results

