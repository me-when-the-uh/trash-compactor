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

# Immediate child of these folders is the game. Skipping the folder itself
# would drop a whole Steam / Epic / Origin library.
_LIBRARY_LEAVES = frozenset({
    "steamapps",
    "xboxgames",
    "windowsapps",
    "program files",
    "program files (x86)",
    "program files (arm)",
    "programdata",
    "epic games",
    "epicgames",
    "egs",
    "gog galaxy",
    "gog galaxy games",
    "gog games",
    "ubisoft",
    "ubisoft game launcher",
    "origin games",
    "origin",
    "ea games",
    "electronic arts",
    "battle.net",
    "riot games",
    "games",
    "my games",
    "common files",
    "amazon games",
})

# Parents whose children are still whole profiles or OS trees. Stop here.
# Do not skip the child.
_HARD_TOO_BROAD = frozenset({
    "windows",
    "users",
    "appdata",
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


def _is_drive_root(path: Path) -> bool:
    return path.parent == path


def _is_library_container(path: Path) -> bool:
    name = path.name.lower()
    if name in _LIBRARY_LEAVES:
        return True
    parent = path.parent.name.lower()
    if name == "common" and parent == "steamapps":
        return True
    if name == "library" and parent == "amazon games":
        return True
    return False


def _too_wide_to_skip(path: Path) -> bool:
    return (
        _is_drive_root(path)
        or _is_library_container(path)
        or path.name.lower() in _HARD_TOO_BROAD
    )


def _safe_root(candidate: Path, dll_path: Path, scan_root: Path) -> Path:
    if _too_wide_to_skip(candidate):
        return _clamp_to_scan(dll_path, scan_root)
    return _clamp_to_scan(candidate, scan_root)


def _common_prefix_path(left: Path, right: Path) -> Optional[Path]:
    matched = 0
    for a, b in zip(left.parts, right.parts):
        if a.lower() != b.lower():
            break
        matched += 1
    if matched == 0:
        return None
    return Path(*left.parts[:matched])


def _closest_exe_lca(dll_parent: Path, exe_dirs: set[str], ceiling: Path) -> Optional[Path]:
    best: Optional[Path] = None
    best_len = -1
    for exe_dir in exe_dirs:
        lca = _common_prefix_path(dll_parent, Path(exe_dir))
        if lca is None:
            continue
        if not is_under(lca, ceiling) or not is_under(dll_parent, lca):
            continue
        if _too_wide_to_skip(lca):
            continue
        n = len(lca.parts)
        if n > best_len:
            best = lca
            best_len = n
    return best


def _default_root(
    bound: Optional[Path],
    dll_path: Path,
    scan_root: Path,
    exe_dirs: set[str],
    exe_best: Optional[Path],
    *,
    allow_bound: bool,
) -> Path:
    parent = dll_path.parent
    if bound is not None:
        lca = _closest_exe_lca(parent, exe_dirs, ceiling=bound)
        bound_n = normalize_path(bound)
        if lca is not None:
            lca_n = normalize_path(lca)
            if allow_bound:
                # Drive child is the install unless an .exe names exactly one
                # folder under it (a custom library with several games).
                if lca_n == bound_n or normalize_path(lca.parent) == bound_n:
                    return _safe_root(lca, dll_path, scan_root)
            elif lca_n != bound_n:
                return _safe_root(lca, dll_path, scan_root)
        if allow_bound:
            return _safe_root(bound, dll_path, scan_root)
    if exe_best is not None:
        return _safe_root(exe_best, dll_path, scan_root)
    return _safe_root(parent, dll_path, scan_root)


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
    dll_path = Path(dll_path)
    parent = dll_path.parent
    unreal = unreal_project_root(parent)
    if unreal is not None:
        return _safe_root(unreal, dll_path, scan_root)

    current = parent
    below: Optional[Path] = None
    exe_best: Optional[Path] = None
    while True:
        if normalize_path(current) in exe_dirs:
            exe_best = current
        if _is_library_container(current):
            if below is not None:
                return _safe_root(below, dll_path, scan_root)
            break
        if current.name.lower() in _HARD_TOO_BROAD:
            return _default_root(
                below, dll_path, scan_root, exe_dirs, exe_best, allow_bound=False
            )
        nxt = current.parent
        if nxt == current:
            return _default_root(
                below, dll_path, scan_root, exe_dirs, exe_best, allow_bound=True
            )
        below = current
        current = nxt
    return _default_root(below, dll_path, scan_root, exe_dirs, exe_best, allow_bound=False)


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
