"""IPC bridge between the HTML frontend and the compression backend."""

import logging
import os
import json
from pathlib import Path
from typing import Optional, Callable, Any, Dict
import webbrowser

from .bridge import GuiBridge

try:
    import webview as pywebview  # type: ignore[reportMissingImports]
except ImportError:
    pywebview = None

from .message_types import (
    GuiRequest, GuiResponse,
    ConfigResponse, StatusResponse,
    FolderSummaryResponse, ProgressUpdateResponse, StateResponse,
    WarningResponse, StartCompressionRequest,
    PauseCompressionRequest, ResumeCompressionRequest, StopCompressionRequest,
    AnalyseFolderRequest, SaveConfigRequest, ResetConfigRequest,
    GetQuickCompressionTargetsRequest, StartQuickCompressionRequest,
    StartDecompressionRequest,
    GetExclusionsRequest, AddExclusionRequest, RemoveExclusionRequest,
)
from ..i18n import _, get_current_locale, get_translations


def _inline_json(value: Any) -> str:
    """JSON that is safe inside a <script> element."""
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


class GuiApi:
    """API exposed to JavaScript via pywebview."""

    def __init__(self, backend_handler: Callable):
        self.backend_handler = backend_handler
        self.current_folder = ""
        self._window: Optional[Any] = None

    def _pick_folder(self) -> Optional[str]:
        if self._window is None:
            return None
        result = self._window.create_file_dialog(pywebview.FileDialog.FOLDER)
        if not result:
            return None
        return result[0]

    def choose_folder(self) -> Dict[str, Any]:
        try:
            folder = self._pick_folder()

            if folder:
                self.current_folder = folder
                return {"type": "Folder", "path": folder}
            return {"type": "Error", "message": _("No folder selected")}
        except Exception as e:
            logging.exception("Error choosing folder: %s", e)
            return {"type": "Error", "message": str(e)}

    def start_compression(self) -> Dict[str, Any]:
        req = StartCompressionRequest(path=self.current_folder)
        return self.backend_handler(req)

    def pause_compression(self) -> Dict[str, Any]:
        req = PauseCompressionRequest()
        return self.backend_handler(req)

    def resume_compression(self) -> Dict[str, Any]:
        req = ResumeCompressionRequest()
        return self.backend_handler(req)

    def stop_compression(self) -> Dict[str, Any]:
        req = StopCompressionRequest()
        return self.backend_handler(req)

    def analyse_folder(self) -> Dict[str, Any]:
        req = AnalyseFolderRequest(path=self.current_folder)
        return self.backend_handler(req)

    def get_quick_compression_targets(self) -> Dict[str, Any]:
        req = GetQuickCompressionTargetsRequest()
        return self.backend_handler(req)

    def start_quick_compression(self, compactos: bool = False) -> Dict[str, Any]:
        req = StartQuickCompressionRequest(compactos=compactos)
        return self.backend_handler(req)

    def start_decompression(self, path: str = "", add_exclusion: bool = True) -> Dict[str, Any]:
        folder = path or self.current_folder
        req = StartDecompressionRequest(path=folder, add_exclusion=add_exclusion)
        return self.backend_handler(req)

    def choose_decompress_folder(self, add_exclusion: bool = True) -> Dict[str, Any]:
        try:
            folder = self._pick_folder()
            if not folder:
                return {"type": "Error", "message": _("No folder selected")}
            self.current_folder = folder
            started = self.start_decompression(folder, add_exclusion)
            if started.get("type") in {"Warning", "Error"}:
                return started
            return {"type": "Folder", "path": folder}
        except Exception as exc:
            logging.exception("Error choosing decompress folder: %s", exc)
            return {"type": "Error", "message": str(exc)}

    def save_config(self, config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        if config is None:
            config = kwargs
        req = SaveConfigRequest(
            decimal=config.get("decimal", False),
            min_savings=float(config.get("min_savings", 15.0)),
            no_lzx=config.get("no_lzx", False),
            force_lzx=config.get("force_lzx", False),
            single_worker=config.get("single_worker", False)
        )
        return self.backend_handler(req)

    def reset_config(self) -> Dict[str, Any]:
        req = ResetConfigRequest()
        return self.backend_handler(req)

    def get_exclusions(self) -> Dict[str, Any]:
        req = GetExclusionsRequest()
        return self.backend_handler(req)

    def add_exclusion(self, path: str) -> Dict[str, Any]:
        req = AddExclusionRequest(path=path or "")
        return self.backend_handler(req)

    def remove_exclusion(self, path: str) -> Dict[str, Any]:
        req = RemoveExclusionRequest(path=path or "")
        return self.backend_handler(req)

    def choose_exclusion_folder(self) -> Dict[str, Any]:
        try:
            folder = self._pick_folder()

            if folder:
                return self.add_exclusion(folder)
            return self.get_exclusions()
        except Exception as exc:
            logging.exception("Error choosing exclusion folder: %s", exc)
            return {"type": "Error", "message": str(exc)}

    def open_url(self, url: str) -> Dict[str, Any]:
        """Open an http(s) URL in the default browser."""
        if not isinstance(url, str) or not url.lower().startswith(("http://", "https://")):
            return {"type": "Error", "message": _("Unsupported link")}
        try:
            webbrowser.open(url)
            return {"type": "Success"}
        except Exception as e:
            logging.exception("Error opening URL: %s", e)
            return {"type": "Error", "message": str(e)}


class GuiServer:
    """Manages the webview GUI server and message routing."""

    def __init__(self, request_handler: Callable[[GuiRequest], GuiResponse]):
        self.request_handler = request_handler
        self.api = None
        self.window = None
        self.running = False
        self.initial_config: Dict[str, Any] = {}
        self._bridge = GuiBridge()

    def _handle_request(self, request: GuiRequest) -> Dict[str, Any]:
        """Call backend handler and convert response to dict."""
        try:
            response = self.request_handler(request)
            if isinstance(response, GuiResponse):
                return json.loads(response.to_json())
            return response if isinstance(response, dict) else {"type": "Error"}
        except Exception as e:
            logging.exception("Error handling request: %s", e)
            return {"type": "Error", "message": str(e)}

    def start(self, folder: Optional[str] = None) -> None:
        if not pywebview:
            logging.error("pywebview not installed. Install with: pip install pywebview")
            return

        self.api = GuiApi(self._handle_request)
        if folder:
            self.api.current_folder = folder

        ui_path = Path(__file__).parent / "ui"
        html_file = ui_path / "index.html"

        if not html_file.exists():
            logging.error("UI files not found at %s", ui_path)
            return

        with open(ui_path / "style.css", "r", encoding="utf-8") as f:
            style = f.read()
        with open(ui_path / "jquery.min.js", "r", encoding="utf-8") as f:
            jquery = f.read()
        with open(ui_path / "app.js", "r", encoding="utf-8") as f:
            script = f.read()
        with open(html_file, "r", encoding="utf-8") as f:
            html = f.read()

        html = html.replace("/*__STYLE__*/", style)
        html = html.replace(
            "/*__I18N__*/",
            _inline_json(
                {
                    "locale": get_current_locale(),
                    "translations": get_translations(),
                }
            ),
        )
        html = html.replace(
            "/*__BOOT_CONFIG__*/",
            _inline_json(getattr(self, "initial_config", {}) or {}),
        )
        html = html.replace("/*__JQUERY__*/", jquery)
        html = html.replace("/*__SCRIPT__*/", script)

        try:
            self.window = pywebview.create_window(
                "Trash Compactor",
                html=html,
                js_api=self.api,
                width=880,
                height=650,
                min_size=(760, 600),
                resizable=True,
                text_select=False,
                background_color="#3d3d3d",
            )
            self.running = True
            self.api._window = self.window
            self._bridge.start(self._emit_batch)
            # Prefer the native Windows backend. Forcing CEF requires an extra
            # cefpython3 runtime that is not bundled in our one-file build.
            debug = os.environ.get("TRASH_COMPACTOR_GUI_DEBUG") == "1"
            try:
                pywebview.start(debug=debug, gui="edgechromium")
            finally:
                self.stop()
        except Exception as e:
            logging.exception("Error starting GUI: %s", e)
            self.stop()

    def stop(self) -> None:
        self.running = False
        self._bridge.stop()

    def _emit_batch(self, items: list) -> None:
        if not self.window or not items:
            return
        payload = json.dumps(items, ensure_ascii=True)
        self.window.evaluate_js(f"Response.batch({payload})")

    def send_response(self, response: GuiResponse) -> None:
        """Queue a response for the GUI pump, without touching the webview."""
        if not self.running:
            return
        self._bridge.enqueue(response)


def create_gui_app(request_handler: Callable[[GuiRequest], GuiResponse]) -> GuiServer:
    return GuiServer(request_handler)
