"""
Webview-based GUI server for trash-compactor.
Handles IPC between HTML/JS frontend and Python compression backend.
"""

import logging
import threading
import queue
import json
from pathlib import Path
from typing import Optional, Callable, Any, Dict
import tkinter as tk
from tkinter import filedialog
import webbrowser

try:
    import webview as pywebview  # type: ignore[reportMissingImports]
except ImportError:
    pywebview = None

from .message_types import (
    GuiRequest, GuiResponse, parse_request,
    ConfigResponse, FolderResponse, StatusResponse,
    FolderSummaryResponse, ProgressUpdateResponse, StateResponse,
    WarningResponse, SelectFolderRequest, StartCompressionRequest,
    PauseCompressionRequest, ResumeCompressionRequest, StopCompressionRequest,
    AnalyseFolderRequest, SaveConfigRequest, ResetConfigRequest, GetProgressUpdateRequest,
    ChooseFolderRequest, OpenUrlRequest
)


class GuiApi:
    """API exposed to JavaScript via pywebview."""

    def __init__(self, backend_handler: Callable):
        self.backend_handler = backend_handler
        self.current_folder = ""

    def choose_folder(self) -> Dict[str, Any]:
        """Show folder picker dialog."""
        try:
            root = tk.Tk()
            root.withdraw()
            folder = filedialog.askdirectory(title="Select folder to compress")
            root.destroy()

            if folder:
                self.current_folder = folder
                return {"type": "Folder", "path": folder}
            return {"type": "Error", "message": "No folder selected"}
        except Exception as e:
            logging.exception("Error choosing folder: %s", e)
            return {"type": "Error", "message": str(e)}

    def start_compression(self) -> Dict[str, Any]:
        """Start compression on current folder."""
        if not self.current_folder:
            return {"type": "Error", "message": "No folder selected"}

        req = StartCompressionRequest(path=self.current_folder)
        return self.backend_handler(req)

    def pause_compression(self) -> Dict[str, Any]:
        """Pause ongoing compression."""
        req = PauseCompressionRequest()
        return self.backend_handler(req)

    def resume_compression(self) -> Dict[str, Any]:
        """Resume paused compression."""
        req = ResumeCompressionRequest()
        return self.backend_handler(req)

    def stop_compression(self) -> Dict[str, Any]:
        """Stop ongoing compression."""
        req = StopCompressionRequest()
        return self.backend_handler(req)

    def analyse_folder(self) -> Dict[str, Any]:
        """Analyse current folder for compression opportunities."""
        if not self.current_folder:
            return {"type": "Error", "message": "No folder selected"}

        req = AnalyseFolderRequest(path=self.current_folder)
        return self.backend_handler(req)

    def get_progress_update(self) -> Dict[str, Any]:
        """Get current progress update."""
        req = GetProgressUpdateRequest()
        return self.backend_handler(req)

    def save_config(self, config: Dict[str, Any] = None, **kwargs) -> Dict[str, Any]:
        """Save configuration."""
        if config is None:
            config = kwargs
        req = SaveConfigRequest(
            decimal=config.get("decimal", False),
            min_savings=float(config.get("min_savings", 18.0)),
            no_lzx=config.get("no_lzx", False),
            force_lzx=config.get("force_lzx", False),
            single_worker=config.get("single_worker", False)
        )
        return self.backend_handler(req)

    def reset_config(self) -> Dict[str, Any]:
        """Reset configuration to defaults."""
        req = ResetConfigRequest()
        return self.backend_handler(req)

    def open_url(self, url: str) -> Dict[str, Any]:
        """Open URL in default browser."""
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
        """Start the GUI server."""
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

        # Load UI files
        with open(ui_path / "style.css", "r", encoding="utf-8") as f:
            style = f.read()
        with open(ui_path / "app.js", "r", encoding="utf-8") as f:
            script = f.read()
        with open(html_file, "r", encoding="utf-8") as f:
            html = f.read()

        # Replace placeholders
        html = html.replace("/*__STYLE__*/", style)
        html = html.replace("/*__SCRIPT__*/", script)

        try:
            self.window = pywebview.create_window(
                "Trash Compactor",
                html=html,
                js_api=self.api,
                width=800,
                height=600,
                background_color="#3d3d3d",
            )
            self.running = True
            # Prefer the native Windows backend. Forcing CEF requires an extra
            # cefpython3 runtime that is not bundled in our one-file build.
            pywebview.start(debug=False, gui="edgechromium")
        except Exception as e:
            logging.exception("Error starting GUI: %s", e)

    def stop(self) -> None:
        """Stop the GUI server."""
        self.running = False
        if self.window:
            try:
                self.window.expose()
            except Exception:
                pass

    def send_response(self, response: GuiResponse) -> None:
        """Send response to GUI (if window exists)."""
        if self.window:
            try:
                # pywebview evaluate_js to call javascript
                # Important: we use json.dumps to safely escape the structure.
                json_str = response.to_json()
                self.window.evaluate_js(f"Response.dispatch({json_str})")
            except Exception as e:
                logging.debug("Could not send response to GUI: %s", e)


def create_gui_app(request_handler: Callable[[GuiRequest], GuiResponse]) -> GuiServer:
    """Create and configure the GUI server."""
    return GuiServer(request_handler)
