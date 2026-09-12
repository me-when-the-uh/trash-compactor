import logging
import os
from pathlib import Path
from typing import Iterable, Iterator

from ..config import (
    MIN_COMPRESSIBLE_SIZE,
    SIZE_THRESHOLDS,
    SKIP_EXTENSIONS,
)
from ..exclusions import iter_user_exclusions_under, merged_exclude_directories
from ..file_utils import DEFAULT_EXCLUDE_DIRECTORIES, get_protection_reason
from ..i18n import _
from ..skip_logic import append_directory_skip_record, maybe_skip_directory
from ..stats import CompressionStats, DirectorySkipRecord
from ..workers import scan_worker_count

CAT_ELIGIBLE = 0
CAT_EXTENSION = 1
CAT_TOO_SMALL = 2
CAT_DEBUG_EXT = 3
CAT_ALREADY_COMPRESSED = 4
CAT_ERROR = 5
CAT_MAGIC = 6

_ALGO_NAMES = ("XPRESS4K", "XPRESS8K", "XPRESS16K", "LZX")
_SIZE_BREAKS = tuple(b for b, _ in SIZE_THRESHOLDS)


def _use_fast_walk() -> bool:
    value = os.getenv("TRASH_COMPACTOR_USE_FAST_WALK", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def fast_walk_available() -> bool:
    if not _use_fast_walk():
        return False
    try:
        import fast_walk

        if not callable(getattr(fast_walk, "walk_and_filter", None)):
            raise ImportError("fast_walk.walk_and_filter is missing")
    except Exception as exc:
        logging.warning("fast_walk is unavailable: %s", exc)
        return False
    return True


class CountingDirEntryIter:
    __slots__ = ("_source", "count")

    def __init__(self, source: Iterable) -> None:
        self._source = iter(source)
        self.count = 0

    def __iter__(self) -> Iterator:
        for entry in self._source:
            self.count += 1
            yield entry


def iter_files(
    root,
    stats: CompressionStats,
    debug_scan_all: bool = False,
    *,
    include_user_exclusions: bool = True,
) -> Iterator[tuple]:
    """Walk, extension/size classification, and NTFS on-disk checks happen in Rust."""
    root_path = Path(root)
    if include_user_exclusions:
        if maybe_skip_directory(root, root, stats).skip:
            return
    else:
        reason = get_protection_reason(root_path)
        if reason:
            append_directory_skip_record(
                stats,
                DirectorySkipRecord(
                    path=str(root_path),
                    relative_path="",
                    reason=reason,
                    category="system",
                ),
            )
            return

    if not fast_walk_available():
        raise RuntimeError("fast_walk extension is required for directory scanning")

    import fast_walk

    if include_user_exclusions:
        for display in iter_user_exclusions_under(root_path):
            try:
                relative = str(Path(display).relative_to(root_path))
            except ValueError:
                relative = display
            append_directory_skip_record(
                stats,
                DirectorySkipRecord(
                    path=display,
                    relative_path=relative,
                    reason=_("User-excluded directory"),
                    category="user",
                ),
            )

    excluded = (
        merged_exclude_directories()
        if include_user_exclusions
        else list(DEFAULT_EXCLUDE_DIRECTORIES)
    )
    for batch in fast_walk.walk_and_filter(
        os.fspath(root),
        excluded,
        sorted(SKIP_EXTENSIONS),
        MIN_COMPRESSIBLE_SIZE,
        list(_SIZE_BREAKS),
        debug_scan_all,
        scan_worker_count(),
    ):
        for path, size, attributes, algo, category, hint in batch:
            yield (path, int(size), int(attributes), _ALGO_NAMES[algo], int(category), int(hint))