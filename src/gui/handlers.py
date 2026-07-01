from typing import TYPE_CHECKING, Callable

from ..config import DEFAULT_MIN_SAVINGS_PERCENT
from ..file_utils import is_admin
from ..i18n import _
from ..one_click import resolve_targets
from .message_types import (
    GuiRequest,
    GuiResponse,
    QuickCompressionTargetsResponse,
    StateResponse,
    StatusResponse,
    WarningResponse,
)

if TYPE_CHECKING:
    from .backend import GuiBackend


def _save_config(backend: "GuiBackend", request: GuiRequest) -> GuiResponse:
    backend.min_savings = getattr(request, "min_savings", backend.min_savings)
    backend.decimal = getattr(request, "decimal", backend.decimal)
    backend.no_lzx = getattr(request, "no_lzx", backend.no_lzx)
    backend.force_lzx = getattr(request, "force_lzx", backend.force_lzx)
    backend.single_worker = getattr(request, "single_worker", backend.single_worker)
    return backend._current_config_response()


def _reset_config(backend: "GuiBackend", _request: GuiRequest) -> GuiResponse:
    backend.min_savings = DEFAULT_MIN_SAVINGS_PERCENT
    backend.decimal = False
    backend.no_lzx = backend.default_no_lzx
    backend.force_lzx = False
    backend.single_worker = False
    return backend._current_config_response()


def _start_compression(backend: "GuiBackend", request: GuiRequest) -> GuiResponse:
    requested_path = backend._requested_path(request)
    if not requested_path and backend.last_analysis_plan is None and not backend.quick_analysis_results:
        return WarningResponse(_("Warning"), _("No folder selected"))
    if requested_path and requested_path != backend.current_folder:
        backend._clear_quick_analysis_results()
        backend._clear_analysis_state()

    if requested_path:
        backend.current_folder = requested_path
    backend.min_savings = getattr(request, "min_savings", backend.min_savings)
    if not backend.start_worker(backend._run_compression):
        return WarningResponse(_("Warning"), _("Could not start; wait for the current task to finish."))
    return StateResponse("Scanning")


def _analyse_folder(backend: "GuiBackend", request: GuiRequest) -> GuiResponse:
    requested_path = backend._requested_path(request)
    if not requested_path:
        return WarningResponse(_("Warning"), _("No folder selected"))
    backend._clear_quick_analysis_results()
    if requested_path and requested_path != backend.current_folder:
        backend._clear_analysis_state()

    backend.current_folder = requested_path
    if not backend.start_worker(backend._run_analysis):
        return WarningResponse(_("Warning"), _("Could not start; wait for the current task to finish."))
    return StateResponse("Scanning")


def _quick_targets(_backend: "GuiBackend", _request: GuiRequest) -> GuiResponse:
    targets = resolve_targets()
    return QuickCompressionTargetsResponse(
        directories=[str(directory) for directory in targets.directories],
        allow_compactos=is_admin(),
    )


def _start_quick_compression(backend: "GuiBackend", request: GuiRequest) -> GuiResponse:
    compactos = getattr(request, "compactos", False)
    backend._clear_quick_analysis_results()
    backend._clear_analysis_state()
    if not backend.start_worker(lambda: backend._run_quick_compression(compactos=compactos)):
        return WarningResponse(_("Warning"), _("Could not start; wait for the current task to finish."))
    return StateResponse("Scanning")


def _pause(backend: "GuiBackend", _request: GuiRequest) -> GuiResponse:
    backend.pause_event.set()
    return StateResponse("Paused")


def _resume(backend: "GuiBackend", _request: GuiRequest) -> GuiResponse:
    backend.pause_event.clear()
    return StateResponse("Resumed")


def _stop(backend: "GuiBackend", _request: GuiRequest) -> GuiResponse:
    backend.stop_event.set()
    return StateResponse("Stopped")


def _progress_update(_backend: "GuiBackend", _request: GuiRequest) -> GuiResponse:
    return StatusResponse("", None)


_DISPATCH: dict[str, Callable[["GuiBackend", GuiRequest], GuiResponse]] = {
    "SaveConfig": _save_config,
    "ResetConfig": _reset_config,
    "StartCompression": _start_compression,
    "AnalyseFolder": _analyse_folder,
    "GetQuickCompressionTargets": _quick_targets,
    "StartQuickCompression": _start_quick_compression,
    "PauseCompression": _pause,
    "ResumeCompression": _resume,
    "StopCompression": _stop,
    "GetProgressUpdate": _progress_update,
}


def dispatch_request(backend: "GuiBackend", request: GuiRequest) -> GuiResponse:
    handler = _DISPATCH.get(getattr(request, "type", ""))
    if handler is None:
        return StatusResponse(_("Unknown request"), None)
    return handler(backend, request)