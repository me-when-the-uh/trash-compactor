import functools
import logging
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from ..i18n import _
from ..config import COMPRESSION_ALGORITHMS, savings_from_entropy, SKIP_EXTENSIONS
from ..file_utils import CompressionDecision, should_compress_file
from ..skip_logic import append_directory_skip_record, evaluate_entropy_directory, get_incompressible_cache, maybe_skip_directory, sample_directory_entropy
from ..stats import CompressionStats, DirectorySkipRecord, EntropySampleRecord
from ..timer import PerformanceMonitor
from ..workers import entropy_worker_count, scan_worker_count
from .entropy import sample_file_entropy


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
    path: str
    file_size: int
    decision: Optional[CompressionDecision]
    error: Optional[str] = None


def _scan_single(
    entry: os.DirEntry,
    debug_scan_all: bool = False,
    check_already_compressed: bool = True,
) -> _ScanPayload:
    file_path = entry.path
    try:
        st = entry.stat()
        file_size = st.st_size
        attrs = getattr(st, 'st_file_attributes', 0)
    except OSError as exc:
        return _ScanPayload(file_path, 0, None, _("Error processing {file_path}: {exc}").format(file_path=file_path, exc=exc))

    decision = should_compress_file(
        file_path,
        file_size=file_size,
        attributes=attrs,
        ignore_extensions=debug_scan_all,
        check_already_compressed=check_already_compressed,
    )
    return _ScanPayload(file_path, file_size, decision)


def _scan_checks_compressed_state() -> bool:
    value = os.getenv("TRASH_COMPACTOR_FAST_SCAN", "0").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def _iter_scanned_files(files: Iterable[os.DirEntry], debug_scan_all: bool = False) -> Iterator[_ScanPayload]:
    workers = scan_worker_count()
    check_already_compressed = _scan_checks_compressed_state()

    mapper = functools.partial(
        _scan_single, 
        debug_scan_all=debug_scan_all,
        check_already_compressed=check_already_compressed,
    )

    if workers <= 1:
        for entry in files:
            yield mapper(entry)
        return

    # chunking for eliminating per-task dispatch overhead in the ThreadPool
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for result in executor.map(mapper, files, chunksize=2000):
            yield result


def plan_compression(
    files: Iterable[os.DirEntry],
    stats: CompressionStats,
    monitor: PerformanceMonitor,
    *,
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
    progress_callback: Optional[Callable[[Path, int, bool, Optional[str]], None]] = None,
    file_observer: Optional[Callable[[Path, int, CompressionDecision], None]] = None,
    apply_entropy_filter: bool = True,
    entropy_progress_callback: Optional[Callable[[Path, int, int], None]] = None,
    debug_scan_all: bool = False,
) -> list[tuple[Path, int, str]]:
    candidates: list[tuple[Path, int, str]] = []
    with monitor.time_file_scan():
        processed = 0
        for payload in _iter_scanned_files(files, debug_scan_all):
            processed += 1
            file_path = Path(payload.path)
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
                if debug_scan_all and file_path.suffix.lower() in SKIP_EXTENSIONS:
                    entropy_sum, sampled_bytes, _ = sample_file_entropy(file_path, byte_budget=65536)
                    if sampled_bytes > 0:
                        average_entropy = entropy_sum / sampled_bytes
                        savings = savings_from_entropy(average_entropy)
                        if savings >= min_savings_percent:
                            projected_size = int(file_size * (1 - savings / 100))
                            print(
                                f"\n[DEBUG] File {file_path.name} has potential savings: {savings:.1f}% "
                                f"({_format_size(file_size)} -> {_format_size(projected_size)})"
                            )

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
                    already_compressed="already compressed" in lowered,
                    category=category,
                )
                logging.debug("Skipping %s: %s", file_path, reason)
                if progress_callback:
                    progress_callback(file_path, processed, False, reason)

    if apply_entropy_filter:
        with monitor.time_entropy_analysis():
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
    monitor: Optional[PerformanceMonitor] = None,
    min_savings_percent: float,
    verbosity: int,
    progress_callback: Optional[Callable[[Path, int, int], None]] = None,
) -> list[tuple[Path, int, str]]:
    if not candidates or min_savings_percent <= 0:
        return candidates

    directories = {path.parent for path, _, _ in candidates}
    directories.add(base_dir)

    root_skip_record: Optional[DirectorySkipRecord] = None
    if any(path.parent == base_dir for path, _, _ in candidates):
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
            
            # Record sample for root
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

    entropy_records, sample_records = evaluate_directories_parallel(
        directories_to_evaluate,
        base_dir,
        min_savings_percent,
        verbosity,
        progress_callback=progress_callback,
    )

    if monitor:
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


def evaluate_directories_parallel(
    directories: Iterable[Path],
    base_dir: Path,
    min_savings_percent: float,
    verbosity: int,
    progress_callback: Optional[Callable[[Path, int, int], None]] = None,
) -> tuple[dict[Path, DirectorySkipRecord], list[EntropySampleRecord]]:
    directory_list = list(directories)
    if not directory_list:
        return {}, []

    worker_count = entropy_worker_count()
    processed_count = 0
    total_count = len(directory_list)

    def _on_complete(directory: Path) -> None:
        nonlocal processed_count
        processed_count += 1
        if progress_callback:
            progress_callback(directory, processed_count, total_count)

    skip_results: dict[Path, DirectorySkipRecord] = {}
    sample_results: list[EntropySampleRecord] = []

    if worker_count <= 1 or len(directory_list) == 1:
        for directory in directory_list:
            skip_record, sample_record = evaluate_entropy_directory(directory, base_dir, min_savings_percent, verbosity)
            _on_complete(directory)
            if skip_record:
                skip_results[directory] = skip_record
            if sample_record:
                sample_results.append(sample_record)
        return skip_results, sample_results

    with ProcessPoolExecutor(max_workers=worker_count) as executor:
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
            _on_complete(directory)

            try:
                skip_record, sample_record = future.result()
            except Exception as exc:
                logging.debug("Entropy sampling failed for %s: %s", directory, exc, exc_info=True)
                continue

            if skip_record:
                skip_results[directory] = skip_record
            if sample_record:
                sample_results.append(sample_record)

    return skip_results, sample_results

