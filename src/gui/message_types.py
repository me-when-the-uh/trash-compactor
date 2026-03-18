"""
Message types for GUI <-> Backend IPC communication.
JSON-serializable dataclasses matching Compactor's request/response pattern.
"""

from dataclasses import dataclass, asdict
from typing import Optional, Dict, Any
import json


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
    type: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class GuiResponse:
    """Base class for all Backend -> GUI responses."""
    type: str

    def to_json(self) -> str:
        return json.dumps(asdict(self))


# Request Types

@dataclass
class SelectFolderRequest(GuiRequest):
    type: str = "SelectFolder"


@dataclass
class StartCompressionRequest(GuiRequest):
    type: str = "StartCompression"
    path: str = ""
    min_savings: float = 18.0

    def __init__(self, path: str = "", min_savings: float = 18.0):
        self.type = "StartCompression"
        self.path = path
        self.min_savings = min_savings


@dataclass
class PauseCompressionRequest(GuiRequest):
    type: str = "PauseCompression"

    def __init__(self):
        self.type = "PauseCompression"


@dataclass
class ResumeCompressionRequest(GuiRequest):
    type: str = "ResumeCompression"

    def __init__(self):
        self.type = "ResumeCompression"


@dataclass
class StopCompressionRequest(GuiRequest):
    type: str = "StopCompression"

    def __init__(self):
        self.type = "StopCompression"


@dataclass
class AnalyseFolderRequest(GuiRequest):
    type: str = "AnalyseFolder"
    path: str = ""

    def __init__(self, path: str = ""):
        self.type = "AnalyseFolder"
        self.path = path


@dataclass
class GetProgressUpdateRequest(GuiRequest):
    type: str = "GetProgressUpdate"

    def __init__(self):
        self.type = "GetProgressUpdate"


@dataclass
class SaveConfigRequest(GuiRequest):
    type: str = "SaveConfig"
    decimal: bool = False
    min_savings: float = 18.0
    no_lzx: bool = False
    force_lzx: bool = False
    single_worker: bool = False

    def __init__(self, decimal: bool = False, min_savings: float = 18.0,
                 no_lzx: bool = False, force_lzx: bool = False, single_worker: bool = False):
        self.type = "SaveConfig"
        self.decimal = decimal
        self.min_savings = min_savings
        self.no_lzx = no_lzx
        self.force_lzx = force_lzx
        self.single_worker = single_worker


@dataclass
class ResetConfigRequest(GuiRequest):
    type: str = "ResetConfig"

    def __init__(self):
        self.type = "ResetConfig"


@dataclass
class ChooseFolderRequest(GuiRequest):
    type: str = "ChooseFolder"

    def __init__(self):
        self.type = "ChooseFolder"


@dataclass
class OpenUrlRequest(GuiRequest):
    type: str = "OpenUrl"
    url: str = ""

    def __init__(self, url: str = ""):
        self.type = "OpenUrl"
        self.url = url


# Response Types

@dataclass
class ConfigResponse(GuiResponse):
    type: str = "Config"
    decimal: bool = False
    min_savings: float = 18.0
    no_lzx: bool = False
    force_lzx: bool = False
    single_worker: bool = False

    def __init__(self, decimal: bool = False, min_savings: float = 18.0,
                 no_lzx: bool = False, force_lzx: bool = False, single_worker: bool = False):
        self.type = "Config"
        self.decimal = decimal
        self.min_savings = min_savings
        self.no_lzx = no_lzx
        self.force_lzx = force_lzx
        self.single_worker = single_worker


@dataclass
class FolderResponse(GuiResponse):
    type: str = "Folder"
    path: str = ""

    def __init__(self, path: str = ""):
        self.type = "Folder"
        self.path = path


@dataclass
class StatusResponse(GuiResponse):
    type: str = "Status"
    status: str = ""
    pct: Optional[float] = None

    def __init__(self, status: str = "", pct: Optional[float] = None):
        self.type = "Status"
        self.status = status
        self.pct = pct


@dataclass
class FolderSummaryResponse(GuiResponse):
    type: str = "FolderSummary"
    info: Dict[str, Any] = None

    def __init__(self, info: Optional[Dict[str, Any]] = None):
        self.type = "FolderSummary"
        self.info = info or {}


@dataclass
class ProgressUpdateResponse(GuiResponse):
    type: str = "ProgressUpdate"
    status: str = ""
    pct: Optional[float] = None

    def __init__(self, status: str = "", pct: Optional[float] = None):
        self.type = "ProgressUpdate"
        self.status = status
        self.pct = pct


@dataclass
class StateResponse(GuiResponse):
    """Generic state change (Paused, Resumed, Stopped, Scanning, Compacting)."""
    type: str = "State"

    def __init__(self, state: str = "State"):
        self.type = state


@dataclass
class WarningResponse(GuiResponse):
    type: str = "Warning"
    title: str = ""
    message: str = ""

    def __init__(self, title: str = "", message: str = ""):
        self.type = "Warning"
        self.title = title
        self.message = message


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
