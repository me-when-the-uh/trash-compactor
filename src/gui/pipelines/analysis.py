import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ...compression.compression_planner import plan_compression
from ...i18n import _
from ...stats import CompressionStats, apply_entropy_projection
from ...timer import PerformanceMonitor
from ..discovery_stream import GuiDiscoveryStream
from ..progress import (
    ENTROPY_PROGRESS_GRANULARITY,
    PLAN_PROGRESS_GRANULARITY,
    UI_STATUS_INTERVAL_SECONDS,
    UI_SUMMARY_INTERVAL_SECONDS,
    entropy_progress_percent,
    scan_progress_percent,
)
from ..summary import build_analysis_timing, build_live_analysis_timing

if TYPE_CHECKING:
    from ..backend import GuiBackend


def run_analysis_pipeline(
    backend: "GuiBackend",
    *,
    report_completion: bool = True,
    quick_dir_index: Optional[int] = None,
    quick_dir_total: Optional[int] = None,
) -> None:
    backend._configure_worker_environment()
    progress_kwargs = {
        "quick_dir_index": quick_dir_index,
        "quick_dir_total": quick_dir_total,
    }

    base_dir = Path(backend.current_folder).resolve()
    stats = CompressionStats()
    stats.set_base_dir(base_dir)
    stats.min_savings_percent = backend.min_savings
    monitor = PerformanceMonitor()
    monitor.start_operation()

    overall_start_time = time.perf_counter()
    backend._check_processed = 0
    backend._send_progress(_("Scanning directory..."), 0.0, **progress_kwargs)

    discovery = GuiDiscoveryStream(backend, base_dir, stats, progress_kwargs, overall_start_time)
    discovery.prefill_walk()

    plan_count = 0
    total_compressible_size = 0
    entropy_phase_start: Optional[float] = None

    def _elapsed_total(now: float) -> float:
        return max(0.001, now - overall_start_time)

    def _live_scan_seconds(now: float) -> float:
        measured = max(0.0, monitor.stats.file_scan_time)
        if measured > 0:
            return measured
        return _elapsed_total(now)

    def _plan_progress(path: Path, processed: int, should_compress: bool, reason: Optional[str], size: int):
        nonlocal plan_count, total_compressible_size, last_update_time, last_summary_update_time
        if should_compress:
            plan_count += 1
            total_compressible_size += size

        discovery.notify_check_progress(processed)
        total_files = max(discovery.count, processed)
        if processed % PLAN_PROGRESS_GRANULARITY != 0 and processed != total_files:
            return
        backend._check_pause_stop()

        now = time.perf_counter()

        if now - last_update_time > UI_STATUS_INTERVAL_SECONDS or processed == total_files:
            last_update_time = now
            backend._send_check_phase_progress(
                processed,
                total_files,
                **progress_kwargs,
            )

        if now - last_summary_update_time > UI_SUMMARY_INTERVAL_SECONDS or processed == total_files:
            last_summary_update_time = now
            backend._send_folder_summary(
                stats,
                plan_count,
                total_compressible_size,
                directory=str(base_dir),
                scope="current",
                analysis_timing=build_live_analysis_timing(
                    scan_seconds=_live_scan_seconds(now),
                    total_files=total_files,
                    total_seconds=_elapsed_total(now),
                ),
            )

    def _entropy_progress(path: Path, processed: int, total: int):
        nonlocal entropy_phase_start, last_update_time, last_summary_update_time
        if processed % ENTROPY_PROGRESS_GRANULARITY != 0 and processed != total:
            return

        backend._check_pause_stop()
        now = time.perf_counter()
        if entropy_phase_start is None:
            entropy_phase_start = now
        entropy_elapsed = max(0.001, now - entropy_phase_start)

        if now - last_update_time > UI_STATUS_INTERVAL_SECONDS or processed == total:
            last_update_time = now
            backend._send_progress(
                _("Sampling entropy... {processed}/{total}").format(
                    processed=processed,
                    total=total,
                ),
                entropy_progress_percent(processed, total),
                **progress_kwargs,
            )

        if now - last_summary_update_time > UI_SUMMARY_INTERVAL_SECONDS or processed == total:
            last_summary_update_time = now
            backend._send_folder_summary(
                stats,
                plan_count,
                total_compressible_size,
                directory=str(base_dir),
                scope="current",
                analysis_timing=build_live_analysis_timing(
                    scan_seconds=_live_scan_seconds(now),
                    total_files=max(discovery.count, processed),
                    entropy_seconds=entropy_elapsed,
                    entropy_files=processed,
                    total_seconds=_elapsed_total(now),
                ),
            )

    backend._check_phase_start = time.perf_counter()
    last_update_time = backend._check_phase_start
    last_summary_update_time = backend._check_phase_start
    discovery.enter_check_phase()
    backend._send_progress(
        _("Analyzing files..."),
        scan_progress_percent(discovery.count),
        **progress_kwargs,
    )

    plan = plan_compression(
        discovery,
        stats,
        monitor,
        base_dir=base_dir,
        min_savings_percent=backend.min_savings,
        verbosity=0,
        progress_callback=_plan_progress,
        entropy_progress_callback=_entropy_progress,
        debug_scan_all=False,
    )

    total_files = discovery.count
    analysis_elapsed = max(0.001, time.perf_counter() - overall_start_time)

    if total_files == 0:
        backend.last_analysis_plan = []
        backend.last_analysis_stats = stats
        backend.last_analysis_monitor = monitor
        monitor.end_operation()
        backend.last_analysis_timing = build_analysis_timing(
            monitor,
            total_seconds=analysis_elapsed,
            total_files=0,
        )
        backend._send_folder_summary(
            stats,
            0,
            0,
            directory=str(base_dir),
            scope="current",
            analysis_timing=backend.last_analysis_timing,
        )
        if report_completion:
            backend._send_progress(
                _("Scanned in {elapsed:.1f}s").format(elapsed=analysis_elapsed),
                100.0,
                **progress_kwargs,
            )
        return

    monitor.stats.total_files = total_files

    backend.last_analysis_plan = plan
    backend.last_analysis_stats = stats
    backend.last_analysis_monitor = monitor

    apply_entropy_projection(stats, plan)
    monitor.end_operation()
    backend.last_analysis_timing = build_analysis_timing(
        monitor,
        total_seconds=analysis_elapsed,
        total_files=total_files,
    )

    if report_completion:
        backend._send_progress(
            _("Scanned in {elapsed:.1f}s").format(elapsed=analysis_elapsed),
            100.0,
            **progress_kwargs,
        )
    backend._send_folder_summary(
        stats,
        len(plan),
        sum(p[1] for p in plan),
        directory=str(base_dir),
        scope="current",
        analysis_timing=backend.last_analysis_timing,
    )