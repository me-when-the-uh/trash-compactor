import bisect
import ctypes
import logging
import os
import subprocess
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from .config import DEFAULT_EXCLUDE_DIRECTORIES, MIN_COMPRESSIBLE_SIZE, SIZE_THRESHOLDS, SKIP_EXTENSIONS
from .drive_inspector import DRIVE_FIXED, DRIVE_REMOTE, is_hard_drive, get_volume_details


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
            return True, f"Protected system directory ({display})"
        prefix = excluded_norm + os.sep
        if normalized.startswith(prefix):
            return True, f"Within protected system directory ({display})"
    return False, None


_SIZE_BREAKS, _SIZE_LABELS = zip(*SIZE_THRESHOLDS)
from .drive_inspector import KERNEL32

_GET_COMPRESSED_FILE_SIZE = KERNEL32.GetCompressedFileSizeW
_GET_COMPRESSED_FILE_SIZE.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_GET_COMPRESSED_FILE_SIZE.restype = wintypes.DWORD

def get_ntfs_compressed_size(file_path: Path) -> int:
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

    @classmethod
    def allow(cls, size_hint: int) -> "CompressionDecision":
        return cls(True, "File eligible for compression", size_hint)

    @classmethod
    def deny(cls, reason: str, size_hint: int = 0) -> "CompressionDecision":
        return cls(False, reason, size_hint)


def get_size_category(file_size: int) -> str:
    index = bisect.bisect_right(_SIZE_BREAKS, file_size)
    return _SIZE_LABELS[index] if index < len(_SIZE_LABELS) else 'large'


def should_skip_directory(directory: Path) -> DirectoryDecision:
    normalized = _normalize_for_compare(directory)
    match, reason = _match_exclusion(normalized)
    if match:
        return DirectoryDecision.deny(reason or "Protected system directory")
    return DirectoryDecision.allow_path()


def is_protected_path(path: str | Path) -> bool:
    normalized = _normalize_for_compare(path)
    match, _ = _match_exclusion(normalized)
    return match


def get_protection_reason(path: str | Path) -> Optional[str]:
    normalized = _normalize_for_compare(path)
    _, reason = _match_exclusion(normalized)
    return reason


def _hidden_startupinfo() -> subprocess.STARTUPINFO:
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def check_compression_with_compact(file_path: Path) -> bool:
    try:
        command = f'compact /a "{file_path}"'
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            startupinfo=_hidden_startupinfo(),
            shell=True,
            text=True,
        )
        if result.returncode != 0:
            return False
        return "0 are not" in result.stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logging.error("Failed to check compression with compact: %s", exc)
        return False


def is_file_compressed(file_path: Path, thorough_check: bool = False) -> tuple[bool, int]:
    try:
        actual_size = file_path.stat().st_size
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

    if thorough_check and compressed_size == actual_size:
        if check_compression_with_compact(file_path):
            logging.debug("File %s detected as compressed by compact command", file_path)
            return True, compressed_size

    return False, compressed_size


def should_compress_file(file_path: Path, thorough_check: bool = False) -> CompressionDecision:
    suffix = file_path.suffix.lower()
    if suffix in SKIP_EXTENSIONS:
        return CompressionDecision.deny(f"Skipped due to extension {suffix}")

    try:
        file_size = file_path.stat().st_size
    except OSError as exc:
        logging.error("Failed to stat %s: %s", file_path, exc)
        return CompressionDecision.deny(f"Unable to read file size: {exc}")

    if file_size < MIN_COMPRESSIBLE_SIZE:
        return CompressionDecision.deny(f"File too small ({file_size} bytes)", file_size)

    is_compressed, compressed_size = is_file_compressed(file_path, thorough_check)
    if is_compressed:
        return CompressionDecision.deny("File is already compressed", compressed_size)

    return CompressionDecision.allow(file_size)


