"""
GUI module for trash-compactor using webview.
Provides a cross-platform graphical interface for compression management.
"""

from .webview_server import GuiServer, create_gui_app, GuiApi
from .message_types import (
    GuiRequest, GuiResponse, parse_request,
    ConfigResponse, FolderResponse, StatusResponse,
    FolderSummaryResponse, ProgressUpdateResponse, StateResponse, WarningResponse
)

__all__ = [
    "GuiServer",
    "create_gui_app",
    "GuiApi",
    "GuiRequest",
    "GuiResponse",
    "parse_request",
    "ConfigResponse",
    "FolderResponse",
    "StatusResponse",
    "FolderSummaryResponse",
    "ProgressUpdateResponse",
    "StateResponse",
    "WarningResponse",
]
