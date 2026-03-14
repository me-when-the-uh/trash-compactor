import os
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

from colorama import Fore, Style

from .compression.compression_executor import execute_compression_plan, legacy_compress_file
from .compression.compression_planner import iter_files, plan_compression
from .compression.entropy import sample_directory_entropy
from .config import DEFAULT_MIN_SAVINGS_PERCENT, clamp_savings_percent, savings_from_entropy
from .i18n import _
from .skip_logic import log_directory_skips, maybe_skip_directory
from .stats import CompressionStats, EntropySampleRecord, LegacyCompressionStats, ProgressTimer
from .timer import PerformanceMonitor
from .workers import entropy_worker_count, lzx_worker_count, set_worker_cap, xp_worker_count


REPORTABLE_DIRECTORY_MIN_BYTES = 5 * 1024 * 1024


def _setup_context(
    directory_path: str,
    min_savings_percent: float,
    verbosity: int,
) -> tuple[CompressionStats, PerformanceMonitor, Path, float, int]:
    stats = CompressionStats()
    monitor = PerformanceMonitor()
    monitor.start_operation()

    min_savings_percent = clamp_savings_percent(min_savings_percent)
    verbosity_level = max(0, int(verbosity))

    base_dir = Path(directory_path).resolve()
    stats.set_base_dir(base_dir)
    stats.min_savings_percent = min_savings_percent

    return stats, monitor, base_dir, min_savings_percent, verbosity_level


def create_compression_plan(
    directory_path: str,
    verbosity: int = 0,
    thorough_check: bool = False,
    min_savings_percent: float = DEFAULT_MIN_SAVINGS_PERCENT,
    debug_scan_all: bool = False,
) -> tuple[CompressionStats, PerformanceMonitor, list[tuple[Path, int, str]], Path, float, int]:
    import logging

    stats, monitor, base_dir, min_savings_percent, verbosity_level = _setup_context(
        directory_path, min_savings_percent, verbosity
    )
    interactive_output = verbosity_level == 0

    if thorough_check:
        logging.info(_("Using thorough checking mode - this will be slower but more accurate for previously compressed files"))

    all_files = list(iter_files(base_dir, stats, verbosity_level, min_savings_percent, collect_entropy=False))
    total_files = len(all_files)
    monitor.stats.total_files = total_files

    timer: Optional[ProgressTimer] = None
    plan: list[tuple[Path, int, str]] = []

    if interactive_output and total_files and getattr(sys.stdout, "isatty", lambda: True)():
        timer = ProgressTimer()
        timer.set_label(_("Scanning files..."))
        timer.start(total=total_files)
        timer.update(0, "")

    try:
        plan = _plan_compression(
            all_files,
            stats,
            monitor,
            thorough_check,
            timer,
            verbosity_level,
            base_dir=base_dir,
            min_savings_percent=min_savings_percent,
            debug_scan_all=debug_scan_all,
        )
    finally:
        if timer:
            if total_files:
                final_skip_message = (
                    _("\n{skipped} out of {total} files are poorly compressible\n").format(
                        skipped=stats.skipped_files, total=total_files
                    )
                )
            else:
                final_skip_message = _("\nNo files discovered for compression.\n")
            timer.stop(final_message=final_skip_message)
            timer = None
            
    return stats, monitor, plan, base_dir, min_savings_percent, verbosity_level


def compress_directory(
    directory_path: str,
    verbosity: int = 0,
    thorough_check: bool = False,
    min_savings_percent: float = DEFAULT_MIN_SAVINGS_PERCENT,
    debug_scan_all: bool = False,
) -> tuple[CompressionStats, PerformanceMonitor]:
    
    stats, monitor, plan, base_dir, min_savings_percent, verbosity_level = create_compression_plan(
        directory_path, verbosity, thorough_check, min_savings_percent, debug_scan_all
    )
    interactive_output = verbosity_level == 0

    return execute_compression_plan_wrapper(stats, monitor, plan, verbosity_level, interactive_output, min_savings_percent)


def execute_compression_plan_wrapper(
    stats: CompressionStats,
    monitor: PerformanceMonitor,
    plan: list[tuple[Path, int, str]],
    verbosity_level: int,
    interactive_output: bool,
    min_savings_percent: float,
) -> tuple[CompressionStats, PerformanceMonitor]:
    monitor.stats.files_skipped = stats.skipped_files

    stage_items: list[tuple[str, list[tuple[Path, int]]]] = []
    stage_states: list[str] = []
    stage_progress: list[dict[str, int]] = []
    stage_processed: dict[str, int] = {}
    stage_index_map: dict[str, int] = {}
    stage_start_times: dict[str, float] = {}
    render_initialized = False
    rendered_lines = 0
    last_render_time = 0.0
    stage_render_interval = 0.1
    stage_render_thread: Optional[threading.Thread] = None
    stage_render_stop: Optional[threading.Event] = None
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

    def _render_stage_statuses(force: bool = False) -> None:
        nonlocal rendered_lines, render_initialized, last_render_time
        if not interactive_output or not stage_items:
            return

        now = time.monotonic()
        if not force and now - last_render_time < stage_render_interval:
            return

        lines: list[str] = []
        for idx, (state, (algo, entries)) in enumerate(zip(stage_states, stage_items)):
            total = stage_progress[idx]['total']
            processed = min(stage_progress[idx]['processed'], total)
            if state == 'done':
                elapsed = now - stage_start_times.get(algo, now)
                if algo in stage_start_times:
                     pass
                lines.append(Fore.GREEN + f"Compressing {total} files with {algo}... done" + Style.RESET_ALL)
            elif state == 'running':
                start_t = stage_start_times.get(algo, now)
                elapsed = now - start_t
                lines.append(
                    Fore.YELLOW
                    + f"[{elapsed:6.1f}s] Compressing {processed}/{total} files with {algo}..."
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
        last_render_time = now

    if plan and interactive_output:
        stage_render_stop = threading.Event()

        with stage_lock:
            _render_stage_statuses(force=True)

        def _render_loop() -> None:
            while not stage_render_stop.wait(stage_render_interval):
                with stage_lock:
                    _render_stage_statuses(force=True)

        stage_render_thread = threading.Thread(target=_render_loop, daemon=True)
        stage_render_thread.start()

    def _stage_start_callback(algo: str, total: int) -> None:
        if not interactive_output or algo not in stage_index_map:
            return
        with stage_lock:
            idx = stage_index_map[algo]
            stage_progress[idx]['total'] = total
            if stage_states[idx] != 'done':
                stage_states[idx] = 'running'
                if algo not in stage_start_times:
                    stage_start_times[algo] = time.monotonic()
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
                if algo not in stage_start_times:
                    stage_start_times[algo] = time.monotonic()
            if stage_processed[algo] >= stage_progress[idx]['total']:
                stage_states[idx] = 'done'
            _render_stage_statuses()

    try:
        if plan:
            xp_workers = xp_worker_count()
            lzx_workers = lzx_worker_count()
            with monitor.time_compression():
                execute_compression_plan(
                    plan,
                    stats,
                    monitor,
                    verbosity_level,
                    xp_workers,
                    lzx_workers,
                    stage_callback=_stage_start_callback,
                    progress_callback=_progress_callback,
                )
            if interactive_output:
                with stage_lock:
                    for algo, _ignored in stage_items:
                        idx = stage_index_map.get(algo)
                        if idx is not None:
                            stage_states[idx] = 'done'
                            stage_progress[idx]['processed'] = stage_progress[idx]['total']
                    _render_stage_statuses(force=True)
                sys.stdout.write("\n")
                sys.stdout.flush()
    finally:
        if stage_render_stop is not None:
            stage_render_stop.set()
        if stage_render_thread is not None:
            stage_render_thread.join(timeout=1.0)
        stage_render_thread = None
        stage_render_stop = None

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
    timer: Optional[ProgressTimer],
    verbosity: int,
    *,
    base_dir: Path,
    min_savings_percent: float,
    debug_scan_all: bool = False,
) -> list[tuple[Path, int, str]]:
    if not files:
        return []

    if timer:
        timer.set_label(_("Analysing files:"))
        timer.set_total(len(files))
        timer.set_message("")

    def _on_progress(path: Path, processed: int, should_compress: bool, reason: Optional[str]) -> None:
        if not timer:
            return
        display = timer.format_path(str(path), str(base_dir))
        if not should_compress and reason:
            display = f"{display} [skip]"
        timer.update(processed, display)
    
    entropy_started = False
    def _entropy_callback_wrapper(path: Path, processed: int, total: int) -> None:
        nonlocal entropy_started
        if not timer:
            return
        if not entropy_started:
            timer.set_label(_("Entropy analysis"))
            timer.set_total(total)
            entropy_started = True
        
        display = timer.format_path(str(path), str(base_dir))
        timer.update(processed, display)

    return plan_compression(
        files,
        stats,
        monitor,
        thorough_check,
        base_dir=base_dir,
        min_savings_percent=min_savings_percent,
        verbosity=verbosity,
        progress_callback=_on_progress,
        entropy_progress_callback=_entropy_callback_wrapper,
        debug_scan_all=debug_scan_all,
    )


def _relative_path(path: Path, base_dir: Path) -> str:
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        try:
            return str(path.resolve().relative_to(base_dir))
        except Exception:
            return str(path)


def _sample_directories_parallel(
    directories: list[Path],
    callback: Optional[Callable[[Path, tuple[Optional[float], int, int]], None]] = None,
) -> dict[Path, tuple[Optional[float], int, int]]:
    if not directories:
        return {}

    worker_count = entropy_worker_count()
    if worker_count <= 1 or len(directories) == 1:
        results = {}
        for directory in directories:
            result = sample_directory_entropy(directory)
            results[directory] = result
            if callback:
                callback(directory, result)
        return results

    results = {}
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(sample_directory_entropy, directory): directory
            for directory in directories
        }

        for future in as_completed(future_map):
            directory = future_map[future]
            try:
                result = future.result()
            except Exception:
                result = (None, 0, 0)
            results[directory] = result
            if callback:
                callback(directory, result)

    return results


def entropy_dry_run(
    directory_path: str,
    *,
    verbosity: int = 0,
    min_savings_percent: float = DEFAULT_MIN_SAVINGS_PERCENT,
    debug_scan_all: bool = False,
) -> tuple[CompressionStats, PerformanceMonitor, list[tuple[Path, int, str]]]:
    
    stats, monitor, plan, base_dir, min_savings_percent, verbosity_level = create_compression_plan(
        directory_path, verbosity, thorough_check=False, min_savings_percent=min_savings_percent, debug_scan_all=debug_scan_all
    )
    stats.entropy_report_threshold_bytes = REPORTABLE_DIRECTORY_MIN_BYTES

    stats.entropy_projected_original_bytes = stats.total_original_size
    
    entropy_map = {Path(r.path): r for r in stats.entropy_samples}
    
    projected_compressed_lzx = 0.0
    projected_compressed_xpress = 0.0
    
    for path, size, algo in plan:
        parent = path.parent
        record = entropy_map.get(parent)
        
        if record:
            savings_factor = max(0.0, record.estimated_savings / 100.0)
            compressed_size = size * (1.0 - savings_factor)
            projected_compressed_lzx += compressed_size
            projected_compressed_xpress += compressed_size * 1.062
        else:
            # Fallback if no entropy record found (e.g. sampling failed or skipped)
            projected_compressed_lzx += size
            projected_compressed_xpress += size

    skipped_size = stats.total_compressed_size
    stats.entropy_projected_compressed_bytes = int(round(projected_compressed_lzx + skipped_size))
    stats.entropy_projected_compressed_bytes_conservative = int(round(projected_compressed_xpress + skipped_size))
    
    monitor.end_operation()
    return stats, monitor, plan


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