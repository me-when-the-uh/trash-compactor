import logging
import threading
import time
from pathlib import Path
from typing import Optional, Dict, Any, Callable

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
    ResetConfigRequest
)
from .webview_server import create_gui_app, GuiServer

# Trash-compactor core imports
from ..compression.compression_executor import execute_compression_plan
from ..compression.compression_planner import iter_files, plan_compression
from ..stats import CompressionStats
from ..timer import PerformanceMonitor
from ..workers import lzx_worker_count, xp_worker_count
from ..config import DEFAULT_MIN_SAVINGS_PERCENT, DRY_RUN_CONSERVATIVE_FACTORS
from ..skip_logic import commit_incompressible_cache


class GuiBackend:
    def __init__(self):
        self.server: Optional[GuiServer] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        
        # State
        self.min_savings = DEFAULT_MIN_SAVINGS_PERCENT
        self.current_folder = ""
        self.decimal = False
        self.no_lzx = False
        self.force_lzx = False
        self.single_worker = False

        self.last_analysis_plan = None
        self.last_analysis_stats = None
        self.last_analysis_monitor = None
        self.last_analysis_timing = None

    def bind_server(self, server: GuiServer):
        self.server = server

    def handle_request(self, request: GuiRequest) -> GuiResponse:
        req_type = getattr(request, "type", "")
        
        # Requests that can be returned immediately
        if req_type == "SaveConfig":
            self.min_savings = getattr(request, "min_savings", self.min_savings)
            self.decimal = getattr(request, "decimal", self.decimal)
            self.no_lzx = getattr(request, "no_lzx", self.no_lzx)
            self.force_lzx = getattr(request, "force_lzx", self.force_lzx)
            self.single_worker = getattr(request, "single_worker", self.single_worker)
            
            return ConfigResponse(
                decimal=self.decimal, 
                min_savings=self.min_savings,
                no_lzx=self.no_lzx,
                force_lzx=self.force_lzx,
                single_worker=self.single_worker
            )
            
        elif req_type == "ResetConfig":
            self.min_savings = DEFAULT_MIN_SAVINGS_PERCENT
            self.decimal = False
            self.no_lzx = False
            self.force_lzx = False
            self.single_worker = False
            return ConfigResponse(
                decimal=self.decimal, 
                min_savings=self.min_savings,
                no_lzx=self.no_lzx,
                force_lzx=self.force_lzx,
                single_worker=self.single_worker
            )
            
        elif req_type == "StartCompression":
            requested_path = getattr(request, "path", self.current_folder)
            if requested_path and requested_path != self.current_folder:
                self.last_analysis_plan = None
                self.last_analysis_stats = None
                self.last_analysis_monitor = None
                self.last_analysis_timing = None

            self.current_folder = requested_path
            self.min_savings = getattr(request, "min_savings", self.min_savings)
            self.start_worker(self._run_compression)
            return StateResponse("Scanning")
            
        elif req_type == "AnalyseFolder":
            requested_path = getattr(request, "path", self.current_folder)
            if requested_path and requested_path != self.current_folder:
                self.last_analysis_plan = None
                self.last_analysis_stats = None
                self.last_analysis_monitor = None
                self.last_analysis_timing = None

            self.current_folder = requested_path
            self.start_worker(self._run_analysis)
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
            # JS polls this; we can just return empty and let push mechanisms handle it, 
            # or return the latest known status. We return generic OK since we push updates.
            return StatusResponse("", None)

        return StatusResponse("Unknown request", None)

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
        self._send(FolderSummaryResponse(self._make_stats_summary(stats, plan_count, total_compressible_size)))

    def _apply_dry_run_projection(self, stats: CompressionStats, plan: list[tuple[Path, int, str]]) -> None:
        stats.entropy_projected_original_bytes = stats.total_original_size

        entropy_map = {Path(record.path): record for record in stats.entropy_samples}

        projected_compressed_lzx = 0.0
        projected_compressed_xpress = 0.0

        for path, size, algo in plan:
            record = entropy_map.get(path.parent)
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

    def _build_analysis_timing(self, discovery_seconds: float, candidate_files: int, monitor: PerformanceMonitor) -> dict:
        file_scan_seconds = max(0.0, monitor.stats.file_scan_time)
        entropy_seconds = max(0.0, monitor.stats.entropy_analysis_time)
        total_seconds = max(0.0, monitor.stats.total_time)

        return {
            "discovery_seconds": discovery_seconds,
            "file_scan_seconds": file_scan_seconds,
            "entropy_seconds": entropy_seconds,
            "total_seconds": total_seconds,
            "candidate_files": candidate_files,
            "discovery_rate": (candidate_files / discovery_seconds) if discovery_seconds > 0 else 0.0,
            "file_scan_rate": (candidate_files / file_scan_seconds) if file_scan_seconds > 0 else 0.0,
            "entropy_rate": (monitor.stats.files_analyzed_for_entropy / entropy_seconds) if entropy_seconds > 0 else 0.0,
        }

    def _make_stats_summary(
        self,
        stats: CompressionStats,
        plan_count: int,
        total_compressible_size: int,
        is_analysis: bool = True,
        analysis_timing: Optional[dict] = None,
    ) -> dict:
        current_physical_size = stats.total_compressed_size + total_compressible_size
        projected_physical_size = (
            stats.entropy_projected_compressed_bytes_conservative
            if stats.entropy_projected_compressed_bytes_conservative > 0
            else current_physical_size
        )
        physical_size = projected_physical_size if is_analysis else current_physical_size

        potential_savings_bytes = 0
        if is_analysis and stats.total_original_size > 0:
            potential_savings_bytes = max(0, stats.total_original_size - physical_size)

        projected_compressible_size = max(0, physical_size - stats.total_compressed_size)

        summary = {
            "logical_size": stats.total_original_size,
            "physical_size": physical_size,
            "is_analysis": is_analysis,
            "potential_savings_bytes": potential_savings_bytes,
            "analysis_timing": analysis_timing,
            "compressed": {
                "count": stats.compressed_files,
                "logical_size": 0, # Not perfectly tracked
                "physical_size": stats.total_compressed_size,
            },
            "compressible": {
                "count": plan_count,
                "logical_size": total_compressible_size,
                "physical_size": projected_compressible_size if is_analysis else total_compressible_size,
            },
            "skipped": {
                "count": stats.skipped_files,
                "logical_size": stats.total_skipped_size,
                "physical_size": stats.total_skipped_size,
            }
        }
        return summary

    def _run_analysis(self):
        try:
            from ..skip_logic import discard_staged_incompressible_cache
            discard_staged_incompressible_cache()
            
            self._run_analysis_pipeline()
            self._send(StateResponse("Stopped"))
        except InterruptedError:
            self._send(StateResponse("Stopped"))
        except Exception as e:
            logging.exception("Analysis error")
            self._send(WarningResponse("Error", str(e)))
            self._send(StateResponse("Stopped"))

    def _run_compression(self):
        try:
            self._run_compression_pipeline()
            self._send(StateResponse("Stopped"))
        except InterruptedError:
            self._send(StateResponse("Stopped"))
        except Exception as e:
            logging.exception("Compression error")
            self._send(WarningResponse("Error", str(e)))
            self._send(StateResponse("Stopped"))

    def _run_analysis_pipeline(self):
        from ..workers import set_worker_cap
        from ..launch import configure_lzx
        
        set_worker_cap(1 if self.single_worker else None)
        configure_lzx(choice_enabled=not self.no_lzx, force_lzx=self.force_lzx, benchmark_ok=None)
        
        base_dir = Path(self.current_folder).resolve()
        stats = CompressionStats()
        stats.set_base_dir(base_dir)
        stats.min_savings_percent = self.min_savings
        monitor = PerformanceMonitor()
        monitor.start_operation()

        # Phase 1: Discover files
        overall_start_time = time.perf_counter()
        self._send(StatusResponse("Scanning directory...", 0.0))
        all_files = []
        
        scan_start_time = time.perf_counter()
        last_scan_update_time = scan_start_time

        for entry in iter_files(base_dir, stats, 0, self.min_savings, collect_entropy=False):
            all_files.append(entry)

            if len(all_files) % 256 == 0:
                self._check_pause_stop()

            now = time.perf_counter()
            if now - last_scan_update_time > 0.25:
                last_scan_update_time = now
                elapsed = max(0.001, now - scan_start_time)
                rate = len(all_files) / elapsed
                self._send(StatusResponse(f"Scanning directory... {len(all_files)} files found ({rate:.0f} files/s)", None))

        discovery_seconds = max(0.001, time.perf_counter() - scan_start_time)

        total_files = len(all_files)
        if total_files == 0:
            self.last_analysis_plan = []
            self.last_analysis_stats = stats
            self.last_analysis_monitor = monitor
            monitor.end_operation()
            self.last_analysis_timing = self._build_analysis_timing(discovery_seconds, 0, monitor)
            self._send(
                FolderSummaryResponse(
                    self._make_stats_summary(stats, 0, 0, analysis_timing=self.last_analysis_timing)
                )
            )
            return

        # Phase 2: Plan Compression
        self._send(StatusResponse("Analyzing file entropy...", 0.0))
        
        plan_count = 0
        total_compressible_size = 0
        analysis_start_time = time.perf_counter()
        last_update_time = analysis_start_time
        last_summary_update_time = analysis_start_time

        def _plan_progress(path: Path, processed: int, should_compress: bool, reason: Optional[str], size: int):
            nonlocal plan_count, total_compressible_size, last_update_time, last_summary_update_time
            if should_compress:
                plan_count += 1
                total_compressible_size += size

            if processed % 128 != 0 and processed != total_files:
                return
            self._check_pause_stop()
            
            now = time.perf_counter()
            # Update UI at most 4 times per second.
            if now - last_update_time > 0.25 or processed == total_files:
                last_update_time = now
                pct = (processed / total_files) * 60.0 # 0-60%
                elapsed = max(0.001, now - analysis_start_time)
                rate = processed / elapsed
                self._send(ProgressUpdateResponse(f"Analyzing... {processed}/{total_files} ({rate:.0f} files/s)", pct))

            if now - last_summary_update_time > 0.5 or processed == total_files:
                last_summary_update_time = now
                self._send(FolderSummaryResponse(self._make_stats_summary(stats, plan_count, total_compressible_size)))

        # Just adapting the entropy wrapper
        def _entropy_progress(path: Path, processed: int, total: int):
            nonlocal last_update_time
            if processed % 16 != 0 and processed != total:
                return

            self._check_pause_stop()
            now = time.perf_counter()
            if now - last_update_time > 0.25 or processed == total:
                last_update_time = now
                # Don't update pct here, just the status text
                self._send(ProgressUpdateResponse(f"Sampling entropy... {processed}/{total}"))

        plan = plan_compression(
            all_files,
            stats,
            monitor,
            base_dir=base_dir,
            min_savings_percent=self.min_savings,
            verbosity=0,
            progress_callback=_plan_progress,
            entropy_progress_callback=_entropy_progress,
            debug_scan_all=False,
        )
        
        self.last_analysis_plan = plan
        self.last_analysis_stats = stats
        self.last_analysis_monitor = monitor

        self._apply_dry_run_projection(stats, plan)
        monitor.end_operation()
        self.last_analysis_timing = self._build_analysis_timing(discovery_seconds, total_files, monitor)

        analysis_elapsed = max(0.001, time.perf_counter() - overall_start_time)
        self._send(
            ProgressUpdateResponse(
                "Analysis complete in "
                f"{analysis_elapsed:.2f}s "
                f"(discover {discovery_seconds:.2f}s, scan {monitor.stats.file_scan_time:.2f}s, entropy {monitor.stats.entropy_analysis_time:.2f}s)",
                100.0,
            )
        )
        self._send(
            FolderSummaryResponse(
                self._make_stats_summary(
                    stats,
                    len(plan),
                    sum(p[1] for p in plan),
                    analysis_timing=self.last_analysis_timing,
                )
            )
        )

    def _run_compression_pipeline(self):
        from ..workers import set_worker_cap
        from ..launch import configure_lzx
        
        set_worker_cap(1 if self.single_worker else None)
        configure_lzx(choice_enabled=not self.no_lzx, force_lzx=self.force_lzx, benchmark_ok=None)
        
        if not hasattr(self, 'last_analysis_plan') or self.last_analysis_plan is None:
            self._send(WarningResponse("Warning", "Please analyze the folder before compressing."))
            return
            
        plan = self.last_analysis_plan
        stats = self.last_analysis_stats
        monitor = self.last_analysis_monitor

        if not plan:
            self._send(ProgressUpdateResponse("Nothing to compress!", 100.0))
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
            if compressed_count[0] % 10 != 0 and compressed_count[0] != total_to_compress:
                return

            self._check_pause_stop()
            nonlocal last_exec_update_time
            now = time.perf_counter()
            if now - last_exec_update_time > 0.1 or compressed_count[0] == total_to_compress:
                last_exec_update_time = now
                pct = 60.0 + (compressed_count[0] / total_to_compress) * 40.0
                elapsed = max(0.001, now - exec_start_time)
                rate = compressed_count[0] / elapsed
                self._send(ProgressUpdateResponse(f"Compressing... {compressed_count[0]}/{total_to_compress} ({rate:.0f} files/s)", pct))
                self._send(FolderSummaryResponse(self._make_stats_summary(stats, total_to_compress, total_compressible_size, is_analysis=False)))

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
        # Reset plan to force re-analysis if attempted again in same session
        self.last_analysis_plan = None
        self.last_analysis_stats = None
        
        # Final summary
        self._send(ProgressUpdateResponse("Complete!", 100.0))
        self._send(FolderSummaryResponse(self._make_stats_summary(stats, 0, 0, is_analysis=False))) # Plan is depleted

def run_gui():
    # Pre-run benchmark before Webview loop consumes UI threads
    from ..benchmark import run_benchmark
    run_benchmark()

    backend = GuiBackend()
    app = create_gui_app(backend.handle_request)
    backend.bind_server(app)
    app.start()

