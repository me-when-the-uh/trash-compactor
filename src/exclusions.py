"""User directory exclusions (issue #21).

Persisted prefixes live next to the incompressible cache. Session extras
come from ``--exclude`` and ``TRASH_COMPACTOR_EXCLUDE``. System directories
are always applied. They cannot be removed.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional

from .file_utils import (
    DEFAULT_EXCLUDE_DIRECTORIES,
    _within_or_equal,
    get_protection_reason,
    normalize_path,
    sanitize_path,
)
from .i18n import _

_SESSION_EXCLUDES: list[str] = []


def set_session_excludes(paths: Iterable[str] | None) -> None:
    global _SESSION_EXCLUDES
    _SESSION_EXCLUDES = [str(path).strip() for path in (paths or []) if str(path).strip()]


def app_data_dir() -> Path:
    appdata = os.getenv("APPDATA")
    if appdata:
        return Path(appdata) / "TrashCompactor"
    return Path.home() / ".cache" / "TrashCompactor"


def exclusions_path() -> Path:
    configured = os.getenv("TRASH_COMPACTOR_EXCLUSIONS_PATH", "").strip()
    if configured:
        return Path(configured)
    return app_data_dir() / "exclusions.txt"


def _dedupe_normalized(paths: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in paths:
        if not raw or not str(raw).strip():
            continue
        path = os.path.normpath(str(raw).strip().strip(" '\""))
        key = normalize_path(path)
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(path)
    return cleaned


def load_persisted_exclusions() -> list[str]:
    path = exclusions_path()
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        values.append(stripped)
    return _dedupe_normalized(values)


def save_persisted_exclusions(paths: Iterable[str]) -> None:
    path = exclusions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(_dedupe_normalized(paths))
    if body:
        body += "\n"
    path.write_text(body, encoding="utf-8")


def env_excludes() -> list[str]:
    raw = os.getenv("TRASH_COMPACTOR_EXCLUDE", "")
    if not raw.strip():
        return []
    return _dedupe_normalized(part.strip() for part in raw.split(";") if part.strip())


def effective_user_exclusions() -> list[str]:
    return _dedupe_normalized(
        list(load_persisted_exclusions()) + env_excludes() + list(_SESSION_EXCLUDES)
    )


def is_system_excluded(path: str | Path) -> bool:
    return get_protection_reason(path) is not None


def match_user_exclusion(path: str | Path) -> Optional[str]:
    normalized = normalize_path(path)
    for display in effective_user_exclusions():
        excluded = normalize_path(display)
        if _within_or_equal(normalized, excluded):
            if normalized == excluded:
                return _("User-excluded directory ({display})").format(display=display)
            return _("Within user-excluded directory ({display})").format(display=display)
    return None


def add_user_exclusion(path: str) -> Optional[str]:
    """Persist ``path``. Returns an error string, or None on success."""
    cleaned = sanitize_path(path)
    if not cleaned:
        return _("No folder selected")
    if is_system_excluded(cleaned):
        return _("Cannot exclude a protected system path.")
    if not os.path.isdir(cleaned):
        return _("Directory '{directory}' was not found.").format(directory=cleaned)
    current = load_persisted_exclusions()
    key = normalize_path(cleaned)
    if any(normalize_path(existing) == key for existing in current):
        return None
    current.append(os.path.normpath(cleaned))
    save_persisted_exclusions(current)
    return None


def remove_user_exclusion(path: str) -> Optional[str]:
    key = normalize_path(sanitize_path(path) or path)
    current = load_persisted_exclusions()
    kept = [item for item in current if normalize_path(item) != key]
    if len(kept) == len(current):
        return _("That folder is not in the exclusion list.")
    save_persisted_exclusions(kept)
    return None


def merged_exclude_directories() -> list[str]:
    return _dedupe_normalized(list(DEFAULT_EXCLUDE_DIRECTORIES) + effective_user_exclusions())


def iter_user_exclusions_under(root: str | Path) -> list[str]:
    """User exclusions strictly under ``root`` that still exist on disk."""
    root_norm = normalize_path(root)
    hits: list[str] = []
    for display in effective_user_exclusions():
        norm = normalize_path(display)
        if norm == root_norm:
            continue
        if norm.startswith(root_norm + os.sep) and os.path.exists(display):
            hits.append(display)
    return hits
