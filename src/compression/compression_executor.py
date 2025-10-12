import logging
import subprocess
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterator, Optional, Sequence

from ..config import COMPRESSION_ALGORITHMS
from ..file_utils import is_file_compressed
from ..stats import CompressionStats
from ..timer import PerformanceMonitor

_BATCH_SIZE = 100
_MAX_COMMAND_CHARS = 4000


def _hidden_startupinfo() -> subprocess.STARTUPINFO:
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return startupinfo


def _run_compact(command: str, *, capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
        startupinfo=_hidden_startupinfo(),
        shell=True,
        text=capture,
    )


def compress_file(file_path: Path, algorithm: str) -> bool:
    try:
        command = fr'compact /c /a /exe:{algorithm} "{file_path.resolve()}"'
        result = _run_compact(command)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logging.error("Error compressing %s: %s", file_path, exc)
        return False


def legacy_compress_file(file_path: Path) -> bool:
    try:
        command = fr'compact /c "{Path(file_path).resolve()}"'
        result = _run_compact(command, capture=True)
        logging.debug("Command: %s", command)
        logging.debug("Output: %s", result.stdout)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError) as exc:
        logging.error("Error branding %s: %s", file_path, exc)
        return False


def execute_compression_plan(
    plan: Sequence[tuple[Path, int, str]],
    stats: CompressionStats,
    monitor: PerformanceMonitor,
    debug_output: bool,
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

    def _chunk(entries: Sequence[tuple[Path, int]], size: int) -> Iterator[list[tuple[Path, int]]]:
        current = []
        current_length = 0

        for path, file_size in entries:
            path_length = len(str(path.resolve())) + 3  # for quotes and space
            if current and (len(current) >= size or current_length + path_length > _MAX_COMMAND_CHARS):
                yield current
                current = []
                current_length = 0

            current.append((path, file_size))
            current_length += path_length

        if current:
            yield current

    def _compact_batch(algo: str, paths: Sequence[Path]) -> subprocess.CompletedProcess:
        quoted = " ".join(f'"{path.resolve()}"' for path in paths)
        return _run_compact(f'compact /c /a /exe:{algo} {quoted}')

    def _record_error(message: str) -> None:
        with stats_lock:
            stats.errors.append(message)

    def _record_success(path: Path, compressed_size: int, algo: str, verified: bool) -> None:
        with stats_lock:
            stats.compressed_files += 1
            stats.total_compressed_size += compressed_size
        if verified:
            logging.debug("Compressed %s using %s", path, algo)
        else:
            logging.debug(
                "Compressed %s using %s (verification reported no size change; trusting compact return)",
                path,
                algo,
            )
        _notify_progress(path, algo)

    def _record_failure(path: Path, file_size: int, algo: str, reason: Optional[str] = None) -> None:
        with stats_lock:
            stats.record_file_skip(path, reason or "Compression failed", file_size, file_size)
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
            verified, compressed_size = is_file_compressed(path, thorough_check=False)
        except OSError as exc:
            _record_error(f"Error verifying {path}: {exc}")
            logging.error("Error verifying %s after %s compression: %s", path, context, exc)
            _record_success(path, fallback_size, algo, verified=False)
        else:
            _record_success(path, compressed_size, algo, verified)

    def _compress_single(path: Path, file_size: int, algo: str) -> None:
        with monitor.time_compression():
            success = compress_file(path, algo)

        if not success:
            _record_failure(path, file_size, algo)
            return

        _finalize_success(path, file_size, algo, context='fallback')

    grouped = {}
    for path, size, algorithm in plan:
        grouped.setdefault(algorithm, []).append((path, size))

    for algorithm, entries in grouped.items():
        workers = lzx_workers if algorithm == 'LZX' else xp_workers
        batches = list(_chunk(entries, _BATCH_SIZE))

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
                    [path for path, _ in batch],
                ): batch
                for batch in batches
            }

            for future in as_completed(futures):
                batch = futures[future]

                try:
                    with monitor.time_compression():
                        result = future.result()
                except Exception as exc:
                    logging.error(
                        "Batch compression exception (%s files, algo=%s): %s. Retrying individually.",
                        len(batch),
                        algorithm,
                        exc,
                    )
                    for path, file_size in batch:
                        _record_error(f"Batch exception for {path}: {exc}")
                        _compress_single(path, file_size, algorithm)
                    continue

                if result.returncode != 0:
                    logging.debug(
                        "Batch compact returned %s for %s with %s files. Falling back to single-file attempts.",
                        result.returncode,
                        algorithm,
                        len(batch),
                    )
                    for path, file_size in batch:
                        _compress_single(path, file_size, algorithm)
                    continue

                for path, file_size in batch:
                    _finalize_success(path, file_size, algorithm, context='batch')