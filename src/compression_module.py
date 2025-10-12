import os
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from colorama import Fore, Style

from .compression.compression_executor import execute_compression_plan, legacy_compress_file
from .compression.compression_planner import iter_files, plan_compression
from .compression.entropy import sample_directory_entropy
from .config import DEFAULT_MIN_SAVINGS_PERCENT, clamp_savings_percent, savings_from_entropy
from .skip_logic import log_directory_skips, maybe_skip_directory
from .stats import CompressionStats, EntropySampleRecord, LegacyCompressionStats, Spinner
from .timer import PerformanceMonitor
from .workers import lzx_worker_count, set_worker_cap, xp_worker_count


REPORTABLE_DIRECTORY_MIN_BYTES = 5 * 1024 * 1024


def compress_directory(
    directory_path: str,
    verbosity: int = 0,
    thorough_check: bool = False,
    min_savings_percent: float = DEFAULT_MIN_SAVINGS_PERCENT,
) -> tuple[CompressionStats, PerformanceMonitor]:
    import logging

    stats = CompressionStats()
    monitor = PerformanceMonitor()
    monitor.start_operation()

    min_savings_percent = clamp_savings_percent(min_savings_percent)

    verbosity_level = max(0, int(verbosity))
    interactive_output = verbosity_level == 0

    base_dir = Path(directory_path).resolve()
    stats.set_base_dir(base_dir)
    stats.min_savings_percent = min_savings_percent
    if thorough_check:
        logging.info("Using thorough checking mode - this will be slower but more accurate for previously compressed files")

    all_files = list(iter_files(base_dir, stats, verbosity_level, min_savings_percent, collect_entropy=False))
    total_files = len(all_files)
    monitor.stats.total_files = total_files

    spinner: Optional[Spinner] = None
    plan: list[tuple[Path, int, str]] = []

    if interactive_output and total_files and getattr(sys.stdout, "isatty", lambda: True)():
        spinner = Spinner()
        spinner.set_label("Scanning files...")
        spinner.start(total=total_files)
        spinner.update(0, "")

    try:
        plan = _plan_compression(
            all_files,
            stats,
            monitor,
            thorough_check,
            spinner,
            verbosity_level,
            base_dir=base_dir,
            min_savings_percent=min_savings_percent,
        )
    finally:
        if spinner:
            if total_files:
                final_skip_message = (
                    f"\n{stats.skipped_files} out of {total_files} files are poorly compressible\n"
                )
            else:
                final_skip_message = "\nNo files discovered for compression.\n"
            spinner.stop(final_message=final_skip_message)
            spinner = None

    monitor.stats.files_skipped = stats.skipped_files

    stage_items: list[tuple[str, list[tuple[Path, int]]]] = []
    stage_states: list[str] = []
    stage_progress: list[dict[str, int]] = []
    stage_processed: dict[str, int] = {}
    stage_index_map: dict[str, int] = {}
    render_initialized = False
    rendered_lines = 0
    stage_lock = threading.Lock()

    if plan and interactive_output:
        grouped: dict[str, list[tuple[Path, int]]] = {}
        for path, size, algorithm in plan:
            grouped.setdefault(algorithm, []).append((path, size))
        stage_items = list(grouped.items())
        stage_states = ['pending'] * len(stage_items)
        stage_progress = [{'total': len(entries), 'processed': 0} for _, entries in stage_items]
        stage_processed = {algo: 0 for algo, _ in stage_items}
        stage_index_map = {algo: idx for idx, (algo, _) in enumerate(stage_items)}

    def _render_stage_statuses() -> None:
        nonlocal rendered_lines, render_initialized
        if not interactive_output or not stage_items:
            return

        spinner_chars = ['\\', '|', '/', '-']
        spinner_idx = int(time.time() * 2) % len(spinner_chars)

        lines: list[str] = []
        for idx, (state, (algo, entries)) in enumerate(zip(stage_states, stage_items)):
            total = stage_progress[idx]['total']
            processed = min(stage_progress[idx]['processed'], total)
            if state == 'done':
                lines.append(Fore.GREEN + f"Compressing {total} files with {algo}... done" + Style.RESET_ALL)
            elif state == 'running':
                lines.append(
                    Fore.YELLOW
                    + f"{spinner_chars[spinner_idx]} Compressing {processed}/{total} files with {algo}..."
                    + Style.RESET_ALL
                )
            else:
                lines.append(f"Pending {total} files for {algo} compression.")

        if not lines:
            return

        if not render_initialized:
            sys.stdout.write("\n" * len(lines))
            sys.stdout.flush()
            render_initialized = True
            rendered_lines = len(lines)

        if rendered_lines:
            sys.stdout.write("\033[F" * rendered_lines)
        for line in lines:
            sys.stdout.write("\r" + line + "\033[K\n")
        sys.stdout.flush()
        rendered_lines = len(lines)

    if plan and interactive_output:
        with stage_lock:
            _render_stage_statuses()

    def _stage_start_callback(algo: str, total: int) -> None:
        if not interactive_output or algo not in stage_index_map:
            return
        with stage_lock:
            idx = stage_index_map[algo]
            stage_progress[idx]['total'] = total
            if stage_states[idx] != 'done':
                stage_states[idx] = 'running'
            _render_stage_statuses()

    def _progress_callback(path: Path, algo: str) -> None:
        if not interactive_output or algo not in stage_index_map:
            return
        with stage_lock:
            idx = stage_index_map[algo]
            stage_processed[algo] += 1
            stage_progress[idx]['processed'] = stage_processed[algo]
            if stage_states[idx] == 'pending':
                stage_states[idx] = 'running'
            if stage_processed[algo] >= stage_progress[idx]['total']:
                stage_states[idx] = 'done'
            _render_stage_statuses()

    if plan:
        xp_workers = xp_worker_count()
        lzx_workers = lzx_worker_count()
        execute_compression_plan(
            plan,
            stats,
            monitor,
            verbosity_level >= 4,
            xp_workers,
            lzx_workers,
            stage_callback=_stage_start_callback,
            progress_callback=_progress_callback,
        )
        if interactive_output:
            with stage_lock:
                for algo, _ in stage_items:
                    idx = stage_index_map.get(algo)
                    if idx is not None:
                        stage_states[idx] = 'done'
                        stage_progress[idx]['processed'] = stage_progress[idx]['total']
                _render_stage_statuses()
            sys.stdout.write("\n")
            sys.stdout.flush()

    log_directory_skips(stats, verbosity_level, min_savings_percent)

    monitor.stats.files_compressed = stats.compressed_files
    monitor.stats.files_skipped = stats.skipped_files
    monitor.end_operation()
    return stats, monitor


def compress_directory_legacy(directory_path: str, thorough_check: bool = False) -> LegacyCompressionStats:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    stats = LegacyCompressionStats()
    base_dir = Path(directory_path).resolve()

    print(f"\nChecking files in {directory_path} for proper compression branding...")
    if thorough_check:
        print("Using thorough checking mode - this will be slower but more accurate")

    from .config import get_cpu_info
    from .workers import _apply_worker_cap

    physical_cores, _ = get_cpu_info()
    default_workers = max(physical_cores or 1, 1)
    worker_count = _apply_worker_cap(default_workers)
    if worker_count == default_workers:
        print(f"Using {worker_count} parallel workers to maximize performance\n")
    else:
        noun = "worker" if worker_count == 1 else "workers"
        print(f"Using {worker_count} {noun} due to storage throttling hints\n")

    targets = _collect_branding_targets(base_dir, stats, thorough_check)
    if not targets:
        print("No files need branding - all eligible files are already marked as compressed.")
        return stats

    print(f"Found {len(targets)} files that need branding...")
    base_dir_str = str(base_dir)

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_to_file = {executor.submit(legacy_compress_file, path): path for path in targets}
        total = len(targets)

        for completed, future in enumerate(as_completed(future_to_file), start=1):
            file_path = future_to_file[future]
            relative_path = os.path.relpath(str(file_path), base_dir_str)

            if completed % 10 == 0 or completed == total:
                print(f"Progress: {completed}/{total} files processed ({completed / total * 100:.1f}%)")

            try:
                result = future.result()
                if result:
                    from .file_utils import is_file_compressed
                    is_compressed, _ = is_file_compressed(file_path, thorough_check=False)
                    if is_compressed:
                        stats.branded_files += 1
                    else:
                        stats.still_unmarked += 1
                        print(f"WARNING: File still not recognized as compressed: {relative_path}")
                else:
                    print(f"ERROR: Failed branding file: {relative_path}")
            except (OSError, ValueError) as exc:
                stats.errors.append(f"Exception for {file_path}: {exc}")
                print(f"ERROR: Exception {exc} while branding file: {relative_path}")

    print(f"\nBranding complete. Successfully branded {stats.branded_files} files.")
    if stats.still_unmarked:
        print(f"Warning: {stats.still_unmarked} files could not be properly marked as compressed.")

    return stats


def _plan_compression(
    files: list[Path],
    stats: CompressionStats,
    monitor: PerformanceMonitor,
    thorough_check: bool,
    spinner: Optional[Spinner],
    verbosity: int,
    *,
    base_dir: Path,
    min_savings_percent: float,
) -> list[tuple[Path, int, str]]:
    if not files:
        return []

    if spinner:
        spinner.set_label("Analysing files...")
        spinner.set_total(len(files))
        spinner.set_message("")

    def _on_progress(path: Path, processed: int, should_compress: bool, reason: Optional[str]) -> None:
        if not spinner:
            return
        display = spinner.format_path(str(path), str(base_dir))
        if not should_compress and reason:
            display = f"{display} [skip]"
        spinner.update(processed, display)

    return plan_compression(
        files,
        stats,
        monitor,
        thorough_check,
        base_dir=base_dir,
        min_savings_percent=min_savings_percent,
        verbosity=verbosity,
        progress_callback=_on_progress,
    )


def _relative_path(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        try:
            return str(path.resolve().relative_to(base_dir))
        except Exception:
            return str(path)


def entropy_dry_run(
    directory_path: str,
    *,
    verbosity: int = 0,
    min_savings_percent: float = DEFAULT_MIN_SAVINGS_PERCENT,
) -> CompressionStats:
    import logging

    stats = CompressionStats()
    min_savings_percent = clamp_savings_percent(min_savings_percent)
    base_dir = Path(directory_path).resolve()
    stats.set_base_dir(base_dir)
    stats.min_savings_percent = min_savings_percent
    stats.entropy_report_threshold_bytes = REPORTABLE_DIRECTORY_MIN_BYTES

    spinner: Optional[Spinner] = None
    if verbosity == 0 and getattr(sys.stdout, "isatty", lambda: True)():
        spinner = Spinner()
        spinner.set_label("Analysing directory entropy")
        spinner.start()

    root_decision = maybe_skip_directory(
        base_dir,
        base_dir,
        stats,
        collect_entropy=False,
        min_savings_percent=min_savings_percent,
        verbosity=verbosity,
    )
    if root_decision.skip:
        logging.warning(
            "Dry run aborted: base directory %s is excluded (%s)",
            base_dir,
            root_decision.reason or "excluded",
        )
        if spinner:
            spinner.stop("\nEntropy analysis skipped: base directory excluded.\n")
        return stats

    def _ancestors_through_base(path: Path) -> list[Path]:
        chain: list[Path] = []
        current = path
        while True:
            chain.append(current)
            if current == base_dir:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
        return chain

    all_files = list(
        iter_files(
            base_dir,
            stats,
            verbosity,
            min_savings_percent,
            collect_entropy=False,
        )
    )

    monitor = PerformanceMonitor()
    eligible_directories: set[Path] = {base_dir}
    eligible_files_found = 0
    direct_file_bytes: defaultdict[Path, int] = defaultdict(int)

    def _observe_file(file_path: Path, file_size: int, decision) -> None:
        nonlocal eligible_files_found
        direct_file_bytes[file_path.parent] += file_size
        if decision.should_compress:
            eligible_files_found += 1
            for ancestor in _ancestors_through_base(file_path.parent):
                eligible_directories.add(ancestor)

    plan_compression(
        all_files,
        stats,
        monitor,
        thorough_check=False,
        base_dir=base_dir,
        min_savings_percent=min_savings_percent,
        verbosity=verbosity,
        progress_callback=None,
        file_observer=_observe_file,
        apply_entropy_filter=False,
    )

    stats.entropy_projected_original_bytes = stats.total_original_size

    if eligible_files_found == 0:
        if spinner:
            spinner.stop("\nEntropy analysis skipped: no compressible files detected.\n")
        return stats

    pending = deque([base_dir])
    visited: set[Path] = set()
    # Track directory topology so we can roll totals up without re-traversing
    children_map: defaultdict[Path, list[Path]] = defaultdict(list)
    ordered_dirs: list[Path] = []
    raw_samples: list[tuple[Path, float, float, int, int]] = []

    while pending:
        current = pending.popleft()
        try:
            marker = current.resolve()
        except OSError:
            marker = current
        if marker in visited:
            continue
        visited.add(marker)
        ordered_dirs.append(current)

        try:
            entries = sorted(current.iterdir(), key=lambda entry: entry.name.casefold())
        except OSError as exc:
            logging.debug("Unable to inspect %s during dry run: %s", current, exc)
            continue

        for entry in entries:
            if not entry.is_dir():
                continue
            decision = maybe_skip_directory(
                entry,
                base_dir,
                stats,
                collect_entropy=False,
                min_savings_percent=min_savings_percent,
                verbosity=verbosity,
            )
            if decision.skip:
                continue
            children_map[current].append(entry)
            pending.append(entry)

        if current == base_dir:
            continue

        if current not in eligible_directories:
            continue

        average_entropy, sampled_files, sampled_bytes = sample_directory_entropy(current)
        if average_entropy is None or sampled_files == 0 or sampled_bytes == 0:
            continue

        estimated_savings = savings_from_entropy(average_entropy)
        stats.entropy_directories_sampled += 1
        below_threshold = estimated_savings < min_savings_percent
        if below_threshold:
            stats.entropy_directories_below_threshold += 1

        if spinner:
            note = "below threshold" if below_threshold else f"~{estimated_savings:.1f}% savings"
            spinner.set_label(
                f"Analysing directory entropy ({stats.entropy_directories_sampled})"
            )
            spinner.update(
                stats.entropy_directories_sampled,
                f"{spinner.format_path(str(current), str(base_dir))} {note}",
            )

        raw_samples.append((current, average_entropy, estimated_savings, sampled_files, sampled_bytes))

        if verbosity >= 4:
            logging.debug(
                "Dry run sample %s: entropy %.2f (~%.1f%% savings) from %s files (%s bytes)",
                current,
                average_entropy,
                estimated_savings,
                sampled_files,
                sampled_bytes,
            )

    def _relative_depth(path: Path) -> int:
        if path == base_dir:
            return 0
        try:
            return len(path.relative_to(base_dir).parts)
        except ValueError:
            return len(path.parts)

    total_bytes_map: dict[Path, int] = {}
    # Bottom-up accumulation keeps complexity linear while giving every directory its full size
    for directory in sorted(ordered_dirs, key=_relative_depth, reverse=True):
        total = direct_file_bytes.get(directory, 0)
        for child in children_map.get(directory, []):
            total += total_bytes_map.get(child, 0)
        total_bytes_map[directory] = total

    base_total = total_bytes_map.get(base_dir, 0)
    if base_total and (verbosity >= 4 or base_total >= REPORTABLE_DIRECTORY_MIN_BYTES):
        average_entropy, sampled_files, sampled_bytes = sample_directory_entropy(
            base_dir,
            skip_root_files=True,
        )
        if average_entropy is not None and sampled_files > 0 and sampled_bytes > 0:
            estimated_savings = savings_from_entropy(average_entropy)
            stats.entropy_directories_sampled += 1
            below_threshold = estimated_savings < min_savings_percent
            if below_threshold:
                stats.entropy_directories_below_threshold += 1
            if spinner:
                note = "below threshold" if below_threshold else f"~{estimated_savings:.1f}% savings"
                spinner.set_label(
                    f"Analysing directory entropy ({stats.entropy_directories_sampled})"
                )
                spinner.update(
                    stats.entropy_directories_sampled,
                    f"{spinner.format_path(str(base_dir), str(base_dir))} {note}",
                )
            raw_samples.append((base_dir, average_entropy, estimated_savings, sampled_files, sampled_bytes))

            if verbosity >= 4:
                logging.debug(
                    "Dry run sample %s: entropy %.2f (~%.1f%% savings) from %s files (%s bytes)",
                    base_dir,
                    average_entropy,
                    estimated_savings,
                    sampled_files,
                    sampled_bytes,
                )

    sampled_paths = {path for path, *_ in raw_samples}
    reportable_totals: dict[Path, int] = {}
    reported_flags: dict[Path, bool] = {}

    # Filter out chains of single-child directories
    for directory in sorted(ordered_dirs, key=_relative_depth, reverse=True):
        residual = direct_file_bytes.get(directory, 0)
        for child in children_map.get(directory, []):
            if child in sampled_paths:
                continue
            residual += total_bytes_map.get(child, 0)
        reportable_totals[directory] = residual
        reported_flags[directory] = directory in sampled_paths and residual >= REPORTABLE_DIRECTORY_MIN_BYTES

    records: list[EntropySampleRecord] = []
    for path, average_entropy, estimated_savings, sampled_files, sampled_bytes in raw_samples:
        records.append(
            EntropySampleRecord(
                path=str(path),
                relative_path=_relative_path(path, base_dir),
                average_entropy=average_entropy,
                estimated_savings=estimated_savings,
                sampled_files=sampled_files,
                sampled_bytes=sampled_bytes,
                total_bytes=total_bytes_map.get(path, 0),
            )
        )

    if verbosity >= 4:
        selected = records
    else:
        # Only surface directories that contribute to savings
        selected = [
            record for record in records if reported_flags.get(Path(record.path), False)
        ]

    stats.entropy_samples = selected
    stats.entropy_samples.sort(key=lambda record: record.estimated_savings, reverse=True)

    base_total = total_bytes_map.get(base_dir, 0)
    projected_total = float(base_total)
    for record in records:
        record_path = Path(record.path)
        share = reportable_totals.get(record_path, total_bytes_map.get(record_path, 0))
        if share <= 0:
            continue
        if record.estimated_savings < min_savings_percent:
            continue
        projected_total -= share
        projected_total += share * max(0.0, 1 - record.estimated_savings / 100.0)

    stats.entropy_projected_original_bytes = base_total
    stats.entropy_projected_compressed_bytes = max(int(round(projected_total)), 0)
    if spinner:
        summary = (
            f"\nEntropy analysis complete: {stats.entropy_directories_sampled} directories sampled, "
            f"{stats.entropy_directories_below_threshold} below threshold.\n"
        )
        spinner.stop(summary)
    return stats


def _collect_branding_targets(
    base_dir: Path,
    stats: LegacyCompressionStats,
    thorough_check: bool,
) -> list[Path]:
    from .config import MIN_COMPRESSIBLE_SIZE, SKIP_EXTENSIONS
    from .file_utils import is_file_compressed

    targets = []
    for root, _, files in os.walk(base_dir):
        for name in files:
            file_path = Path(root) / name
            stats.total_files += 1

            if file_path.suffix.lower() in SKIP_EXTENSIONS:
                continue

            try:
                if file_path.stat().st_size < MIN_COMPRESSIBLE_SIZE:
                    continue

                is_compressed, _ = is_file_compressed(file_path, thorough_check)
                if not is_compressed:
                    targets.append(file_path)
            except (OSError, ValueError) as exc:
                stats.errors.append(f"Error checking file {file_path}: {exc}")
    return targets