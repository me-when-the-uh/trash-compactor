"""
Message types for GUI <-> Backend IPC communication.
JSON-serializable dataclasses matching Compactor's request/response pattern.
"""

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass
class FolderSummary:
    """Summary statistics for a folder analysis."""
    logical_size: int = 0
    physical_size: int = 0
    compressed_count: int = 0
    compressed_logical: int = 0
    compressed_physical: int = 0
    compressible_count: int = 0
    compressible_logical: int = 0
    compressible_physical: int = 0
    skipped_count: int = 0
    skipped_logical: int = 0
    skipped_physical: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logical_size": self.logical_size,
            "physical_size": self.physical_size,
            "compressed": {
                "count": self.compressed_count,
                "logical_size": self.compressed_logical,
                "physical_size": self.compressed_physical,
            },
            "compressible": {
                "count": self.compressible_count,
                "logical_size": self.compressible_logical,
                "physical_size": self.compressible_physical,
            },
            "skipped": {
                "count": self.skipped_count,
                "logical_size": self.skipped_logical,
                "physical_size": self.skipped_physical,
            },
        }


@dataclass
class GuiRequest:
    """Base class for all GUI -> Backend requests."""
    type: str = field(init=False, default="")

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class GuiResponse:
    """Base class for all Backend -> GUI responses."""
    type: str = field(init=False, default="")

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class SelectFolderRequest(GuiRequest):
    type: str = field(init=False, default="SelectFolder")


@dataclass
class StartCompressionRequest(GuiRequest):
    type: str = field(init=False, default="StartCompression")
    path: str = ""
    min_savings: float = 18.0


@dataclass
class PauseCompressionRequest(GuiRequest):
    type: str = field(init=False, default="PauseCompression")


@dataclass
class ResumeCompressionRequest(GuiRequest):
    type: str = field(init=False, default="ResumeCompression")


@dataclass
class StopCompressionRequest(GuiRequest):
    type: str = field(init=False, default="StopCompression")


@dataclass
class AnalyseFolderRequest(GuiRequest):
    type: str = field(init=False, default="AnalyseFolder")
    path: str = ""


@dataclass
class GetQuickCompressionTargetsRequest(GuiRequest):
    type: str = field(init=False, default="GetQuickCompressionTargets")


@dataclass
class StartQuickCompressionRequest(GuiRequest):
    type: str = field(init=False, default="StartQuickCompression")


@dataclass
class GetProgressUpdateRequest(GuiRequest):
    type: str = field(init=False, default="GetProgressUpdate")


@dataclass
class SaveConfigRequest(GuiRequest):
    type: str = field(init=False, default="SaveConfig")
    decimal: bool = False
    min_savings: float = 18.0
    no_lzx: bool = False
    force_lzx: bool = False
    single_worker: bool = False


@dataclass
class ResetConfigRequest(GuiRequest):
    type: str = field(init=False, default="ResetConfig")


@dataclass
class ChooseFolderRequest(GuiRequest):
    type: str = field(init=False, default="ChooseFolder")


@dataclass
class OpenUrlRequest(GuiRequest):
    type: str = field(init=False, default="OpenUrl")
    url: str = ""


@dataclass
class ConfigResponse(GuiResponse):
    type: str = field(init=False, default="Config")
    decimal: bool = False
    min_savings: float = 18.0
    no_lzx: bool = False
    force_lzx: bool = False
    single_worker: bool = False
    lzx_warning: str = ""


@dataclass
class FolderResponse(GuiResponse):
    type: str = field(init=False, default="Folder")
    path: str = ""


@dataclass
class StatusResponse(GuiResponse):
    type: str = field(init=False, default="Status")
    status: str = ""
    pct: Optional[float] = None


@dataclass
class FolderSummaryResponse(GuiResponse):
    type: str = field(init=False, default="FolderSummary")
    info: Dict[str, Any] = field(default_factory=dict)
    directory: str = ""
    scope: str = ""


@dataclass
class QuickCompressionTargetsResponse(GuiResponse):
    type: str = field(init=False, default="QuickCompressionTargets")
    directories: list[str] = field(default_factory=list)
    allow_compactos: bool = False


@dataclass
class ProgressUpdateResponse(GuiResponse):
    type: str = field(init=False, default="ProgressUpdate")
    status: str = ""
    pct: Optional[float] = None
    quick_history: bool = False


@dataclass
class StateResponse(GuiResponse):
    """Generic state change (Paused, Resumed, Stopped, Scanning, Compacting)."""
    type: str = "State"

    def __init__(self, state: str = "State"):
        self.type = state


@dataclass
class WarningResponse(GuiResponse):
    type: str = field(init=False, default="Warning")
    title: str = ""
    message: str = ""


def parse_request(data: str) -> Optional[GuiRequest]:
    """Parse JSON request string into appropriate GuiRequest subclass."""
    try:
        obj = json.loads(data)
        req_type = obj.get("type")

        if req_type == "SelectFolder":
            return SelectFolderRequest()
        elif req_type == "StartCompression":
            return StartCompressionRequest(
                path=obj.get("path", ""),
                min_savings=obj.get("min_savings", 18.0),
            )
        elif req_type == "PauseCompression":
            return PauseCompressionRequest()
        elif req_type == "ResumeCompression":
            return ResumeCompressionRequest()
        elif req_type == "StopCompression":
            return StopCompressionRequest()
        elif req_type == "AnalyseFolder":
            return AnalyseFolderRequest(path=obj.get("path", ""))
        elif req_type == "GetQuickCompressionTargets":
            return GetQuickCompressionTargetsRequest()
        elif req_type == "StartQuickCompression":
            return StartQuickCompressionRequest()
        elif req_type == "GetProgressUpdate":
            return GetProgressUpdateRequest()
        elif req_type == "SaveConfig":
            return SaveConfigRequest(
                decimal=obj.get("decimal", False),
                min_savings=float(obj.get("min_savings", 18.0)),
                no_lzx=obj.get("no_lzx", False),
                force_lzx=obj.get("force_lzx", False),
                single_worker=obj.get("single_worker", False),
            )
        elif req_type == "ResetConfig":
            return ResetConfigRequest()
        elif req_type == "ChooseFolder":
            return ChooseFolderRequest()
        elif req_type == "OpenUrl":
            return OpenUrlRequest(url=obj.get("url", ""))
    except Exception:
        pass

    return None
