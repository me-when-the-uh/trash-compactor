import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from colorama import Fore, Style

from .compression.compression_executor import execute_compression_plan
from .compression.compression_planner import iter_files, plan_compression
from .config import (
    DEFAULT_MIN_SAVINGS_PERCENT,
    DRY_RUN_CONSERVATIVE_FACTORS,
    clamp_savings_percent,
    savings_from_entropy,
)
from .i18n import _
from .skip_logic import commit_incompressible_cache, log_directory_skips, maybe_skip_directory
from .stats import CompressionStats, EntropySampleRecord, ProgressTimer
from .timer import PerformanceMonitor
from .workers import lzx_worker_count, set_worker_cap, xp_worker_count


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
    min_savings_percent: float = DEFAULT_MIN_SAVINGS_PERCENT,
    debug_scan_all: bool = False,
) -> tuple[CompressionStats, PerformanceMonitor, list[tuple[Path, int, str]], Path, float, int]:
    import logging

    stats, monitor, base_dir, min_savings_percent, verbosity_level = _setup_context(
        directory_path, min_savings_percent, verbosity
    )
    interactive_output = verbosity_level == 0

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
                if stats.lz4_certain_incompressible_files > 0:
                    final_skip_message += _(
                        "{certain} files are certainly poorly compressible\n"
                    ).format(certain=stats.lz4_certain_incompressible_files)
            else:
                final_skip_message = _("\nNo files discovered for compression.\n")
            timer.stop(final_message=final_skip_message)
            timer = None
            
    return stats, monitor, plan, base_dir, min_savings_percent, verbosity_level


def compress_directory(
    directory_path: str,
    verbosity: int = 0,
    min_savings_percent: float = DEFAULT_MIN_SAVINGS_PERCENT,
    debug_scan_all: bool = False,
) -> tuple[CompressionStats, PerformanceMonitor]:
    
    stats, monitor, plan, base_dir, min_savings_percent, verbosity_level = create_compression_plan(
        directory_path, verbosity, min_savings_percent, debug_scan_all
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
                rate_str = f" @ {processed / elapsed:.0f}/s" if elapsed > 0 else ""
                lines.append(
                    Fore.YELLOW
                    + f"[{elapsed:6.1f}s] Compressing {processed}/{total} files with {algo}...{rate_str}"
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
    commit_incompressible_cache()
    return stats, monitor


def _plan_compression(
    files: list[Path],
    stats: CompressionStats,
    monitor: PerformanceMonitor,
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

    def _on_progress(path: Path, processed: int, should_compress: bool, reason: Optional[str], size: int) -> None:
        if not timer:
            return
        timer.update(processed)

    entropy_started = False
    def _entropy_callback_wrapper(path: Path, processed: int, total: int) -> None:
        nonlocal entropy_started
        if not timer:
            return
        if not entropy_started:
            timer.set_label(_("Entropy analysis"))
            timer.set_total(total)
            entropy_started = True

        timer.update(processed)

    return plan_compression(
        files,
        stats,
        monitor,
        base_dir=base_dir,
        min_savings_percent=min_savings_percent,
        verbosity=verbosity,
        progress_callback=_on_progress,
        entropy_progress_callback=_entropy_callback_wrapper,
        debug_scan_all=debug_scan_all,
    )


def entropy_dry_run(
    directory_path: str,
    *,
    verbosity: int = 0,
    min_savings_percent: float = DEFAULT_MIN_SAVINGS_PERCENT,
    debug_scan_all: bool = False,
) -> tuple[CompressionStats, PerformanceMonitor, list[tuple[Path, int, str]]]:
    
    stats, monitor, plan, base_dir, min_savings_percent, verbosity_level = create_compression_plan(
        directory_path,
        verbosity,
        min_savings_percent=min_savings_percent,
        debug_scan_all=debug_scan_all,
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
            conservative_factor = DRY_RUN_CONSERVATIVE_FACTORS.get(algo, 1.06)
            projected_compressed_xpress += compressed_size * conservative_factor
        else:
            # Fallback if no entropy record found (e.g. sampling failed or skipped)
            projected_compressed_lzx += size
            projected_compressed_xpress += size

    skipped_size = stats.total_compressed_size
    stats.entropy_projected_compressed_bytes = int(round(projected_compressed_lzx + skipped_size))
    stats.entropy_projected_compressed_bytes_conservative = int(round(projected_compressed_xpress + skipped_size))
    
    monitor.end_operation()
    return stats, monitor, plan