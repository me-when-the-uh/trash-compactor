import logging
import os
from pathlib import Path
from typing import Callable, Iterable, Optional

from ..i18n import _
from ..config import ENTROPY_MAX_BYTES, ENTROPY_MAX_FILE_BUDGET, ENTROPY_MAX_FILES, savings_from_entropy
from ..skip_logic import (
    append_directory_skip_record,
    entropy_records_from_probe,
    evaluate_entropy_directory,
    get_incompressible_cache,
    sample_directory_entropy,
)
from ..stats import CompressionStats, DirectorySkipRecord, EntropySampleRecord, SkipBulkLedger
from ..timer import PerformanceMonitor
from ..workers import entropy_worker_count
from .entropy import sample_file_entropy
from .file_scan import (
    CAT_ALREADY_COMPRESSED,
    CAT_DEBUG_EXT,
    CAT_ELIGIBLE,
    CAT_ERROR,
    CAT_EXTENSION,
    CountingDirEntryIter,
    iter_files,
)  # noqa: F401

PlanEntry = tuple[str, int, str]


def _format_size(num_bytes: int) -> str:
    try:
        n = int(num_bytes)
    except Exception:
        return f"{num_bytes} B"
    if n < 1024:
        return f"{n} B"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if n < 1024.0:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PB"


def plan_compression(
    files: Iterable,
    stats: CompressionStats,
    monitor: PerformanceMonitor,
    *,
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
    progress_callback: Optional[Callable[[str, int, bool, Optional[str], int], None]] = None,
    apply_entropy_filter: bool = True,
    entropy_progress_callback: Optional[Callable[[Path, int, int], None]] = None,
    debug_scan_all: bool = False,
) -> list[PlanEntry]:
    candidates: list[PlanEntry] = []
    with monitor.time_file_scan():
        processed = 0
        bulk_skips = SkipBulkLedger()
        use_bulk_skips = verbosity < 4
        for path, size, _attributes, algo, category, hint in files:
            processed += 1

            if category == CAT_ERROR:
                reason = _("Error processing {file_path}").format(file_path=path)
                stats.errors.append(reason)
                if use_bulk_skips:
                    bulk_skips.add("error", hint, size)
                else:
                    stats.record_file_skip_counters(hint, size, category="error")
                logging.error(reason)
                if progress_callback:
                    progress_callback(path, processed, False, reason, size)
                continue

            stats.total_original_size += size
            stats.total_on_disk_size += hint

            if category == CAT_ELIGIBLE or category == CAT_DEBUG_EXT:
                candidates.append((path, size, algo))
                if category == CAT_DEBUG_EXT:
                    _debug_extension_probe(path, size, min_savings_percent)
                if progress_callback:
                    progress_callback(path, processed, True, None, size)
            elif category == CAT_ALREADY_COMPRESSED:
                if use_bulk_skips:
                    bulk_skips.add("already_compressed", hint, size)
                else:
                    stats.record_file_skip_counters(hint, size, already_compressed=True, category="already_compressed")
                    logging.debug("Skipping %s: already compressed", path)
                if progress_callback:
                    progress_callback(path, processed, False, _("File is already compressed"), size)
            elif category == CAT_EXTENSION:
                if use_bulk_skips:
                    bulk_skips.add("extension", hint, size)
                else:
                    stats.record_file_skip_counters(hint, size, category="extension")
                if progress_callback:
                    progress_callback(path, processed, False, _("Skipped due to extension"), size)
            else:
                if use_bulk_skips:
                    bulk_skips.add("too_small", hint, size)
                else:
                    stats.record_file_skip_counters(hint, size, category="too_small")
                    logging.debug("Skipping %s: file too small", path)
                if progress_callback:
                    progress_callback(path, processed, False, _("File too small"), size)

        if use_bulk_skips:
            stats.record_bulk_skips(bulk_skips)

    if apply_entropy_filter:
        with monitor.time_entropy_analysis():
            get_incompressible_cache().clear_hash_cache()
            candidates = _filter_high_entropy_directories(
                candidates,
                base_dir=base_dir,
                stats=stats,
                monitor=monitor,
                min_savings_percent=min_savings_percent,
                verbosity=verbosity,
                progress_callback=entropy_progress_callback,
            )
    return candidates


def _debug_extension_probe(path: str, file_size: int, min_savings_percent: float) -> None:
    entropy_sum, sampled_bytes, _ = sample_file_entropy(
        Path(path), byte_budget=ENTROPY_MAX_FILE_BUDGET, file_size=file_size
    )
    if sampled_bytes <= 0:
        return
    savings = savings_from_entropy(entropy_sum / sampled_bytes)
    if savings >= min_savings_percent:
        projected = int(file_size * (1 - savings / 100))
        print(
            f"\n[DEBUG] File {os.path.basename(path)} has potential savings: {savings:.1f}% "
            f"({_format_size(file_size)} -> {_format_size(projected)})"
        )


def _filter_high_entropy_directories(
    candidates: list[PlanEntry],
    *,
    base_dir: Path,
    stats: CompressionStats,
    monitor: Optional[PerformanceMonitor] = None,
    min_savings_percent: float,
    verbosity: int,
    progress_callback: Optional[Callable[[Path, int, int], None]] = None,
) -> list[PlanEntry]:
    if not candidates or min_savings_percent <= 0:
        return candidates

    base_dir_str = os.fspath(base_dir)
    directory_paths: set[str] = {os.path.dirname(path_str) for path_str, _, _ in candidates}
    directory_paths.add(base_dir_str)
    directories = {Path(path_str) for path_str in directory_paths}

    root_skip_record: Optional[DirectorySkipRecord] = None
    if any(os.path.dirname(path_str) == base_dir_str for path_str, _, _ in candidates):
        average_entropy, sampled_files, sampled_bytes, lz4_certain_files = sample_directory_entropy(
            base_dir,
            include_subdirectories=False,
        )
        if monitor and sampled_files > 0:
            monitor.stats.files_analyzed_for_entropy += sampled_files

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

            from ..skip_logic import _relative_to_base
            root_sample = EntropySampleRecord(
                path=str(base_dir),
                relative_path=_relative_to_base(base_dir, base_dir),
                average_entropy=average_entropy,
                estimated_savings=estimated_savings,
                sampled_files=sampled_files,
                sampled_bytes=sampled_bytes,
                lz4_certain_files=lz4_certain_files,
                total_bytes=0,
            )
            stats.entropy_samples.append(root_sample)
            stats.entropy_directories_sampled += 1
            stats.lz4_certain_incompressible_files += lz4_certain_files
            if estimated_savings < min_savings_percent:
                stats.entropy_directories_below_threshold += 1

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

    cache = get_incompressible_cache()
    sorted_directories = sorted(directories, key=lambda item: (len(item.parts), str(item).casefold()))
    for directory in sorted_directories:
        if directory == base_dir:
            continue
        if _has_skipped_ancestor(directory, base_dir, skipped_directories):
            continue
        if not cache.contains(directory):
            continue

        from ..skip_logic import _relative_to_base
        record = DirectorySkipRecord(
            path=str(directory),
            relative_path=_relative_to_base(directory, base_dir),
            reason=_("Cached: High entropy directory"),
            category='high_entropy',
            average_entropy=8.0,
            estimated_savings=0.0,
            sampled_files=0,
            sampled_bytes=0,
        )
        append_directory_skip_record(stats, record)
        skipped_directories[directory] = record

    directories_to_evaluate = [
        directory
        for directory in directories
        if directory != base_dir and not _has_skipped_ancestor(directory, base_dir, skipped_directories)
    ]

    if progress_callback and directories_to_evaluate:
        progress_callback(base_dir, 0, len(directories_to_evaluate), 0)

    entropy_records, sample_records = evaluate_directories_parallel(
        directories_to_evaluate,
        base_dir,
        min_savings_percent,
        verbosity,
        progress_callback=progress_callback,
    )

    if monitor and not progress_callback:
        for record in sample_records:
            monitor.stats.files_analyzed_for_entropy += record.sampled_files

    for record in sample_records:
        stats.entropy_samples.append(record)
        stats.entropy_directories_sampled += 1
        stats.lz4_certain_incompressible_files += record.lz4_certain_files
        if record.estimated_savings < min_savings_percent:
            stats.entropy_directories_below_threshold += 1

    for directory in sorted_directories:
        if _has_skipped_ancestor(directory, base_dir, skipped_directories):
            continue

        record = entropy_records.get(directory)
        if record:
            append_directory_skip_record(stats, record)
            skipped_directories[directory] = record
            cache.add(directory)

    if not skipped_directories and root_skip_record is None:
        return candidates

    filtered: list[PlanEntry] = []
    for path_str, file_size, algorithm in candidates:
        parent_str = os.path.dirname(path_str)
        parent = Path(parent_str)
        if root_skip_record is not None and parent_str == base_dir_str:
            stats.record_file_skip(
                Path(path_str),
                root_skip_record.reason,
                file_size,
                file_size,
                category=root_skip_record.category,
            )
            logging.debug("Skipping %s due to %s", path_str, root_skip_record.reason)
            continue
        skip_record = _locate_skip_record(parent, base_dir, skipped_directories)
        if skip_record is not None:
            stats.record_file_skip(
                Path(path_str),
                skip_record.reason,
                file_size,
                file_size,
                category=skip_record.category,
            )
            logging.debug("Skipping %s due to %s", path_str, skip_record.reason)
            continue
        filtered.append((path_str, file_size, algorithm))

    return filtered


def _has_skipped_ancestor(
    directory: Path,
    base_dir: Path,
    skipped: dict[Path, DirectorySkipRecord],
) -> bool:
    current = directory
    while True:
        record = skipped.get(current)
        if record is not None:
            if current != base_dir or directory == base_dir:
                return True
        if current == base_dir:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def _locate_skip_record(
    directory: Path,
    base_dir: Path,
    skipped: dict[Path, DirectorySkipRecord],
) -> Optional[DirectorySkipRecord]:
    current = directory
    while True:
        record = skipped.get(current)
        if record is not None:
            if current != base_dir or directory == base_dir:
                return record
        if current == base_dir:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def evaluate_directories_parallel(
    directories: Iterable[Path],
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
    progress_callback: Optional[Callable[..., None]] = None,
) -> tuple[dict[Path, DirectorySkipRecord], list[EntropySampleRecord]]:
    directory_list = list(directories)
    if not directory_list:
        return {}, []

    worker_count = entropy_worker_count()
    processed_count = 0
    total_count = len(directory_list)

    def _on_complete(directory: Path, dir_sampled_files: int = 0) -> None:
        nonlocal processed_count
        processed_count += 1
        if progress_callback:
            progress_callback(directory, processed_count, total_count, dir_sampled_files)

    skip_results: dict[Path, DirectorySkipRecord] = {}
    sample_results: list[EntropySampleRecord] = []

    serial_threshold = max(worker_count * 2, 8)
    if worker_count <= 1 or len(directory_list) < serial_threshold:
        for directory in directory_list:
            skip_record, sample_record = evaluate_entropy_directory(directory, base_dir, min_savings_percent, verbosity)
            sampled_files = sample_record.sampled_files if sample_record else 0
            _on_complete(directory, sampled_files)
            if skip_record:
                skip_results[directory] = skip_record
            if sample_record:
                sample_results.append(sample_record)
        return skip_results, sample_results

    import fast_walk

    rust_progress = None
    if progress_callback:

        def _rust_progress(
            directory: str,
            processed: int,
            total: int,
            dir_sampled_files: int = 0,
        ) -> None:
            progress_callback(Path(directory), processed, total, dir_sampled_files)

        rust_progress = _rust_progress

    raw_results = fast_walk.probe_directories_parallel(
        [str(directory) for directory in directory_list],
        ENTROPY_MAX_FILES,
        ENTROPY_MAX_BYTES,
        ENTROPY_MAX_FILE_BUDGET,
        True,
        worker_count,
        rust_progress,
    )

    for result in raw_results:
        directory = Path(result.dir)
        skip_record, sample_record = entropy_records_from_probe(
            directory,
            base_dir,
            min_savings_percent,
            verbosity,
            result.average_entropy,
            result.sampled_files,
            result.sampled_bytes,
            result.lz4_certain,
        )
        if skip_record:
            skip_results[directory] = skip_record
        if sample_record:
            sample_results.append(sample_record)

    return skip_results, sample_results