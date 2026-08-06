import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from ..i18n import _
from ..config import COMPRESSION_ALGORITHMS
from ..file_utils import is_file_compressed
from ..stats import CompressionStats
from ..timer import PerformanceMonitor
from ..workers import hdd_mode

_BATCH_SIZE = 100
_HDD_BATCH_SIZE = 25
_MAX_COMMAND_CHARS = 4000
_HDD_MAX_COMMAND_CHARS = 1500
_COMPACT_TIMEOUT_SECONDS = 600
_SINGLE_FILE_TIMEOUT_SECONDS = 60


def _batch_limits() -> tuple[int, int]:
    if hdd_mode():
        return _HDD_BATCH_SIZE, _HDD_MAX_COMMAND_CHARS
    return _BATCH_SIZE, _MAX_COMMAND_CHARS


def _compact_path(path_str: str) -> str:
    resolved = str(Path(path_str).resolve())
    if resolved.startswith("\\\\?\\"):
        stripped = resolved[4:]
        if len(stripped) < 260:
            return stripped
    return resolved


def _hidden_startupinfo() -> subprocess.STARTUPINFO:
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _run_compact(
    args: Sequence[str],
    *,
    capture: bool = False,
    timeout: int = _COMPACT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        startupinfo=_hidden_startupinfo(),
        shell=False,
        text=capture,
        timeout=timeout,
    )


def compress_file(file_path: Path, algorithm: str, *, timeout: int = _COMPACT_TIMEOUT_SECONDS) -> bool:
    try:
        # compact /c /a /exe:{algorithm} "{file_path}"
        command = ['compact', '/c', '/a', f'/exe:{algorithm}', _compact_path(str(file_path))]
        result = _run_compact(command, timeout=timeout)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logging.error("Error compressing %s: %s", file_path, exc)
        return False


def execute_compression_plan(
    plan: Sequence[tuple[str, int, str]],
    stats: CompressionStats,
    monitor: PerformanceMonitor,
    verbosity: int,
    xp_workers: int,
    lzx_workers: int,
    *,
    stage_callback: Optional[Callable[[str, int], None]] = None,
    progress_callback: Optional[Callable[[Path, str], None]] = None,
) -> None:
    total = len(plan)
    if not total:
        return

    stats_lock = threading.Lock()
    progress_lock = threading.Lock()

    def _chunk(entries: Sequence[tuple[str, int]]) -> Iterator[list[tuple[str, int]]]:
        current = []
        current_length = 0
        batch_size, max_chars = _batch_limits()

        for path_str, file_size in entries:
            path_length = len(_compact_path(path_str)) + 3  # for quotes and space
            if current and (len(current) >= batch_size or current_length + path_length > max_chars):
                yield current
                current = []
                current_length = 0

            current.append((path_str, file_size))
            current_length += path_length

        if current:
            yield current

    def _compact_batch(algo: str, path_strs: Sequence[str]) -> subprocess.CompletedProcess:
        # compact /c /a /exe:{algo} path1 path2 ...
        args = ['compact', '/c', '/a', f'/exe:{algo}']
        args.extend(_compact_path(path_str) for path_str in path_strs)
        return _run_compact(args)

    def _record_error(message: str) -> None:
        with stats_lock:
            stats.errors.append(message)

    def _record_success(path: Path, compressed_size: int, algo: str, verified: bool) -> None:
        if verified:
            with stats_lock:
                stats.compressed_files += 1
                stats.total_compressed_size += compressed_size
            logging.debug("Compressed %s using %s", path, algo)
        else:
            # Verification failed to show size change, so we don't count it as compressed
            if verbosity >= 2:
                logging.warning(
                    "Compressed %s using %s but verification reported no size change",
                    path,
                    algo,
                )
            else:
                logging.debug(
                    "Compressed %s using %s (verification reported no size change; trusting compact return)",
                    path,
                    algo,
                )
        _notify_progress(path, algo)

    def _record_failure(path: Path, file_size: int, algo: str, reason: Optional[str] = None) -> None:
        with stats_lock:
            stats.record_file_skip(path, reason or _("Compression failed"), file_size, file_size)
        if reason:
            logging.debug("Compression skipped for %s using %s: %s", path, algo, reason)
        else:
            logging.debug("Compression failed for %s using %s", path, algo)
        _notify_progress(path, algo)

    def _notify_progress(path: Path, algo: str) -> None:
        if progress_callback is None:
            return
        with progress_lock:
            try:
                progress_callback(path, algo)
            except Exception:  # pragma: no cover - defensive logging
                logging.debug("Progress callback failed for %s", path, exc_info=True)

    def _finalize_success(path: Path, fallback_size: int, algo: str, context: str) -> None:
        try:
            verified, compressed_size = is_file_compressed(path)
        except OSError as exc:
            _record_error(_("Error verifying {path}: {exc}").format(path=path, exc=exc))
            logging.error("Error verifying %s after %s compression: %s", path, context, exc)
            _record_success(path, fallback_size, algo, verified=False)
        else:
            _record_success(path, compressed_size, algo, verified)

    def _compress_single(
        path: Path,
        file_size: int,
        algo: str,
        *,
        timeout: int = _SINGLE_FILE_TIMEOUT_SECONDS,
    ) -> None:
        success = compress_file(path, algo, timeout=timeout)

        if not success:
            _record_failure(path, file_size, algo)
            return

        _finalize_success(path, file_size, algo, context='fallback')

    grouped: dict[str, list[tuple[str, int]]] = {}
    for path_str, size, algorithm in plan:
        grouped.setdefault(algorithm, []).append((path_str, size))

    for algorithm, entries in grouped.items():
        workers = lzx_workers if algorithm == 'LZX' else xp_workers
        batches = list(_chunk(entries))

        if stage_callback:
            try:
                stage_callback(algorithm, len(entries))
            except Exception:  # pragma: no cover - defensive logging
                logging.debug("Stage callback failed for %s", algorithm, exc_info=True)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _compact_batch,
                    algorithm,
                    [path_str for path_str, _ in batch],
                ): batch
                for batch in batches
            }

            for future in as_completed(futures):
                batch = futures[future]

                try:
                    result = future.result()
                except Exception as exc:
                    logging.error(
                        "Batch compression exception (%s files, algo=%s): %s. Retrying individually.",
                        len(batch),
                        algorithm,
                        exc,
                    )
                    for path_str, file_size in batch:
                        path = Path(path_str)
                        _record_error(_("Batch exception for {path}: {exc}").format(path=path, exc=exc))
                        _compress_single(path, file_size, algorithm, timeout=_SINGLE_FILE_TIMEOUT_SECONDS)
                    continue

                if result.returncode != 0:
                    logging.debug(
                        "Batch compact returned %s for %s with %s files. Falling back to single-file attempts.",
                        result.returncode,
                        algorithm,
                        len(batch),
                    )
                    for path_str, file_size in batch:
                        _compress_single(Path(path_str), file_size, algorithm, timeout=_SINGLE_FILE_TIMEOUT_SECONDS)
                    continue

                for path_str, file_size in batch:
                    _finalize_success(Path(path_str), file_size, algorithm, context='batch')