import logging
import os
from typing import Callable, Iterable, Iterator, Optional

from ..config import (
    DEFAULT_EXCLUDE_DIRECTORIES,
    MIN_COMPRESSIBLE_SIZE,
    SIZE_THRESHOLDS,
    SKIP_EXTENSIONS,
)
from ..file_utils import _normalize_for_compare
from ..skip_logic import maybe_skip_directory
from ..stats import CompressionStats
from ..workers import scan_worker_count

CAT_ELIGIBLE = 0
CAT_EXTENSION = 1
CAT_TOO_SMALL = 2
CAT_DEBUG_EXT = 3
CAT_ALREADY_COMPRESSED = 4
CAT_ERROR = 5

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
    """Count scan results without materializing the full list elsewhere."""

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
    verbosity: int,
    min_savings_percent: float,
    debug_scan_all: bool = False,
) -> Iterator[tuple]:
    """Yield (path, size, attributes, algo, category, hint) for every file.

    Walk, extension/size classification, and NTFS on-disk checks happen in Rust.
    """
    if maybe_skip_directory(root, root, stats, False, min_savings_percent, verbosity).skip:
        return

    if not fast_walk_available():
        raise RuntimeError("fast_walk extension is required for directory scanning")

    import fast_walk

    excluded = [_normalize_for_compare(path) for path in DEFAULT_EXCLUDE_DIRECTORIES]
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