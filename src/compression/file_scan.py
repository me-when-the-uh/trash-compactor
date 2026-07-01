import functools
import os
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from ..i18n import _
from ..file_utils import CompressionDecision, should_compress_file
from ..skip_logic import maybe_skip_directory
from ..stats import CompressionStats
from ..workers import scan_worker_count


class CountingDirEntryIter:
    """Count DirEntry yields without materializing the full file list."""

    __slots__ = ("_source", "count")

    def __init__(self, source: Iterable[os.DirEntry]) -> None:
        self._source = iter(source)
        self.count = 0

    def __iter__(self) -> Iterator[os.DirEntry]:
        for entry in self._source:
            self.count += 1
            yield entry


def iter_files(
    root: Path,
    stats: CompressionStats,
    verbosity: int,
    min_savings_percent: float,
    collect_entropy: bool,
    skipped_file_callback: Optional[Callable[[Path], None]] = None,
) -> Iterator[os.DirEntry]:
    skip_root = maybe_skip_directory(
        root,
        root,
        stats,
        collect_entropy,
        min_savings_percent,
        verbosity,
    ).skip
    if skip_root:
        if skipped_file_callback:
            _traverse_skipped(root, skipped_file_callback)
        return

    stack: list[str] = [os.fspath(root)]

    while stack:
        current_dir = stack.pop()

        try:
            with os.scandir(current_dir) as it:
                valid_dirs: list[str] = []
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            decision = maybe_skip_directory(
                                entry.path,
                                root,
                                stats,
                                collect_entropy,
                                min_savings_percent,
                                verbosity,
                            )
                            if decision.skip:
                                if skipped_file_callback:
                                    _traverse_skipped(Path(entry.path), skipped_file_callback)
                                continue
                            valid_dirs.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            yield entry
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue
        stack.extend(reversed(valid_dirs))


def _traverse_skipped(root: Path, callback: Callable[[Path], None]) -> None:
    for current_root, _, files in os.walk(root):
        current_base = Path(current_root)
        for name in files:
            callback(current_base / name)


@dataclass(frozen=True)
class ScanPayload:
    path: str
    file_size: int
    decision: Optional[CompressionDecision]
    error: Optional[str] = None


def _scan_single(
    entry: os.DirEntry,
    debug_scan_all: bool = False,
    check_already_compressed: bool = True,
) -> ScanPayload:
    file_path = entry.path
    try:
        st = entry.stat()
        file_size = st.st_size
        attrs = getattr(st, 'st_file_attributes', 0)
    except OSError as exc:
        return ScanPayload(file_path, 0, None, _("Error processing {file_path}: {exc}").format(file_path=file_path, exc=exc))

    decision = should_compress_file(
        file_path,
        file_size=file_size,
        attributes=attrs,
        ignore_extensions=debug_scan_all,
        check_already_compressed=check_already_compressed,
    )
    return ScanPayload(file_path, file_size, decision)


def _scan_checks_compressed_state() -> bool:
    value = os.getenv("TRASH_COMPACTOR_FAST_SCAN", "0").strip().lower()
    return value not in {"1", "true", "yes", "on"}


def iter_scanned_files(files: Iterable[os.DirEntry], debug_scan_all: bool = False) -> Iterator[ScanPayload]:
    workers = scan_worker_count()
    check_already_compressed = _scan_checks_compressed_state()

    mapper = functools.partial(
        _scan_single,
        debug_scan_all=debug_scan_all,
        check_already_compressed=check_already_compressed,
    )

    if workers <= 1:
        yield from map(mapper, files)
        return

    with ThreadPoolExecutor(max_workers=workers) as executor:
        from itertools import islice
        chunk_size = 2000
        in_flight_limit = max(workers * 4, workers + 1)
        pending: set[Future[list[ScanPayload]]] = set()
        entries = iter(files)

        def _submit_next() -> bool:
            chunk = list(islice(entries, chunk_size))
            if not chunk:
                return False
            # Submit the whole chunk as a single Future to avoid per-task scheduling overhead
            pending.add(executor.submit(lambda c: [mapper(entry) for entry in c], chunk))
            return True

        for _ in range(in_flight_limit):
            if not _submit_next():
                break

        while pending:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                yield from future.result()

            refill = in_flight_limit - len(pending)
            for _ in range(refill):
                if not _submit_next():
                    break