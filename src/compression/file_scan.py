import functools
import os
import stat
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
    """Count file scan inputs without materializing the full file list."""

    __slots__ = ("_source", "count")

    def __init__(self, source: Iterable["_FileScanInput"]) -> None:
        self._source = iter(source)
        self.count = 0

    def __iter__(self) -> Iterator["_FileScanInput"]:
        for entry in self._source:
            self.count += 1
            yield entry


def _walk_skipped_inline(root: Path, callback: Callable[[Path], None]) -> None:
    stack: list[str] = [os.fspath(root)]

    while stack:
        current_dir = stack.pop()
        try:
            with os.scandir(current_dir) as it:
                subdirs: list[str] = []
                for entry in it:
                    try:
                        st = entry.stat()
                        if stat.S_ISDIR(st.st_mode):
                            subdirs.append(entry.path)
                        elif stat.S_ISREG(st.st_mode):
                            callback(Path(entry.path))
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue
        stack.extend(reversed(subdirs))


def iter_files(
    root: Path,
    stats: CompressionStats,
    verbosity: int,
    min_savings_percent: float,
    collect_entropy: bool,
    skipped_file_callback: Optional[Callable[[Path], None]] = None,
) -> Iterator["_FileScanInput"]:
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
            _walk_skipped_inline(root, skipped_file_callback)
        return

    stack: list[str] = [os.fspath(root)]

    while stack:
        current_dir = stack.pop()

        try:
            with os.scandir(current_dir) as it:
                valid_dirs: list[str] = []
                for entry in it:
                    try:
                        st = entry.stat()
                        if stat.S_ISDIR(st.st_mode):
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
                                    _walk_skipped_inline(Path(entry.path), skipped_file_callback)
                                continue
                            valid_dirs.append(entry.path)
                        elif stat.S_ISREG(st.st_mode):
                            yield _FileScanInput(
                                entry.path,
                                st.st_size,
                                getattr(st, "st_file_attributes", 0),
                            )
                    except OSError:
                        continue
        except (OSError, PermissionError):
            continue
        stack.extend(reversed(valid_dirs))


@dataclass(frozen=True, slots=True)
class ScanPayload:
    path: str
    file_size: int
    decision: Optional[CompressionDecision]
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class FileScanInput:
    path: str
    file_size: int
    attributes: int


_FileScanInput = FileScanInput


def _scan_path(
    scan_input: _FileScanInput,
    debug_scan_all: bool = False,
    check_already_compressed: bool = True,
) -> ScanPayload:
    try:
        decision = should_compress_file(
            scan_input.path,
            file_size=scan_input.file_size,
            attributes=scan_input.attributes,
            ignore_extensions=debug_scan_all,
            check_already_compressed=check_already_compressed,
        )
        return ScanPayload(scan_input.path, scan_input.file_size, decision)
    except OSError as exc:
        return ScanPayload(
            scan_input.path,
            0,
            None,
            _("Error processing {file_path}: {exc}").format(file_path=scan_input.path, exc=exc),
        )


def _scan_checks_compressed_state() -> bool:
    value = os.getenv("TRASH_COMPACTOR_FAST_SCAN", "1").strip().lower()
    return value in {"0", "false", "no", "off"}


def iter_scanned_files(files: Iterable[_FileScanInput], debug_scan_all: bool = False) -> Iterator[ScanPayload]:
    workers = scan_worker_count()
    check_already_compressed = _scan_checks_compressed_state()

    mapper = functools.partial(
        _scan_path,
        debug_scan_all=debug_scan_all,
        check_already_compressed=check_already_compressed,
    )

    if workers <= 1:
        for scan_input in files:
            yield mapper(scan_input)
        return

    with ThreadPoolExecutor(max_workers=workers) as executor:
        from itertools import islice

        chunk_size = 2000
        in_flight_limit = max(workers * 4, workers + 1)
        pending: set[Future[list[ScanPayload]]] = set()
        entries = iter(files)

        def _process_chunk(chunk: list[_FileScanInput]) -> list[ScanPayload]:
            return [mapper(scan_input) for scan_input in chunk]

        def _submit_next() -> bool:
            chunk = list(islice(entries, chunk_size))
            if not chunk:
                return False
            pending.add(executor.submit(_process_chunk, chunk))
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