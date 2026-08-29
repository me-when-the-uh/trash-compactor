"""Detect DirectStorage installs and BypassIO compact.exe failures.

The runtime ships as dstorage.dll / dstoragecore.dll. Microsoft documents that
NTFS compression (WOF / compact /exe) vetoes BypassIO, the path DirectStorage
uses on Windows 11. The DLLs are the marker, there is no asset-format magic.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from .file_utils import normalize_path

DLL_NAMES = frozenset({"dstorage.dll", "dstoragecore.dll"})

# Library containers we treat as the upper bound when walking up from a DLL.
# Without these, a scan of Program Files / Steam would skip the whole library.
_LIBRARY_LEAVES = frozenset({
    "common",
    "steamapps",
    "xboxgames",
    "windowsapps",
    "program files",
    "program files (x86)",
    "program files (arm)",
    "programdata",
    "epic games",
    "gog galaxy",
    "gog galaxy games",
    "ubisoft",
    "ubisoft game launcher",
    "origin games",
    "ea games",
    "battle.net",
    "riot games",
    "games",
})

_BPIO_MARKERS = (
    "bypassio",
    "bypass io",
    "bypass-io",
    "with bpio",
    "not_supported_with_bpio",
    "not supported with bpio",
)


def is_under(path: str | Path, root: str | Path) -> bool:
    needle = normalize_path(path)
    base = normalize_path(root)
    if needle == base:
        return True
    prefix = base if base.endswith(os.sep) else base + os.sep
    return needle.startswith(prefix)


def is_directstorage_dll(path: str | Path) -> bool:
    return os.path.basename(str(path)).lower() in DLL_NAMES


def compact_failure_is_bypassio(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _BPIO_MARKERS)


def collapse_roots(roots: Iterable[Path]) -> list[Path]:
    ordered = sorted(roots, key=lambda item: len(normalize_path(item)))
    kept: list[Path] = []
    for root in ordered:
        if any(is_under(root, existing) for existing in kept):
            continue
        kept.append(root)
    return kept


def _clamp_to_scan(path: Path, scan_root: Path) -> Path:
    if is_under(path, scan_root):
        return path
    return scan_root


def unreal_project_root(dll_parent: Path) -> Optional[Path]:
    """Project root for an Unreal Engine layout: .../Engine/Binaries/ThirdParty/Windows/DirectStorage[/arch]."""
    names = [part.lower() for part in dll_parent.parts]
    try:
        index = names.index("directstorage")
    except ValueError:
        return None
    if index < 4:
        return None
    if names[index - 4:index] != ["engine", "binaries", "thirdparty", "windows"]:
        return None
    engine = Path(*dll_parent.parts[: index - 3])
    return engine.parent


def game_root_from_dll(dll_path: Path, scan_root: Path, exe_dirs: set[str]) -> Path:
    parent = dll_path.parent
    unreal = unreal_project_root(parent)
    if unreal is not None:
        return _clamp_to_scan(unreal, scan_root)

    current = parent
    best = parent
    scan_n = normalize_path(scan_root)
    while True:
        current_n = normalize_path(current)
        if current_n in exe_dirs:
            best = current
        if current.name.lower() in _LIBRARY_LEAVES:
            break
        if current_n == scan_n:
            break
        nxt = current.parent
        if nxt == current:
            break
        current = nxt
    return _clamp_to_scan(best, scan_root)


def game_roots_from_dlls(
    dll_paths: Iterable[str],
    scan_root: Path,
    exe_dirs: set[str],
) -> list[Path]:
    roots = [
        game_root_from_dll(Path(path), scan_root, exe_dirs)
        for path in dll_paths
    ]
    return collapse_roots(roots)
