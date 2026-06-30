import logging
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .message_types import (
    GuiRequest,
    GuiResponse,
    AnalyseFolderRequest,
    StartCompressionRequest,
    PauseCompressionRequest,
    ResumeCompressionRequest,
    StopCompressionRequest,
    SelectFolderRequest,
    FolderSummaryResponse,
    StatusResponse,
    StateResponse,
    ProgressUpdateResponse,
    WarningResponse,
    ConfigResponse,
    QuickCompressionTargetsResponse,
    ResetConfigRequest,
    CompactOSIndicatorResponse
)
from .webview_server import create_gui_app, GuiServer
from ..i18n import _

from ..compression.compression_executor import execute_compression_plan
from ..compression.compression_planner import iter_files, plan_compression
from ..stats import CompressionStats
from ..timer import PerformanceMonitor
from ..workers import lzx_worker_count, xp_worker_count
from ..config import DEFAULT_MIN_SAVINGS_PERCENT, DRY_RUN_CONSERVATIVE_FACTORS
from ..file_utils import is_admin
from ..skip_logic import commit_incompressible_cache
from ..one_click import resolve_targets, run_compactos_hidden


UI_STATUS_INTERVAL_SECONDS = 0.10
UI_SUMMARY_INTERVAL_SECONDS = 0.25
SCAN_STOP_CHECK_EVERY_FILES = 64
PLAN_PROGRESS_GRANULARITY = 32
ENTROPY_PROGRESS_GRANULARITY = 8
COMPRESSION_PROGRESS_GRANULARITY = 4
_PROGRESS_INDETERMINATE = -1.0
_PROGRESS_WALK_DONE = 35.0
_PROGRESS_CHECK_END = 75.0
_PROGRESS_ENTROPY_END = 98.0


class _GuiDiscoveryStream:
    """Yield DirEntry objects while updating walk UI; avoids materializing the full file list."""

    __slots__ = (
        "_backend",
        "_base_dir",
        "_stats",
        "_progress_kwargs",
        "count",
        "walk_seconds",
        "complete",
        "buffering",
        "_check_phase",
        "_walk_start",
        "_last_scan_update",
        "_last_summary_update",
    )

    def __init__(
        self,
        backend: "GuiBackend",
        base_dir: Path,
        stats: CompressionStats,
        progress_kwargs: dict[str, Any],
    ) -> None:
        self._backend = backend
        self._base_dir = base_dir
        self._stats = stats
        self._progress_kwargs = progress_kwargs
        self.count = 0
        self.walk_seconds = 0.0
        self.complete = False
        self.buffering = False
        self._check_phase = False
        self._walk_start = 0.0
        self._last_scan_update = 0.0
        self._last_summary_update = 0.0

    def enter_check_phase(self) -> None:
        self._check_phase = True

    def notify_check_progress(self, processed: int) -> None:
        if processed > self._backend._check_processed:
            self.buffering = False
        self._backend._check_processed = processed

    def _sync_buffering_state(self) -> None:
        if not self._check_phase or self.complete:
            self.buffering = False
            return
        processed = self._backend._check_processed
        if processed > 0 and processed >= self.count - 1:
            self.buffering = True

    def __iter__(self):
        self._walk_start = time.perf_counter()
        self._last_scan_update = self._walk_start
        self._last_summary_update = self._walk_start
        try:
            for entry in iter_files(
                self._base_dir,
                self._stats,
                0,
                self._backend.min_savings,
                collect_entropy=False,
            ):
                self.count += 1
                if self.count % SCAN_STOP_CHECK_EVERY_FILES == 0:
                    self._backend._check_pause_stop()

                now = time.perf_counter()
                walk_elapsed = max(0.001, now - self._walk_start)
                if self._check_phase:
                    self._sync_buffering_state()
                    processed = self._backend._check_processed
                    if (
                        (self.buffering or processed < self.count)
                        and now - self._last_scan_update > UI_STATUS_INTERVAL_SECONDS
                    ):
                        self._last_scan_update = now
                        self._backend._send_check_phase_progress(
                            processed,
                            self.count,
                            scanning_more=self.buffering,
                            **self._progress_kwargs,
                        )
                elif now - self._last_scan_update > UI_STATUS_INTERVAL_SECONDS:
                    self._last_scan_update = now
                    self._backend._send_progress(
                        _(
                            "Scanning directory... {count} files found ({rate:.0f} files/s)"
                        ).format(
                            count=self.count,
                            rate=self.count / walk_elapsed,
                        ),
                        _PROGRESS_INDETERMINATE,
                        **self._progress_kwargs,
                    )

                if now - self._last_summary_update > UI_SUMMARY_INTERVAL_SECONDS:
                    self._last_summary_update = now
                    if self._check_phase:
                        check_elapsed = max(
                            0.001,
                            now - self._backend._check_phase_start,
                        )
                        timing = self._backend._build_live_analysis_timing(
                            walk_seconds=walk_elapsed,
                            total_files=max(self.count, self._backend._check_processed),
                            check_seconds=check_elapsed,
                        )
                    else:
                        timing = self._backend._build_live_analysis_timing(
                            walk_seconds=walk_elapsed,
                            total_files=self.count,
                        )
                    self._backend._send_folder_summary(
                        self._stats,
                        0,
                        0,
                        directory=str(self._base_dir),
                        scope="current",
                        analysis_timing=timing,
                    )
                yield entry
        finally:
            self.complete = True
            self.buffering = False
            self.walk_seconds = max(0.001, time.perf_counter() - self._walk_start)


class GuiBackend:
    def __init__(self, benchmark_ok: Optional[bool] = None):
        self.server: Optional[GuiServer] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.benchmark_ok = benchmark_ok

        self.min_savings = DEFAULT_MIN_SAVINGS_PERCENT
        self.current_folder = ""
        self.decimal = False
        self.default_no_lzx = benchmark_ok is False
        self.no_lzx = self.default_no_lzx
        self.force_lzx = False
        self.single_worker = False
        self.lzx_warning = _("It is recommended to disable LZX compression for this system.") if self.default_no_lzx else ""

        self.last_analysis_plan = None
        self.last_analysis_stats = None
        self.last_analysis_monitor = None
        self.last_analysis_timing = None
        self.quick_analysis_results: list[dict[str, Any]] = []

    def _current_config_response(self) -> ConfigResponse:
        return ConfigResponse(
            decimal=self.decimal,
            min_savings=self.min_savings,
            no_lzx=self.no_lzx,
            force_lzx=self.force_lzx,
            single_worker=self.single_worker,
            lzx_warning=self.lzx_warning,
        )

    def _clear_analysis_state(self) -> None:
        self.last_analysis_plan = None
        self.last_analysis_stats = None
        self.last_analysis_monitor = None
        self.last_analysis_timing = None

    def _clear_quick_analysis_results(self) -> None:
        self.quick_analysis_results = []

    def _requested_path(self, request: GuiRequest) -> str:
        return getattr(request, "path", "") or self.current_folder

    def _configure_worker_environment(self) -> None:
        from ..launch import configure_lzx
        from ..workers import set_worker_cap

        set_worker_cap(1 if self.single_worker else None)
        configure_lzx(
            choice_enabled=not self.no_lzx,
            force_lzx=self.force_lzx,
            benchmark_ok=self.benchmark_ok,
            disabled_reason='benchmark' if self.default_no_lzx else None,
            announce=False,
        )

    def _send_folder_summary(
        self,
        stats: CompressionStats,
        plan_count: int,
        total_compressible_size: int,
        *,
        directory: str,
        scope: str,
        is_analysis: bool = True,
        analysis_timing: Optional[dict] = None,
    ) -> None:
        self._send(
            FolderSummaryResponse(
                self._make_stats_summary(
                    stats,
                    plan_count,
                    total_compressible_size,
                    is_analysis=is_analysis,
                    analysis_timing=analysis_timing,
                ),
                directory=directory,
                scope=scope,
            )
        )

    def _run_pipeline(self, label: str, action: Callable[[], None]) -> None:
        try:
            action()
        except InterruptedError:
            pass
        except Exception as exc:
            logging.exception("%s error", label)
            self._send(WarningResponse(_("Error"), str(exc)))
        finally:
            self._send(StateResponse("Stopped"))

    def bind_server(self, server: GuiServer):
        self.server = server

    def handle_request(self, request: GuiRequest) -> GuiResponse:
        req_type = getattr(request, "type", "")

        if req_type == "SaveConfig":
            self.min_savings = getattr(request, "min_savings", self.min_savings)
            self.decimal = getattr(request, "decimal", self.decimal)
            self.no_lzx = getattr(request, "no_lzx", self.no_lzx)
            self.force_lzx = getattr(request, "force_lzx", self.force_lzx)
            self.single_worker = getattr(request, "single_worker", self.single_worker)

            return self._current_config_response()

        elif req_type == "ResetConfig":
            self.min_savings = DEFAULT_MIN_SAVINGS_PERCENT
            self.decimal = False
            self.no_lzx = self.default_no_lzx
            self.force_lzx = False
            self.single_worker = False

            return self._current_config_response()

        elif req_type == "StartCompression":
            requested_path = self._requested_path(request)
            if not requested_path and self.last_analysis_plan is None and not self.quick_analysis_results:
                return WarningResponse(_("Warning"), _("No folder selected"))
            if requested_path and requested_path != self.current_folder:
                self._clear_quick_analysis_results()
                self._clear_analysis_state()

            if requested_path:
                self.current_folder = requested_path
            self.min_savings = getattr(request, "min_savings", self.min_savings)
            self.start_worker(self._run_compression)
            return StateResponse("Scanning")

        elif req_type == "AnalyseFolder":
            requested_path = self._requested_path(request)
            if not requested_path:
                return WarningResponse(_("Warning"), _("No folder selected"))
            self._clear_quick_analysis_results()
            if requested_path and requested_path != self.current_folder:
                self._clear_analysis_state()

            self.current_folder = requested_path
            self.start_worker(self._run_analysis)
            return StateResponse("Scanning")

        elif req_type == "GetQuickCompressionTargets":
            return self._make_quick_targets_response()

        elif req_type == "StartQuickCompression":
            compactos = getattr(request, "compactos", False)
            self.start_worker(lambda: self._run_quick_compression(compactos=compactos))
            return StateResponse("Scanning")

        elif req_type == "PauseCompression":
            self.pause_event.set()
            return StateResponse("Paused")

        elif req_type == "ResumeCompression":
            self.pause_event.clear()
            return StateResponse("Resumed")

        elif req_type == "StopCompression":
            self.stop_event.set()
            return StateResponse("Stopped")

        elif req_type == "GetProgressUpdate":
            return StatusResponse("", None)

        return StatusResponse(_("Unknown request"), None)

    def start_worker(self, target: Callable):
        self.stop_event.clear()
        self.pause_event.clear()
        if self.worker_thread and self.worker_thread.is_alive():
            logging.warning("User requested action while worker is already running.")
            return

        self.worker_thread = threading.Thread(target=target, daemon=True)
        self.worker_thread.start()

    def _send(self, response: GuiResponse):
        if self.server:
            self.server.send_response(response)

    def _scale_quick_progress(
        self,
        local_pct: Optional[float],
        dir_index: Optional[int],
        dir_total: Optional[int],
    ) -> Optional[float]:
        if local_pct is not None and local_pct < 0:
            return _PROGRESS_INDETERMINATE
        if not dir_index or not dir_total or dir_total <= 0 or local_pct is None:
            return local_pct
        segment = 100.0 / dir_total
        return ((dir_index - 1) * segment) + (local_pct / 100.0) * segment

    def _send_progress(
        self,
        status: str,
        pct: Optional[float],
        *,
        quick_history: bool = False,
        quick_dir_index: Optional[int] = None,
        quick_dir_total: Optional[int] = None,
        final: bool = False,
    ) -> None:
        self._send(
            ProgressUpdateResponse(
                status,
                self._scale_quick_progress(pct, quick_dir_index, quick_dir_total),
                quick_history=quick_history,
                final=final,
            )
        )

    def _check_phase_progress_pct(self, processed: int, total: int) -> float:
        if total:
            return (
                _PROGRESS_WALK_DONE
                + (processed / total) * (_PROGRESS_CHECK_END - _PROGRESS_WALK_DONE)
            )
        return _PROGRESS_WALK_DONE

    def _send_check_phase_progress(
        self,
        processed: int,
        total: int,
        *,
        scanning_more: bool = False,
        quick_dir_index: Optional[int] = None,
        quick_dir_total: Optional[int] = None,
    ) -> None:
        check_elapsed = max(0.001, time.perf_counter() - self._check_phase_start)
        check_rate = processed / check_elapsed if processed else 0.0
        status = _("Analyzing... {processed}/{total} ({rate:.0f} files/s)").format(
            processed=processed,
            total=total,
            rate=check_rate,
        )
        if scanning_more:
            status += " " + _("(Scanning more files...)")
        self._send_progress(
            status,
            self._check_phase_progress_pct(processed, total),
            quick_dir_index=quick_dir_index,
            quick_dir_total=quick_dir_total,
        )

    def _check_pause_stop(self):
        if self.stop_event.is_set():
            raise InterruptedError("Stopped by user")
            
        if self.pause_event.is_set():
            self._send(StateResponse("Paused"))
            while self.pause_event.is_set():
                if self.stop_event.is_set():
                    raise InterruptedError("Stopped by user")
                time.sleep(0.2)
            self._send(StateResponse("Resumed"))

    def _sync_stats(self, stats: CompressionStats, plan: list):
        plan_count = len(plan)
        total_compressible_size = sum(p[1] for p in plan)
        self._send_folder_summary(
            stats,
            plan_count,
            total_compressible_size,
            directory=str(self.current_folder),
            scope="current",
        )

    def _make_quick_targets_response(self) -> QuickCompressionTargetsResponse:
        targets = resolve_targets()
        return QuickCompressionTargetsResponse(
            directories=[str(directory) for directory in targets.directories],
            allow_compactos=is_admin(),
        )

    def _accumulate_stats(self, target: CompressionStats, source: CompressionStats) -> None:
        target.compressed_files += source.compressed_files
        target.skipped_files += source.skipped_files
        target.already_compressed_files += source.already_compressed_files
        target.total_original_size += source.total_original_size
        target.total_compressed_size += source.total_compressed_size
        target.total_skipped_size += source.total_skipped_size
        target.total_skipped_physical_size += source.total_skipped_physical_size
        target.already_compressed_logical_size += source.already_compressed_logical_size
        target.already_compressed_physical_size += source.already_compressed_physical_size
        target.skip_extension_files += source.skip_extension_files
        target.skip_low_savings_files += source.skip_low_savings_files
        target.errors.extend(source.errors)
        target.entropy_projected_original_bytes += source.entropy_projected_original_bytes or source.total_original_size
        target.entropy_projected_compressed_bytes += source.entropy_projected_compressed_bytes or source.total_compressed_size
        target.entropy_projected_compressed_bytes_conservative += source.entropy_projected_compressed_bytes_conservative or source.total_compressed_size

    def _build_quick_total_timing(
        self,
        walk_seconds: float,
        check_seconds: float,
        entropy_seconds: float,
        total_files: int,
        entropy_files: int,
    ) -> dict:
        walk_seconds = max(0.0, walk_seconds)
        check_seconds = max(0.0, check_seconds)
        combined_scan_seconds = walk_seconds + check_seconds
        return {
            "combined_scan_seconds": combined_scan_seconds,
            "walk_seconds": walk_seconds,
            "walk_rate": (total_files / walk_seconds) if walk_seconds > 0 else 0.0,
            "check_seconds": check_seconds,
            "check_rate": (total_files / check_seconds) if check_seconds > 0 else 0.0,
            "scan_rate": (total_files / combined_scan_seconds) if combined_scan_seconds > 0 else 0.0,
            "entropy_seconds": max(0.0, entropy_seconds),
            "entropy_rate": (entropy_files / entropy_seconds) if entropy_seconds > 0 else 0.0,
        }

    def _build_live_analysis_timing(
        self,
        *,
        walk_seconds: float,
        total_files: int,
        check_seconds: float = 0.0,
        entropy_seconds: float = 0.0,
        entropy_files: int = 0,
    ) -> dict:
        walk_seconds = max(0.0, walk_seconds)
        check_seconds = max(0.0, check_seconds)
        entropy_seconds = max(0.0, entropy_seconds)
        combined_scan_seconds = walk_seconds + check_seconds
        return {
            "combined_scan_seconds": combined_scan_seconds,
            "walk_seconds": walk_seconds,
            "walk_rate": (total_files / walk_seconds) if walk_seconds > 0 else 0.0,
            "check_seconds": check_seconds,
            "check_rate": (total_files / check_seconds) if check_seconds > 0 and total_files > 0 else 0.0,
            "scan_rate": (total_files / combined_scan_seconds) if combined_scan_seconds > 0 else 0.0,
            "entropy_seconds": entropy_seconds,
            "entropy_rate": (entropy_files / entropy_seconds) if entropy_seconds > 0 else 0.0,
        }

    def _apply_dry_run_projection(self, stats: CompressionStats, plan: list[tuple[str, int, str]]) -> None:
        stats.entropy_projected_original_bytes = stats.total_original_size

        entropy_map = {Path(record.path): record for record in stats.entropy_samples}

        projected_compressed_lzx = 0.0
        projected_compressed_xpress = 0.0

        for path_str, size, algo in plan:
            record = entropy_map.get(Path(path_str).parent)
            if record:
                savings_factor = max(0.0, record.estimated_savings / 100.0)
                compressed_size = size * (1.0 - savings_factor)
                projected_compressed_lzx += compressed_size
                conservative_factor = DRY_RUN_CONSERVATIVE_FACTORS.get(algo, 1.06)
                projected_compressed_xpress += compressed_size * conservative_factor
            else:
                projected_compressed_lzx += size
                projected_compressed_xpress += size

        skipped_size = stats.total_compressed_size
        stats.entropy_projected_compressed_bytes = int(round(projected_compressed_lzx + skipped_size))
        stats.entropy_projected_compressed_bytes_conservative = int(round(projected_compressed_xpress + skipped_size))

    def _build_analysis_timing(
        self,
        walk_seconds: float,
        candidate_files: int,
        monitor: PerformanceMonitor,
    ) -> dict:
        walk_seconds = max(0.0, walk_seconds)
        check_seconds = max(0.0, monitor.stats.file_scan_time)
        entropy_seconds = max(0.0, monitor.stats.entropy_analysis_time)
        combined_scan_seconds = walk_seconds + check_seconds
        walk_rate = (candidate_files / walk_seconds) if walk_seconds > 0 else 0.0
        check_rate = (candidate_files / check_seconds) if check_seconds > 0 else 0.0

        return {
            "combined_scan_seconds": combined_scan_seconds,
            "walk_seconds": walk_seconds,
            "walk_rate": walk_rate,
            "check_seconds": check_seconds,
            "check_rate": check_rate,
            "scan_rate": walk_rate,
            "entropy_seconds": entropy_seconds,
            "entropy_rate": (
                monitor.stats.files_analyzed_for_entropy / entropy_seconds
            ) if entropy_seconds > 0 else 0.0,
        }

    def _make_stats_summary(
        self,
        stats: CompressionStats,
        plan_count: int,
        total_compressible_size: int,
        is_analysis: bool = True,
        analysis_timing: Optional[dict] = None,
    ) -> dict:
        already_compressed_files = max(0, stats.already_compressed_files)
        already_compressed_logical_size = max(0, stats.already_compressed_logical_size)
        already_compressed_physical_size = max(0, stats.already_compressed_physical_size)

        excluded_count = max(0, stats.skipped_files - already_compressed_files)
        excluded_logical_size = max(0, stats.total_skipped_size - already_compressed_logical_size)

        if is_analysis:
            current_on_disk_size = already_compressed_physical_size + excluded_logical_size + total_compressible_size

            projected_compressible_size = total_compressible_size
            if stats.entropy_projected_compressed_bytes_conservative > 0:
                projected_compressible_size = max(
                    0,
                    stats.entropy_projected_compressed_bytes_conservative - stats.total_skipped_physical_size,
                )

            projected_on_disk_size = already_compressed_physical_size + excluded_logical_size + projected_compressible_size
            physical_size = projected_on_disk_size
            potential_savings_bytes = max(0, current_on_disk_size - projected_on_disk_size)
            compressed_count = already_compressed_files
            compressed_logical_size = already_compressed_logical_size
            compressed_physical_size = already_compressed_physical_size
            compressible_count = plan_count
            compressible_logical_size = total_compressible_size
            compressible_physical_size = projected_compressible_size
        else:
            current_on_disk_size = max(0, stats.total_compressed_size)
            projected_on_disk_size = current_on_disk_size
            physical_size = current_on_disk_size
            potential_savings_bytes = max(0, stats.total_original_size - current_on_disk_size)
            compressed_count = already_compressed_files
            compressed_logical_size = already_compressed_logical_size
            compressed_physical_size = already_compressed_physical_size
            compressible_count = max(0, stats.compressed_files)
            compressible_logical_size = max(
                0,
                stats.total_original_size - already_compressed_logical_size - excluded_logical_size,
            )
            compressible_physical_size = max(
                0,
                current_on_disk_size - already_compressed_physical_size - excluded_logical_size,
            )

        summary = {
            "logical_size": stats.total_original_size,
            "physical_size": physical_size,
            "current_on_disk_size": current_on_disk_size,
            "projected_on_disk_size": projected_on_disk_size,
            "is_analysis": is_analysis,
            "min_savings_percent": self.min_savings,
            "potential_savings_bytes": potential_savings_bytes,
            "analysis_timing": analysis_timing,
            "compressed": {
                "count": compressed_count,
                "logical_size": compressed_logical_size,
                "physical_size": compressed_physical_size,
            },
            "compressible": {
                "count": compressible_count,
                "logical_size": compressible_logical_size,
                "physical_size": compressible_physical_size,
            },
            "skipped": {
                "count": excluded_count,
                "logical_size": excluded_logical_size,
                "physical_size": excluded_logical_size,
            }
        }
        return summary

    def _run_analysis(self):
        def _pipeline() -> None:
            from ..skip_logic import discard_staged_incompressible_cache

            discard_staged_incompressible_cache()
            self._run_analysis_pipeline()

        self._run_pipeline("Analysis", _pipeline)

    def _run_compression(self):
        self._run_pipeline("Compression", self._run_compression_pipeline)

    def _run_analysis_pipeline(
        self,
        *,
        report_completion: bool = True,
        quick_dir_index: Optional[int] = None,
        quick_dir_total: Optional[int] = None,
    ):
        self._configure_worker_environment()
        progress_kwargs = {
            "quick_dir_index": quick_dir_index,
            "quick_dir_total": quick_dir_total,
        }

        base_dir = Path(self.current_folder).resolve()
        stats = CompressionStats()
        stats.set_base_dir(base_dir)
        stats.min_savings_percent = self.min_savings
        monitor = PerformanceMonitor()
        monitor.start_operation()

        overall_start_time = time.perf_counter()
        self._check_processed = 0
        self._send_progress(_("Scanning directory..."), _PROGRESS_INDETERMINATE, **progress_kwargs)

        discovery = _GuiDiscoveryStream(self, base_dir, stats, progress_kwargs)

        plan_count = 0
        total_compressible_size = 0
        self._check_phase_start = time.perf_counter()
        entropy_phase_start: Optional[float] = None
        last_update_time = self._check_phase_start
        last_summary_update_time = self._check_phase_start

        def _plan_progress(path: Path, processed: int, should_compress: bool, reason: Optional[str], size: int):
            nonlocal plan_count, total_compressible_size, last_update_time, last_summary_update_time
            if should_compress:
                plan_count += 1
                total_compressible_size += size

            discovery.notify_check_progress(processed)
            total_files = max(discovery.count, processed)
            if processed % PLAN_PROGRESS_GRANULARITY != 0 and processed != total_files:
                return
            self._check_pause_stop()

            now = time.perf_counter()
            check_elapsed = max(0.001, now - self._check_phase_start)

            if now - last_update_time > UI_STATUS_INTERVAL_SECONDS or processed == total_files:
                last_update_time = now
                self._send_check_phase_progress(
                    processed,
                    total_files,
                    **progress_kwargs,
                )

            if now - last_summary_update_time > UI_SUMMARY_INTERVAL_SECONDS or processed == total_files:
                last_summary_update_time = now
                self._send_folder_summary(
                    stats,
                    plan_count,
                    total_compressible_size,
                    directory=str(base_dir),
                    scope="current",
                    analysis_timing=self._build_live_analysis_timing(
                        walk_seconds=discovery.walk_seconds,
                        total_files=total_files,
                        check_seconds=check_elapsed,
                    ),
                )

        def _entropy_progress(path: Path, processed: int, total: int):
            nonlocal entropy_phase_start, last_update_time, last_summary_update_time
            if processed % ENTROPY_PROGRESS_GRANULARITY != 0 and processed != total:
                return

            self._check_pause_stop()
            now = time.perf_counter()
            if entropy_phase_start is None:
                entropy_phase_start = now
            entropy_elapsed = max(0.001, now - entropy_phase_start)

            if now - last_update_time > UI_STATUS_INTERVAL_SECONDS or processed == total:
                last_update_time = now
                local_pct = (
                    _PROGRESS_CHECK_END
                    + (processed / total) * (_PROGRESS_ENTROPY_END - _PROGRESS_CHECK_END)
                    if total
                    else _PROGRESS_CHECK_END
                )
                self._send_progress(
                    _("Sampling entropy... {processed}/{total}").format(
                        processed=processed,
                        total=total,
                    ),
                    local_pct,
                    **progress_kwargs,
                )

            if now - last_summary_update_time > UI_SUMMARY_INTERVAL_SECONDS or processed == total:
                last_summary_update_time = now
                self._send_folder_summary(
                    stats,
                    plan_count,
                    total_compressible_size,
                    directory=str(base_dir),
                    scope="current",
                    analysis_timing=self._build_live_analysis_timing(
                        walk_seconds=discovery.walk_seconds,
                        total_files=max(discovery.count, processed),
                        check_seconds=max(0.0, monitor.stats.file_scan_time),
                        entropy_seconds=entropy_elapsed,
                        entropy_files=processed,
                    ),
                )

        discovery.enter_check_phase()
        self._send_progress(_("Analyzing files..."), _PROGRESS_WALK_DONE, **progress_kwargs)

        plan = plan_compression(
            discovery,
            stats,
            monitor,
            base_dir=base_dir,
            min_savings_percent=self.min_savings,
            verbosity=0,
            progress_callback=_plan_progress,
            entropy_progress_callback=_entropy_progress,
            debug_scan_all=False,
        )

        total_files = discovery.count
        walk_seconds = discovery.walk_seconds

        if total_files == 0:
            self.last_analysis_plan = []
            self.last_analysis_stats = stats
            self.last_analysis_monitor = monitor
            monitor.end_operation()
            self.last_analysis_timing = self._build_analysis_timing(walk_seconds, 0, monitor)
            self._send_folder_summary(
                stats,
                0,
                0,
                directory=str(base_dir),
                scope="current",
                analysis_timing=self.last_analysis_timing,
            )
            return

        monitor.stats.total_files = total_files

        self.last_analysis_plan = plan
        self.last_analysis_stats = stats
        self.last_analysis_monitor = monitor

        self._apply_dry_run_projection(stats, plan)
        monitor.end_operation()
        self.last_analysis_timing = self._build_analysis_timing(walk_seconds, total_files, monitor)

        if report_completion:
            analysis_elapsed = max(0.001, time.perf_counter() - overall_start_time)
            self._send_progress(
                _("Analysis complete in {elapsed:.2f}s").format(elapsed=analysis_elapsed),
                100.0,
                **progress_kwargs,
            )
        self._send_folder_summary(
            stats,
            len(plan),
            sum(p[1] for p in plan),
            directory=str(base_dir),
            scope="current",
            analysis_timing=self.last_analysis_timing,
        )

    def _run_quick_compression(self, compactos: bool = False):
        self._run_pipeline("Quick compression", lambda: self._run_quick_compression_pipeline(compactos=compactos))

    def _run_compression_pipeline(self):
        self._configure_worker_environment()

        if self.quick_analysis_results:
            self._run_quick_analysis_compression_pipeline()
            return
        
        if not hasattr(self, 'last_analysis_plan') or self.last_analysis_plan is None:
            self._send(WarningResponse("Warning", "Please analyze the folder before compressing."))
            return
            
        plan = self.last_analysis_plan
        stats = self.last_analysis_stats
        monitor = self.last_analysis_monitor

        if not plan:
            self._send(ProgressUpdateResponse(_("Nothing to compress!"), 100.0))
            return

        self._send(StateResponse("Compacting"))
        
        total_to_compress = len(plan)
        total_compressible_size = sum(p[1] for p in plan)
        compressed_count = [0]
        
        exec_start_time = time.perf_counter()
        last_exec_update_time = exec_start_time
        
        def _exec_start(algo: str, total: int):
            pass
            
        def _exec_progress(path: Path, algo: str):
            compressed_count[0] += 1
            if compressed_count[0] % COMPRESSION_PROGRESS_GRANULARITY != 0 and compressed_count[0] != total_to_compress:
                return

            self._check_pause_stop()
            nonlocal last_exec_update_time
            now = time.perf_counter()
            if now - last_exec_update_time > 0.1 or compressed_count[0] == total_to_compress:
                last_exec_update_time = now
                pct = 60.0 + (compressed_count[0] / total_to_compress) * 40.0
                elapsed = max(0.001, now - exec_start_time)
                rate = compressed_count[0] / elapsed
                self._send(
                    ProgressUpdateResponse(
                        _("Compressing... {compressed}/{total} ({rate:.0f} files/s)").format(
                            compressed=compressed_count[0],
                            total=total_to_compress,
                            rate=rate,
                        ),
                        pct,
                    )
                )
                self._send_folder_summary(
                    stats,
                    total_to_compress,
                    total_compressible_size,
                    directory=str(self.current_folder),
                    scope="current",
                    is_analysis=False,
                )

        with monitor.time_compression():
            execute_compression_plan(
                plan,
                stats,
                monitor,
                verbosity=0,
                xp_workers=xp_worker_count(),
                lzx_workers=lzx_worker_count(),
                stage_callback=_exec_start,
                progress_callback=_exec_progress,
            )

        commit_incompressible_cache()
        self.last_analysis_plan = None
        self.last_analysis_stats = None

        self._send_progress(
            _("Complete!"),
            100.0,
            final=True,
        )
        self._send_folder_summary(
            stats,
            stats.compressed_files,
            total_compressible_size,
            directory=str(self.current_folder),
            scope="current",
            is_analysis=False,
        )

    def _run_quick_analysis_compression_pipeline(self) -> None:
        entries = list(self.quick_analysis_results)
        if not entries:
            self._send(WarningResponse(_("Warning"), _("Please run quick analysis before compressing.")))
            return

        total_to_compress = sum(len(entry.get("plan") or []) for entry in entries)
        if total_to_compress <= 0:
            self._send(ProgressUpdateResponse(_("Nothing to compress!"), 100.0))
            return

        self._send(StateResponse("Compacting"))

        total_stats = CompressionStats()
        total_stats.min_savings_percent = self.min_savings
        total_target_size = 0

        compressed_count = [0]
        exec_start_time = time.perf_counter()

        for index, entry in enumerate(entries, start=1):
            self._check_pause_stop()

            directory = str(entry.get("directory") or "")
            plan = entry.get("plan") or []
            stats = entry.get("stats")
            monitor = entry.get("monitor")

            if not directory or stats is None or monitor is None:
                continue

            if not plan:
                self._send(
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

            self.current_folder = directory
            directory_target_size = sum(item[1] for item in plan)
            total_target_size += directory_target_size
            self._send(
                StatusResponse(
                    _("Quick compression: compressing {directory} ({index}/{total})").format(
                        directory=directory,
                        index=index,
                        total=len(entries),
                    ),
                    None,
                )
            )

            def _exec_start(algo: str, total: int):
                pass

            def _exec_progress(path: Path, algo: str):
                compressed_count[0] += 1
                if compressed_count[0] % COMPRESSION_PROGRESS_GRANULARITY != 0 and compressed_count[0] != total_to_compress:
                    return

                self._check_pause_stop()
                now = time.perf_counter()
                elapsed = max(0.001, now - exec_start_time)
                rate = compressed_count[0] / elapsed
                pct = (compressed_count[0] / total_to_compress) * 100.0
                self._send_progress(
                    _("Quick compressing... {compressed}/{total} ({rate:.0f} files/s)").format(
                        compressed=compressed_count[0],
                        total=total_to_compress,
                        rate=rate,
                    ),
                    pct,
                )
                self._send_folder_summary(
                    stats,
                    len(plan),
                    directory_target_size,
                    directory=directory,
                    scope="directory",
                    is_analysis=False,
                )

            with monitor.time_compression():
                execute_compression_plan(
                    plan,
                    stats,
                    monitor,
                    verbosity=0,
                    xp_workers=xp_worker_count(),
                    lzx_workers=lzx_worker_count(),
                    stage_callback=_exec_start,
                    progress_callback=_exec_progress,
                )

            self._accumulate_stats(total_stats, stats)

            self._send_folder_summary(
                stats,
                len(plan),
                directory_target_size,
                directory=directory,
                scope="directory",
                is_analysis=False,
            )
            self._send_folder_summary(
                total_stats,
                total_stats.compressed_files,
                total_target_size,
                directory="Total",
                scope="total",
                is_analysis=False,
            )

        commit_incompressible_cache()
        self.last_analysis_plan = None
        self.last_analysis_stats = None
        self.quick_analysis_results = []
        self._send_progress(
            _("Quick compression complete"),
            100.0,
            quick_history=True,
            final=True,
        )

    def _run_quick_compression_pipeline(self, compactos: bool = False):
        self._configure_worker_environment()

        targets = list(resolve_targets().directories)
        if not targets:
            self.quick_analysis_results = []
            self._send(WarningResponse(_("Warning"), _("No default quick-compression targets were found on this system.")))
            return

        compactos_thread = None
        compactos_result = {"success": False, "output": ""}
        if compactos and is_admin():
            self._send(CompactOSIndicatorResponse(show=True, text=_("Compressing Windows binaries...")))

            def _run_compactos():
                def _progress(s, p=None):
                    self._send(CompactOSIndicatorResponse(show=True, text=s))
                success, output, parsed = run_compactos_hidden(
                    progress_callback=_progress,
                    line_callback=_progress,
                )
                compactos_result["success"] = success
                compactos_result["output"] = output
                saved = int(parsed.get("saved_bytes") or 0) if isinstance(parsed, dict) else 0
                msg = _("Done compressing Windows binaries.") if success else _("CompactOS compression failed.")
                self._send(CompactOSIndicatorResponse(show=True, text=msg, done=success, saved_bytes=saved))
            compactos_thread = threading.Thread(target=_run_compactos, daemon=True)
            compactos_thread.start()

        quick_results: list[dict[str, Any]] = []
        total_analysis_stats = CompressionStats()
        total_analysis_stats.min_savings_percent = self.min_savings
        total_analysis_plan_count = 0
        total_analysis_compressible_size = 0

        total_walk_seconds = 0.0
        total_check_seconds = 0.0
        total_entropy_seconds = 0.0
        total_scanned_files = 0
        total_entropy_files = 0

        for index, directory in enumerate(targets, start=1):
            self._check_pause_stop()
            self.current_folder = str(directory)
            self._send_progress(
                _("Quick analysis: scanning {directory} ({index}/{total})").format(
                    directory=directory,
                    index=index,
                    total=len(targets),
                ),
                _PROGRESS_INDETERMINATE,
                quick_dir_index=index,
                quick_dir_total=len(targets),
            )
            self._run_analysis_pipeline(
                report_completion=False,
                quick_dir_index=index,
                quick_dir_total=len(targets),
            )

            current_stats = self.last_analysis_stats
            current_plan = self.last_analysis_plan or []
            current_monitor = self.last_analysis_monitor
            current_timing = self.last_analysis_timing or {}

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

            self._accumulate_stats(total_analysis_stats, current_stats)
            total_analysis_plan_count += len(current_plan)
            total_analysis_compressible_size += current_plan_size
            total_walk_seconds += float(current_timing.get("walk_seconds", 0.0) or 0.0)
            total_check_seconds += float(current_timing.get("check_seconds", 0.0) or 0.0)
            total_entropy_seconds += float(current_timing.get("entropy_seconds", 0.0) or 0.0)
            total_scanned_files += int(getattr(current_monitor.stats, "total_files", 0) or 0)
            total_entropy_files += int(getattr(current_monitor.stats, "files_analyzed_for_entropy", 0) or 0)

            total_summary = self._make_stats_summary(
                total_analysis_stats,
                total_analysis_plan_count,
                total_analysis_compressible_size,
                analysis_timing=self._build_quick_total_timing(
                    total_walk_seconds,
                    total_check_seconds,
                    total_entropy_seconds,
                    total_scanned_files,
                    total_entropy_files,
                ),
            )

            current_summary = self._make_stats_summary(
                current_stats,
                len(current_plan),
                current_plan_size,
                analysis_timing=current_timing,
            )
            self._send(FolderSummaryResponse(current_summary, directory=str(directory), scope="directory"))
            self._send(FolderSummaryResponse(total_summary, directory="Total", scope="total"))

        self.quick_analysis_results = quick_results
        self._send_progress(
            _("Scanning complete"),
            100.0,
            quick_history=True,
        )

        if compactos_thread is not None:
            compactos_thread.join()

def run_gui(benchmark_ok: Optional[bool] = None):
    backend = GuiBackend(benchmark_ok=benchmark_ok)
    app = create_gui_app(backend.handle_request)
    app.initial_config = json.loads(backend._current_config_response().to_json())
    backend.bind_server(app)
    app.start()
    print(_("Exiting..."), flush=True)

