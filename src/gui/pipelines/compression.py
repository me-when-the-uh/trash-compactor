import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from ...compression.compression_executor import execute_compression_plan
from ...exceptions import WorkerStopped
from ...i18n import _
from ...skip_logic import commit_incompressible_cache
from ...stats import CompressionStats
from ...workers import lzx_worker_count, xp_worker_count
from ..message_types import ProgressUpdateResponse, StateResponse, StatusResponse, WarningResponse
from ..progress import COMPRESSION_PROGRESS_GRANULARITY

if TYPE_CHECKING:
    from ..backend import GuiBackend


def _exec_progress_factory(
    backend: "GuiBackend",
    *,
    total: int,
    compressed_count: list[int],
    exec_start_time: float,
    status_template: str,
    pct_fn: Callable[[int, int], float],
    summary_fn: Callable[[], None],
    update_interval: float = 0.1,
    use_send_progress: bool = False,
) -> Callable[[Path, str], None]:
    last_update = exec_start_time

    def _exec_progress(path: Path, algo: str) -> None:
        nonlocal last_update
        compressed_count[0] += 1
        if compressed_count[0] % COMPRESSION_PROGRESS_GRANULARITY != 0 and compressed_count[0] != total:
            return

        backend._check_pause_stop()
        now = time.perf_counter()
        if now - last_update <= update_interval and compressed_count[0] != total:
            return
        last_update = now

        elapsed = max(0.001, now - exec_start_time)
        rate = compressed_count[0] / elapsed
        pct = pct_fn(compressed_count[0], total)
        status = status_template.format(
            compressed=compressed_count[0],
            total=total,
            rate=rate,
        )
        if use_send_progress:
            backend._send_progress(status, pct)
        else:
            backend._send(ProgressUpdateResponse(status, pct))
        summary_fn()

    return _exec_progress


def run_compression_pipeline(backend: "GuiBackend") -> None:
    backend._configure_worker_environment()

    if backend.quick_analysis_results:
        run_quick_analysis_compression(backend)
        return

    if not hasattr(backend, 'last_analysis_plan') or backend.last_analysis_plan is None:
        backend._send(WarningResponse("Warning", "Please analyze the folder before compressing."))
        return

    plan = backend.last_analysis_plan
    stats = backend.last_analysis_stats
    monitor = backend.last_analysis_monitor

    if not plan:
        backend._send(ProgressUpdateResponse(_("Nothing to compress!"), 100.0))
        return

    backend._send(StateResponse("Compacting"))

    total_to_compress = len(plan)
    total_compressible_size = sum(p[1] for p in plan)
    compressed_count = [0]
    exec_start_time = time.perf_counter()

    def _summary() -> None:
        backend._send_folder_summary(
            stats,
            total_to_compress,
            total_compressible_size,
            directory=str(backend.current_folder),
            scope="current",
            is_analysis=False,
        )

    _exec_progress = _exec_progress_factory(
        backend,
        total=total_to_compress,
        compressed_count=compressed_count,
        exec_start_time=exec_start_time,
        status_template=_("Compressing... {compressed}/{total} ({rate:.0f} files/s)"),
        pct_fn=lambda n, t: 60.0 + (n / t) * 40.0,
        summary_fn=_summary,
    )

    try:
        with monitor.time_compression():
            execute_compression_plan(
                plan,
                stats,
                monitor,
                verbosity=0,
                xp_workers=xp_worker_count(),
                lzx_workers=lzx_worker_count(),
                stage_callback=lambda _algo, _total: None,
                progress_callback=_exec_progress,
            )
    except Exception:
        from ...skip_logic import discard_staged_incompressible_cache

        discard_staged_incompressible_cache()
        raise

    commit_incompressible_cache()
    backend.last_analysis_plan = None
    backend.last_analysis_stats = None

    backend._send_progress(_("Complete!"), 100.0, final=True)
    backend._send_folder_summary(
        stats,
        stats.compressed_files,
        total_compressible_size,
        directory=str(backend.current_folder),
        scope="current",
        is_analysis=False,
    )


def run_quick_analysis_compression(backend: "GuiBackend") -> None:
    entries = list(backend.quick_analysis_results)
    if not entries:
        backend._send(WarningResponse(_("Warning"), _("Please run quick analysis before compressing.")))
        return

    total_to_compress = sum(len(entry.get("plan") or []) for entry in entries)
    if total_to_compress <= 0:
        backend._send(ProgressUpdateResponse(_("Nothing to compress!"), 100.0))
        return

    backend._send(StateResponse("Compacting"))

    total_stats = CompressionStats()
    total_stats.min_savings_percent = backend.min_savings
    total_target_size = 0
    compressed_count = [0]
    exec_start_time = time.perf_counter()

    try:
        _run_quick_compression_loop(
            backend,
            entries,
            total_to_compress,
            total_stats,
            total_target_size,
            compressed_count,
            exec_start_time,
        )
    except WorkerStopped:
        backend._discard_quick_pipeline_state()
        raise


def _run_quick_compression_loop(
    backend: "GuiBackend",
    entries: list[dict[str, Any]],
    total_to_compress: int,
    total_stats: CompressionStats,
    total_target_size: int,
    compressed_count: list[int],
    exec_start_time: float,
) -> None:
    try:
        for index, entry in enumerate(entries, start=1):
            backend._check_pause_stop()

            directory = str(entry.get("directory") or "")
            plan = entry.get("plan") or []
            stats = entry.get("stats")
            monitor = entry.get("monitor")

            if not directory or stats is None or monitor is None:
                continue

            if not plan:
                backend._send(
                    StatusResponse(
                        _("Quick compression: nothing to compress in {directory} ({index}/{total})").format(
                            directory=directory,
                            index=index,
                            total=len(entries),
                        ),
                        None,
                    )
                )
                continue

            backend.current_folder = directory
            directory_target_size = sum(item[1] for item in plan)
            total_target_size += directory_target_size
            backend._send(
                StatusResponse(
                    _("Quick compression: compressing {directory} ({index}/{total})").format(
                        directory=directory,
                        index=index,
                        total=len(entries),
                    ),
                    None,
                )
            )

            def _summary() -> None:
                backend._send_folder_summary(
                    stats,
                    len(plan),
                    directory_target_size,
                    directory=directory,
                    scope="directory",
                    is_analysis=False,
                )

            _exec_progress = _exec_progress_factory(
                backend,
                total=total_to_compress,
                compressed_count=compressed_count,
                exec_start_time=exec_start_time,
                status_template=_("Quick compressing... {compressed}/{total} ({rate:.0f} files/s)"),
                pct_fn=lambda n, t: (n / t) * 100.0,
                summary_fn=_summary,
                update_interval=0.0,
                use_send_progress=True,
            )

            with monitor.time_compression():
                execute_compression_plan(
                    plan,
                    stats,
                    monitor,
                    verbosity=0,
                    xp_workers=xp_worker_count(),
                    lzx_workers=lzx_worker_count(),
                    stage_callback=lambda _algo, _total: None,
                    progress_callback=_exec_progress,
                )

            from ..summary import accumulate_stats
            accumulate_stats(total_stats, stats)

            backend._send_folder_summary(
                stats,
                len(plan),
                directory_target_size,
                directory=directory,
                scope="directory",
                is_analysis=False,
            )
            backend._send_folder_summary(
                total_stats,
                total_stats.compressed_files,
                total_target_size,
                directory="Total",
                scope="total",
                is_analysis=False,
            )
    except Exception:
        from ...skip_logic import discard_staged_incompressible_cache

        discard_staged_incompressible_cache()
        raise

    commit_incompressible_cache()
    backend.last_analysis_plan = None
    backend.last_analysis_stats = None
    backend.quick_analysis_results = []
    backend._send_progress(
        _("Quick compression complete"),
        100.0,
        quick_history=True,
        final=True,
    )