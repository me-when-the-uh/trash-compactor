import bisect
import ctypes
import logging
import os
import stat
import subprocess
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from .i18n import _
from .config import DEFAULT_EXCLUDE_DIRECTORIES, MIN_COMPRESSIBLE_SIZE, SIZE_THRESHOLDS, SKIP_EXTENSIONS
from .drive_inspector import DRIVE_FIXED, DRIVE_REMOTE, is_hard_drive, get_volume_details


def sanitize_path(path: str) -> str:
    return os.path.normpath(path.strip(" '\""))


def is_admin() -> bool:
    try:
        return os.getuid() == 0
    except AttributeError:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())


def hide_console_window() -> None:
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE = 0
    except (AttributeError, OSError):
        # If console window is unavailable
        pass


def _normalize_for_compare(path: str | Path) -> str:
    normalized = os.path.normcase(os.path.normpath(str(path)))
    if len(normalized) == 2 and normalized[1] == ':':
        return normalized + os.sep
    return normalized


_DEFAULT_EXCLUDE_MAP: dict[str, str] = {
    _normalize_for_compare(candidate): os.path.normpath(candidate)
    for candidate in DEFAULT_EXCLUDE_DIRECTORIES
}


def _match_exclusion(normalized: str) -> tuple[bool, Optional[str]]:
    for excluded_norm, display in _DEFAULT_EXCLUDE_MAP.items():
        if normalized == excluded_norm:
            return True, _("Protected system directory ({display})").format(display=display)
        prefix = excluded_norm + os.sep
        if normalized.startswith(prefix):
            return True, _("Within protected system directory ({display})").format(display=display)
    return False, None


_SIZE_BREAKS, _SIZE_LABELS = zip(*SIZE_THRESHOLDS)
from .drive_inspector import KERNEL32

_FILE_ATTRIBUTE_COMPRESSED = getattr(stat, 'FILE_ATTRIBUTE_COMPRESSED', 0x800)

_GET_COMPRESSED_FILE_SIZE = KERNEL32.GetCompressedFileSizeW
_GET_COMPRESSED_FILE_SIZE.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_GET_COMPRESSED_FILE_SIZE.restype = wintypes.DWORD

def get_ntfs_compressed_size(file_path: str | Path) -> int:
    high = wintypes.DWORD()
    low = _GET_COMPRESSED_FILE_SIZE(str(file_path), ctypes.byref(high))
    if low == 0xFFFFFFFF:
        error = ctypes.get_last_error()
        if error:
            raise ctypes.WinError(error)
    return (high.value << 32) + low

@dataclass(frozen=True)
class DirectoryDecision:
    skip: bool
    reason: str = ""

    @property
    def allow(self) -> bool:
        return not self.skip

    @classmethod
    def deny(cls, reason: str) -> "DirectoryDecision":
        return cls(True, reason)

    @classmethod
    def allow_path(cls) -> "DirectoryDecision":
        return cls(False, "")


@dataclass(frozen=True)
class CompressionDecision:
    should_compress: bool
    reason: str
    size_hint: int = 0
    category: str = "generic"
    already_compressed: bool = False

    @classmethod
    def allow(cls, size_hint: int) -> "CompressionDecision":
        return cls(True, _("File eligible for compression"), size_hint, "eligible", False)

    @classmethod
    def deny(
        cls,
        reason: str,
        size_hint: int = 0,
        category: str = "generic",
        already_compressed: bool = False,
    ) -> "CompressionDecision":
        return cls(False, reason, size_hint, category, already_compressed)


def get_size_category(file_size: int) -> str:
    index = bisect.bisect_right(_SIZE_BREAKS, file_size)
    return _SIZE_LABELS[index] if index < len(_SIZE_LABELS) else 'large'


def should_skip_directory(directory: Path) -> DirectoryDecision:
    normalized = _normalize_for_compare(directory)
    match, reason = _match_exclusion(normalized)
    if match:
        return DirectoryDecision.deny(reason or _("Protected system directory"))
    return DirectoryDecision.allow_path()


def is_protected_path(path: str | Path) -> bool:
    normalized = _normalize_for_compare(path)
    match, _ = _match_exclusion(normalized)
    return match


def get_protection_reason(path: str | Path) -> Optional[str]:
    normalized = _normalize_for_compare(path)
    _, reason = _match_exclusion(normalized)
    return reason


def describe_protected_path(directory: str) -> Optional[str]:
    return get_protection_reason(directory)


def is_file_compressed(
    file_path: str | Path, 
    *,
    actual_size: Optional[int] = None,
    attributes: Optional[int] = None,
) -> tuple[bool, int]:
    if actual_size is None or attributes is None:
        try:
            stat_info = os.stat(file_path)
            actual_size = stat_info.st_size
            attributes = stat_info.st_file_attributes
        except OSError as exc:
            logging.error("Failed to get actual file size for %s: %s", file_path, exc)
            return False, 0

    try:
        compressed_size = get_ntfs_compressed_size(file_path)
    except OSError as exc:
        logging.error("Failed to get compressed size for %s: %s", file_path, exc)
        return False, actual_size

    if compressed_size < actual_size:
        return True, compressed_size

    if attributes & _FILE_ATTRIBUTE_COMPRESSED:
        return True, compressed_size

    return False, compressed_size


def should_compress_file(
    file_path: str | Path,
    *,
    file_size: Optional[int] = None,
    attributes: Optional[int] = None,
    ignore_extensions: bool = False,
    check_already_compressed: bool = True,
) -> CompressionDecision:
    if isinstance(file_path, str):
        suffix = os.path.splitext(file_path)[1].lower()
    else:
        suffix = file_path.suffix.lower()

    if not ignore_extensions and suffix in SKIP_EXTENSIONS:
        return CompressionDecision.deny(
            _("Skipped due to extension {suffix}").format(suffix=suffix),
            category="extension",
        )

    try:
        resolved_size = file_size if file_size is not None else os.stat(file_path).st_size
    except OSError as exc:
        logging.error("Failed to stat %s: %s", file_path, exc)
        return CompressionDecision.deny(
            _("Unable to read file size: {exc}").format(exc=exc),
            category="error",
        )

    if resolved_size < MIN_COMPRESSIBLE_SIZE:
        return CompressionDecision.deny(
            _("File too small ({size} bytes)").format(size=resolved_size),
            resolved_size,
            category="too_small",
        )

    if check_already_compressed:
        is_compressed, compressed_size = is_file_compressed(
            file_path, actual_size=resolved_size, attributes=attributes
        )
        if is_compressed:
            return CompressionDecision.deny(
                _("File is already compressed"),
                compressed_size,
                category="already_compressed",
                already_compressed=True,
            )

    return CompressionDecision.allow(resolved_size)


