import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from ..config import DEFAULT_MIN_SAVINGS_PERCENT
from ..i18n import _
from ..stats import CompressionStats
from .handlers import dispatch_request
from .message_types import (
    ConfigResponse,
    FolderSummaryResponse,
    GuiRequest,
    GuiResponse,
    ProgressUpdateResponse,
    StateResponse,
    WarningResponse,
)
from .pipelines.analysis import run_analysis_pipeline
from .pipelines.compression import run_compression_pipeline
from .pipelines.quick import run_quick_compression_pipeline
from .progress import scale_quick_progress, scan_progress_percent
from .summary import make_stats_summary
from .webview_server import GuiServer, create_gui_app


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
        self.hdd_mode = False
        self._user_overrode_single_worker = False  # set when user explicitly toggles/saves differing from auto-detect
        self.lzx_warning = _("It is recommended to disable LZX compression for this system.") if self.default_no_lzx else ""

        self.last_analysis_plan = None
        self.last_analysis_stats = None
        self.last_analysis_monitor = None
        self.last_analysis_timing = None
        self.quick_analysis_results: list[dict[str, Any]] = []
        self._cached_validation_path = ""
        self._cached_volume_details = None

    def _current_config_response(self) -> ConfigResponse:
        return ConfigResponse(
            decimal=self.decimal,
            min_savings=self.min_savings,
            no_lzx=self.no_lzx,
            force_lzx=self.force_lzx,
            single_worker=self.single_worker,
            hdd_mode=self.hdd_mode,
            lzx_warning=self.lzx_warning,
        )

    def _mark_single_worker_override(self, value: bool) -> None:
        self._user_overrode_single_worker = True
        self.single_worker = value

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
        from ..workers import set_hdd_mode, set_worker_cap

        set_worker_cap(1 if self.single_worker else None)
        set_hdd_mode(self.hdd_mode)
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
                make_stats_summary(
                    stats,
                    plan_count,
                    total_compressible_size,
                    min_savings_percent=self.min_savings,
                    is_analysis=is_analysis,
                    analysis_timing=analysis_timing,
                    lzx_enabled=not self.no_lzx,
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
        return dispatch_request(self, request)

    def start_worker(self, target: Callable) -> bool:
        thread = self.worker_thread
        if thread and thread.is_alive():
            if not self.stop_event.is_set():
                logging.warning("Worker already running.")
                return False
            logging.warning("Waiting for prior worker to finish after stop.")
            thread.join(timeout=1.0)
            if thread.is_alive():
                logging.error("Prior worker did not exit in time.")
                return False

        self.stop_event.clear()
        self.pause_event.clear()
        self.worker_thread = threading.Thread(target=target, daemon=True)
        self.worker_thread.start()
        return True

    def _send(self, response: GuiResponse):
        if self.server:
            self.server.send_response(response)

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
                scale_quick_progress(pct, quick_dir_index, quick_dir_total),
                quick_history=quick_history,
                final=final,
            )
        )

    def _send_check_phase_progress(
        self,
        processed: int,
        total: int,
        *,
        scanning_more: bool = False,
        quick_dir_index: Optional[int] = None,
        quick_dir_total: Optional[int] = None,
    ) -> None:
        status = _("Analysing... {processed}/{total}").format(
            processed=processed,
            total=total,
        )
        if scanning_more:
            status += " " + _("(Scanning more files...)")
        self._send_progress(
            status,
            scan_progress_percent(total),
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

    def _run_analysis(self):
        def _pipeline() -> None:
            from ..skip_logic import discard_staged_incompressible_cache

            discard_staged_incompressible_cache()
            run_analysis_pipeline(self)

        self._run_pipeline("Analysis", _pipeline)

    def _run_compression(self):
        self._run_pipeline("Compression", lambda: run_compression_pipeline(self))

    def _run_quick_compression(self, compactos: bool = False):
        self._run_pipeline("Quick compression", lambda: run_quick_compression_pipeline(self, compactos=compactos))

    def _discard_quick_pipeline_state(self) -> None:
        self._clear_quick_analysis_results()
        self._clear_analysis_state()


def run_gui(benchmark_ok: Optional[bool] = None):
    backend = GuiBackend(benchmark_ok=benchmark_ok)
    app = create_gui_app(backend.handle_request)
    app.initial_config = json.loads(backend._current_config_response().to_json())
    backend.bind_server(app)
    app.start()
    print(_("Exiting..."), flush=True)