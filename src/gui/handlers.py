from typing import TYPE_CHECKING, Callable, Optional

from ..config import DEFAULT_MIN_SAVINGS_PERCENT
from ..drive_inspector import DRIVE_REMOTE, VolumeDetails, get_volume_details_fast
from ..file_utils import describe_protected_path, is_admin, sanitize_path
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


def _cached_volume_details(backend: "GuiBackend", directory: str) -> VolumeDetails:
    candidate = sanitize_path(directory)
    if candidate == backend._cached_validation_path and backend._cached_volume_details is not None:
        return backend._cached_volume_details

    details = get_volume_details_fast(candidate)
    backend._cached_validation_path = candidate
    backend._cached_volume_details = details
    return details


def _validate_target_path(backend: "GuiBackend", directory: str) -> Optional[GuiResponse]:
    candidate = sanitize_path(directory)
    if not candidate:
        return WarningResponse(_("Warning"), _("No folder selected"))

    protection_reason = describe_protected_path(candidate)
    if protection_reason:
        return WarningResponse(_("Warning"), _("Cannot compress protected path: {reason}").format(reason=protection_reason))

    details = _cached_volume_details(backend, candidate)
    if details.anchor is None:
        return WarningResponse(_("Warning"), _("Unable to resolve the target volume. Please verify the path."))

    if details.drive_type == DRIVE_REMOTE:
        return WarningResponse(_("Warning"), _("Network shares are not supported targets for compression."))

    if details.filesystem and details.filesystem != "NTFS":
        return WarningResponse(
            _("Warning"),
            _("Windows compression requires NTFS. Detected filesystem: {filesystem}").format(
                filesystem=details.filesystem or "unknown"
            ),
        )

    return None


def _start_compression(backend: "GuiBackend", request: GuiRequest) -> GuiResponse:
    requested_path = backend._requested_path(request)
    if not requested_path and backend.last_analysis_plan is None and not backend.quick_analysis_results:
        return WarningResponse(_("Warning"), _("No folder selected"))

    if not backend.quick_analysis_results:
        if requested_path:
            validation = _validate_target_path(backend, requested_path)
            if validation is not None:
                return validation

        if requested_path and requested_path != backend.current_folder:
            backend._clear_quick_analysis_results()
            backend._clear_analysis_state()

    if requested_path:
        backend.current_folder = requested_path

    min_savings = getattr(request, "min_savings", None)
    if min_savings is not None:
        backend.min_savings = min_savings

    if not backend.start_worker(backend._run_compression):
        return WarningResponse(_("Warning"), _("Could not start; wait for the current task to finish."))
    return StateResponse("Scanning")


def _analyse_folder(backend: "GuiBackend", request: GuiRequest) -> GuiResponse:
    requested_path = backend._requested_path(request)
    validation = _validate_target_path(backend, requested_path)
    if validation is not None:
        return validation

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