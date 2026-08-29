import ctypes
import logging
import os
import stat
import subprocess
from dataclasses import dataclass
from ctypes import wintypes
from pathlib import Path
from typing import Optional, Tuple

from .i18n import _
from .drive_inspector import DRIVE_REMOTE, KERNEL32, get_volume_details


def sanitize_path(path: str) -> str:
    return os.path.normpath(path.strip(" '\""))


def is_admin() -> bool:
    try:
        return os.getuid() == 0
    except AttributeError:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())


def hidden_startupinfo() -> subprocess.STARTUPINFO:
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def normalize_path(path: str | Path) -> str:
    normalized = os.path.normcase(os.path.normpath(str(path)))
    if len(normalized) == 2 and normalized[1] == ':':
        return normalized + os.sep
    return normalized


def _fixed_drive_roots() -> tuple[str, ...]:
    try:
        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        _get_drive_type = kernel32.GetDriveTypeW
        _get_drive_type.argtypes = [wintypes.LPCWSTR]
        _get_drive_type.restype = wintypes.UINT
        _logical_drives = kernel32.GetLogicalDrives
        _logical_drives.restype = wintypes.DWORD

        DRIVE_FIXED = 3
        bitmask = _logical_drives()
        roots: list[str] = []
        for index in range(26):
            if bitmask & (1 << index):
                root = f"{chr(ord('A') + index)}:\\"
                if _get_drive_type(root) == DRIVE_FIXED:
                    roots.append(root)
        if roots:
            return tuple(roots)
    except Exception:
        pass

    # Fallback: only the system drive.
    system_drive = os.environ.get('SystemDrive', 'C:')
    return (system_drive if system_drive.endswith(('\\', '/')) else f"{system_drive}\\",)


def _default_excluded_directories() -> tuple[str, ...]:
    system_root = os.environ.get('SystemRoot') or ''
    system_root_norm = os.path.normcase(os.path.normpath(system_root)) if system_root else ''

    def _drive_path(root: str, segment: str) -> str:
        return os.path.join(root, segment)

    entries: list[str] = []

    for drive_root in _fixed_drive_roots():
        # The active Windows install may live on any fixed drive; always exclude
        # it, even if SystemDrive does not point at it.
        windows_root = _drive_path(drive_root, 'Windows')
        if not system_root_norm or os.path.normcase(os.path.normpath(windows_root)) == system_root_norm:
            entries.append(windows_root)

        # Stale installs: Windows.old is always protected at the drive root, and
        # Windows.old.NNN (e.g. Windows.old.000) via the dotted-prefix entry.
        entries.append(_drive_path(drive_root, 'Windows.old'))
        entries.append(_drive_path(drive_root, 'Windows.old.'))
        try:
            with os.scandir(drive_root) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    name = entry.name
                    if name.lower().startswith('windows.old') and (
                        len(name) == len('windows.old') or name[len('windows.old')] == '.'
                    ):
                        entries.append(entry.path)
        except OSError:
            pass

        # Protected directories on every fixed drive
        for segment in ('$Recycle.Bin', 'System Volume Information', 'Recovery', 'PerfLogs'):
            entries.append(_drive_path(drive_root, segment))

    if not entries:
        entries.append(os.environ.get('SystemRoot') or system_root)

    seen: set[str] = set()
    cleaned: list[str] = []
    for entry in entries:
        if not entry:
            continue
        normalized = os.path.normcase(os.path.normpath(entry))
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned.append(os.path.normpath(entry))
    return tuple(cleaned)


DEFAULT_EXCLUDE_DIRECTORIES: Tuple[str, ...] = _default_excluded_directories()

_DEFAULT_EXCLUDE_MAP: dict[str, str] = {
    normalize_path(candidate): os.path.normpath(candidate)
    for candidate in DEFAULT_EXCLUDE_DIRECTORIES
}


def _within_or_equal(normalized: str, excluded: str) -> bool:
    return normalized == excluded or normalized.startswith(excluded + os.sep)


def _match_exclusion(normalized: str) -> tuple[bool, Optional[str]]:
    for excluded_norm, display in _DEFAULT_EXCLUDE_MAP.items():
        # Dotted namespace prefix (e.g. C:\Windows.old.000 under Windows.old.)
        within = _within_or_equal(normalized, excluded_norm) or (
            excluded_norm.endswith(os.sep + "windows.old.") and normalized.startswith(excluded_norm)
        )
        if not within:
            continue
        if normalized == excluded_norm:
            return True, _("Protected system directory ({display})").format(display=display)
        return True, _("Within protected system directory ({display})").format(display=display)
    return False, None


_FILE_ATTRIBUTE_COMPRESSED = getattr(stat, 'FILE_ATTRIBUTE_COMPRESSED', 0x800)

_GET_COMPRESSED_FILE_SIZE = KERNEL32.GetCompressedFileSizeW
_GET_COMPRESSED_FILE_SIZE.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
_GET_COMPRESSED_FILE_SIZE.restype = wintypes.DWORD

def get_ntfs_compressed_size(file_path: str | Path) -> int:
    high = wintypes.DWORD()
    low = _GET_COMPRESSED_FILE_SIZE(str(file_path), ctypes.byref(high))
    if low == 0xFFFFFFFF:
        # GetCompressedFileSizeW returns INVALID_FILE_SIZE (0xFFFFFFFF) as the
        # low DWORD for both failure AND for legitimate sizes >= 4 GiB where
        # the high DWORD is non-zero. This right here is the correct error identifier
        error = ctypes.get_last_error()
        if error:
            raise ctypes.WinError(error)
    return (high.value << 32) + low

@dataclass(frozen=True, slots=True)
class DirectoryDecision:
    skip: bool
    reason: str = ""
    category: str = "system"

    @classmethod
    def deny(cls, reason: str, category: str = "system") -> "DirectoryDecision":
        return cls(True, reason, category)

    @classmethod
    def allow_path(cls) -> "DirectoryDecision":
        return cls(False, "")


def should_skip_directory(directory: str | Path) -> DirectoryDecision:
    normalized = normalize_path(directory)
    match, reason = _match_exclusion(normalized)
    if match:
        return DirectoryDecision.deny(reason or _("Protected system directory"), category="system")
    from .exclusions import match_user_exclusion

    user_reason = match_user_exclusion(directory)
    if user_reason:
        return DirectoryDecision.deny(user_reason, category="user")
    return DirectoryDecision.allow_path()


def get_protection_reason(path: str | Path) -> Optional[str]:
    normalized = normalize_path(path)
    _, reason = _match_exclusion(normalized)
    return reason


def validate_target_path(directory: str) -> Optional[str]:
    """Return a reason the target cannot be compressed, or None if it can.

    Covers the structural checks shared by the CLI and GUI: protected system
    paths, unresolvable volumes, network shares, and non-NTFS filesystems.
    """
    candidate = sanitize_path(directory)
    if not candidate:
        return _("No folder selected")

    protection_reason = get_protection_reason(candidate)
    if protection_reason:
        return _("Cannot compress protected path: {reason}").format(reason=protection_reason)

    from .exclusions import match_user_exclusion

    user_reason = match_user_exclusion(candidate)
    if user_reason:
        return _("Cannot compress excluded path: {reason}").format(reason=user_reason)

    from .drive_inspector import get_volume_details_fast

    details = get_volume_details_fast(candidate)
    if details.anchor is None:
        return _("Unable to resolve the target volume. Please verify the path.")

    if details.drive_type == DRIVE_REMOTE:
        return _("Network shares are not supported targets for compression.")

    if details.filesystem and details.filesystem != "NTFS":
        return _(
            "Windows compression requires NTFS. Detected filesystem: {filesystem}"
        ).format(filesystem=details.filesystem or "unknown")

    return None


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
            attributes = getattr(stat_info, "st_file_attributes", 0)
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


