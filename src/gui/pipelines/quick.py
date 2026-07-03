import threading
import time
from typing import TYPE_CHECKING, Any

from ...file_utils import is_admin
from ...i18n import _
from ...one_click import resolve_targets, run_compactos_hidden
from ...stats import CompressionStats
from ..message_types import CompactOSIndicatorResponse, FolderSummaryResponse, WarningResponse
from ..summary import accumulate_stats, build_live_analysis_timing, make_stats_summary
from .analysis import run_analysis_pipeline

if TYPE_CHECKING:
    from ..backend import GuiBackend


def run_quick_compression_pipeline(backend: "GuiBackend", compactos: bool = False) -> None:
    backend._configure_worker_environment()

    targets = list(resolve_targets().directories)
    if not targets:
        backend._discard_quick_pipeline_state()
        backend._send(WarningResponse(_("Warning"), _("No default quick-compression targets were found on this system.")))
        return

    compactos_thread = None
    if compactos and is_admin():
        backend._send(CompactOSIndicatorResponse(show=True, text=_("Compressing Windows binaries...")))

        def _run_compactos():
            def _progress(s, p=None):
                backend._send(CompactOSIndicatorResponse(show=True, text=s))
            success, output, parsed = run_compactos_hidden(
                progress_callback=_progress,
                line_callback=_progress,
            )
            saved = int(parsed.get("saved_bytes") or 0) if isinstance(parsed, dict) else 0
            msg = _("Done compressing Windows binaries.") if success else _("CompactOS compression failed.")
            backend._send(CompactOSIndicatorResponse(show=True, text=msg, done=success, saved_bytes=saved))

        compactos_thread = threading.Thread(target=_run_compactos, daemon=True)
        compactos_thread.start()

    quick_results: list[dict[str, Any]] = []
    total_analysis_stats = CompressionStats()
    total_analysis_stats.min_savings_percent = backend.min_savings
    total_analysis_plan_count = 0
    total_analysis_compressible_size = 0

    total_scan_seconds = 0.0
    total_entropy_seconds = 0.0
    total_scanned_files = 0
    total_entropy_directories = 0
    total_entropy_files = 0

    quick_start_time = time.perf_counter()
    interrupted = False
    try:
        for index, directory in enumerate(targets, start=1):
            backend._check_pause_stop()
            backend.current_folder = str(directory)
            backend._send_progress(
                _("Quick analysis: scanning {directory} ({index}/{total})").format(
                    directory=directory,
                    index=index,
                    total=len(targets),
                ),
                0.0,
                quick_dir_index=index,
                quick_dir_total=len(targets),
            )
            run_analysis_pipeline(
                backend,
                report_completion=False,
                quick_dir_index=index,
                quick_dir_total=len(targets),
            )

            current_stats = backend.last_analysis_stats
            current_plan = backend.last_analysis_plan or []
            current_monitor = backend.last_analysis_monitor
            current_timing = backend.last_analysis_timing or {}

            if current_stats is None or current_monitor is None:
                continue

            current_plan_size = sum(item[1] for item in current_plan)
            quick_results.append(
                {
                    "directory": str(directory),
                    "plan": current_plan,
                    "stats": current_stats,
                    "monitor": current_monitor,
                    "timing": current_timing,
                }
            )

            accumulate_stats(total_analysis_stats, current_stats)
            total_analysis_plan_count += len(current_plan)
            total_analysis_compressible_size += current_plan_size
            total_scan_seconds += float(current_timing.get("scan_seconds", 0.0) or 0.0)
            total_entropy_seconds += float(current_timing.get("entropy_seconds", 0.0) or 0.0)
            total_scanned_files += int(getattr(current_monitor.stats, "total_files", 0) or 0)
            total_entropy_directories += int(
                getattr(current_monitor.stats, "directories_analyzed_for_entropy", 0) or 0
            )
            total_entropy_files += int(getattr(current_monitor.stats, "files_analyzed_for_entropy", 0) or 0)

            quick_elapsed = max(0.001, time.perf_counter() - quick_start_time)
            total_summary = make_stats_summary(
                total_analysis_stats,
                total_analysis_plan_count,
                total_analysis_compressible_size,
                min_savings_percent=backend.min_savings,
                analysis_timing=build_live_analysis_timing(
                    scan_seconds=total_scan_seconds,
                    total_files=total_scanned_files,
                    entropy_seconds=total_entropy_seconds,
                    entropy_directories=total_entropy_directories,
                    entropy_files=total_entropy_files,
                    total_seconds=quick_elapsed,
                ),
                lzx_enabled=not backend.no_lzx,
            )

            current_summary = make_stats_summary(
                current_stats,
                len(current_plan),
                current_plan_size,
                min_savings_percent=backend.min_savings,
                analysis_timing=current_timing,
                lzx_enabled=not backend.no_lzx,
            )
            backend._send(FolderSummaryResponse(current_summary, directory=str(directory), scope="directory"))
            backend._send(FolderSummaryResponse(total_summary, directory="Total", scope="total"))

        backend.quick_analysis_results = quick_results
        quick_elapsed = max(0.001, time.perf_counter() - quick_start_time)
        backend.last_analysis_timing = build_live_analysis_timing(
            scan_seconds=total_scan_seconds,
            total_files=total_scanned_files,
            entropy_seconds=total_entropy_seconds,
            entropy_directories=total_entropy_directories,
            entropy_files=total_entropy_files,
            total_seconds=quick_elapsed,
        )
        backend._send_progress(
            _("Scanned in {elapsed:.1f}s").format(elapsed=quick_elapsed),
            100.0,
            quick_history=True,
        )
    except InterruptedError:
        interrupted = True
        quick_results.clear()
        backend._discard_quick_pipeline_state()
        raise
    finally:
        if compactos_thread is not None:
            compactos_thread.join(timeout=0.1)