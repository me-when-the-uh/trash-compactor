import logging
import os
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from ..config import COMPRESSION_ALGORITHMS, savings_from_entropy
from ..file_utils import CompressionDecision, should_compress_file
from ..skip_logic import append_directory_skip_record, evaluate_entropy_directory, maybe_skip_directory
from ..stats import CompressionStats, DirectorySkipRecord
from ..timer import PerformanceMonitor


def iter_files(
    root: Path,
    stats: CompressionStats,
    verbosity: int,
    min_savings_percent: float,
    collect_entropy: bool,
) -> Iterator[Path]:
    skip_root = maybe_skip_directory(
        root,
        root,
        stats,
        collect_entropy,
        min_savings_percent,
        verbosity,
    ).skip
    if skip_root:
        return

    for current_root, dirnames, files in os.walk(root):
        current_base = Path(current_root)

        new_dirnames = []
        for name in dirnames:
            candidate = current_base / name
            decision = maybe_skip_directory(
                candidate,
                root,
                stats,
                collect_entropy,
                min_savings_percent,
                verbosity,
            )
            if decision.skip:
                continue
            new_dirnames.append(name)

        dirnames[:] = new_dirnames

        for name in files:
            yield current_base / name


def plan_compression(
    files: Iterable[Path],
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
    candidates = []
    with monitor.time_file_scan():
        processed = 0
        for file_path in files:
            processed += 1
            try:
                decision = should_compress_file(file_path, thorough_check)
                file_size = file_path.stat().st_size
                if file_observer:
                    file_observer(file_path, file_size, decision)
                stats.total_original_size += file_size

                if decision.should_compress:
                    algorithm = COMPRESSION_ALGORITHMS[get_size_category(file_size)]
                    candidates.append((file_path, file_size, algorithm))
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
                        already_compressed="already compressed" in reason.lower(),
                        category=category,
                    )
                    logging.debug("Skipping %s: %s", file_path, reason)
                    if progress_callback:
                        progress_callback(file_path, processed, False, reason)
            except OSError as exc:
                stats.errors.append(f"Error processing {file_path}: {exc}")
                try:
                    file_size_fallback = file_path.stat().st_size
                except OSError:
                    file_size_fallback = 0
                stats.record_file_skip(
                    file_path,
                    f"Error processing file: {exc}",
                    file_size_fallback,
                    file_size_fallback,
                    category='error',
                )
                logging.error("Error processing %s: %s", file_path, exc)
                if progress_callback:
                    progress_callback(file_path, processed, False, f"Error processing file: {exc}")
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

    for directory in sorted(directories, key=lambda item: (len(item.parts), str(item).casefold())):
        if _has_skipped_ancestor(directory, base_dir, skipped_directories):
            continue

        record = evaluate_entropy_directory(directory, base_dir, min_savings_percent, verbosity)
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

