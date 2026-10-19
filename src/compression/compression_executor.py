import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional, Sequence

from ..i18n import _
from ..directstorage import compact_failure_is_bypassio, is_under, normalize_path
from ..exceptions import WorkerStopped
from ..file_utils import (
    decode_oem,
    hidden_startupinfo,
    is_file_compressed,
    kill_process_tree,
    track_process,
    untrack_process,
    windows_system_executable,
)
from ..stats import CompressionStats, _algo_field
from ..workers import hdd_mode

# Volumes compact.exe reported as unable to compress.
_wof_unattached_warned: set[str] = set()
_wof_warn_lock = threading.Lock()

_BATCH_SIZE = 100
_HDD_BATCH_SIZE = 25
_MAX_COMMAND_CHARS = 4000
_HDD_MAX_COMMAND_CHARS = 1500
_COMPACT_TIMEOUT_SECONDS = 600
_SINGLE_FILE_TIMEOUT_SECONDS = 60
_TIMEOUT_BYTES_PER_SECOND = 8 * 1024 * 1024
_TIMEOUT_CAP_SECONDS = 6 * 60 * 60


def timeout_for_bytes(total_bytes: int, floor: int) -> int:
    """Seconds to allow compact.exe, at least ``floor`` and at most six hours"""
    scaled = max(0, int(total_bytes)) // _TIMEOUT_BYTES_PER_SECOND
    if scaled < floor:
        return floor
    if scaled > _TIMEOUT_CAP_SECONDS:
        return _TIMEOUT_CAP_SECONDS
    return int(scaled)

# Below Normal process priority for compact.exe
_BELOW_NORMAL_PRIORITY_CLASS = 0x00004000


def _batch_limits() -> tuple[int, int]:
    if hdd_mode():
        return _HDD_BATCH_SIZE, _HDD_MAX_COMMAND_CHARS
    return _BATCH_SIZE, _MAX_COMMAND_CHARS


def _compact_path(path_str: str) -> str:
    if not os.path.isabs(path_str):
        path_str = str(Path(path_str).resolve())
    if path_str.startswith("\\\\?\\"):
        stripped = path_str[4:]
        if len(stripped) < 260:
            return stripped
    return path_str


def _compact_executable() -> str:
    return windows_system_executable("compact.exe")


def _run_compact(
    args: Sequence[str],
    *,
    timeout: int = _COMPACT_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess:
    proc = subprocess.Popen(
        list(args),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        startupinfo=hidden_startupinfo(),
        creationflags=subprocess.CREATE_NO_WINDOW | _BELOW_NORMAL_PRIORITY_CLASS,
        shell=False,
    )
    track_process(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            stdout, stderr = proc.communicate()
            raise
        return subprocess.CompletedProcess(
            args=list(args),
            returncode=proc.returncode if proc.returncode is not None else 1,
            stdout=decode_oem(stdout),
            stderr=decode_oem(stderr),
        )
    finally:
        untrack_process(proc)


def _compact_text(result: subprocess.CompletedProcess) -> str:
    return f"{result.stderr or ''}{result.stdout or ''}"


def _volume_label(path_str: str) -> str:
    drive = os.path.splitdrive(path_str)[0]
    if not drive:
        return path_str
    return drive if drive.endswith(("\\", "/")) else f"{drive}\\"


def _wof_unattached_message(volume: str) -> str:
    return _(
        "The filesystem on {volume} reported NTFS but compact.exe /exe failed "
        "because the Windows Overlay Filter (wof.sys) is not attached. "
        "From an elevated prompt run: fltmc attach Wof {volume}"
    ).format(volume=volume)


def _wof_unattached_volume(path_str: str, result: subprocess.CompletedProcess) -> Optional[str]:
    """Volume label when compact.exe reports that the volume cannot compress."""
    if result.returncode == 0:
        return None
    if "does not support compression" not in _compact_text(result).lower():
        return None
    volume = _volume_label(path_str)
    with _wof_warn_lock:
        first = volume not in _wof_unattached_warned
        _wof_unattached_warned.add(volume)
    if first:
        logging.error(_wof_unattached_message(volume))
    return volume


def _is_wof_unattached(path_str: str) -> bool:
    return _volume_label(path_str) in _wof_unattached_warned


def compress_file_with_reason(
    file_path: Path,
    algorithm: str,
    *,
    timeout: int = _COMPACT_TIMEOUT_SECONDS,
) -> tuple[bool, Optional[str]]:
    try:
        command = [_compact_executable(), '/c', '/a', f'/exe:{algorithm}', _compact_path(str(file_path))]
        result = _run_compact(command, timeout=timeout)
        _wof_unattached_volume(str(file_path), result)
        if result.returncode == 0:
            return True, None
        if compact_failure_is_bypassio(_compact_text(result)):
            return False, _("Skipped because BypassIO is active on this file")
        return False, None
    except (OSError, subprocess.SubprocessError) as exc:
        logging.error("Error compressing %s: %s", file_path, exc)
        return False, None


def execute_compression_plan(
    plan: Sequence[tuple[str, int, str]],
    stats: CompressionStats,
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
    blocked_dirs: set[str] = set()
    bypassio_reason = _("Skipped because BypassIO is active on this file")
    wof_reason = _("Skipped: the volume cannot compress files")
    bypassio_warned = False
    wof_errored: set[str] = set()
    # Set when the run is abandoned: queued batches must not start compact.exe.
    cancel = threading.Event()

    def _record_wof_error(volume: str) -> None:
        if volume in wof_errored:
            return
        wof_errored.add(volume)
        _record_error(_wof_unattached_message(volume))

    def _block_parent(path_str: str) -> None:
        parent = normalize_path(os.path.dirname(path_str))
        if parent:
            blocked_dirs.add(parent)

    def _is_blocked(path_str: str) -> bool:
        if not blocked_dirs:
            return False
        return any(is_under(path_str, blocked) for blocked in blocked_dirs)

    def _compact_batch(algo: str, path_strs: Sequence[str], total_bytes: int) -> subprocess.CompletedProcess:
        if cancel.is_set():
            raise WorkerStopped()
        # compact /c /a /exe:{algo} path1 path2 ...
        args = [_compact_executable(), '/c', '/a', f'/exe:{algo}']
        args.extend(_compact_path(path_str) for path_str in path_strs)
        return _run_compact(args, timeout=timeout_for_bytes(total_bytes, _COMPACT_TIMEOUT_SECONDS))

    def _record_error(message: str) -> None:
        with stats_lock:
            stats.record_error(message)

    def _record_success(path: Path, compressed_size: int, algo: str, verified: bool) -> None:
        # Files that did not shrink stay in the on-disk total.
        with stats_lock:
            bucket = _algo_field(stats, algo)
            stats.total_compressed_size += compressed_size
            bucket.bytes_compressed += compressed_size
            if verified:
                stats.compressed_files += 1
                bucket.files_compressed += 1
        if verified:
            logging.debug("Compressed %s using %s", path, algo)
        elif verbosity >= 1:
            logging.warning(
                "Compressed %s using %s but verification reported no size change",
                path,
                algo,
            )
        else:
            logging.debug(
                "Compressed %s using %s (verification reported no size change)",
                path,
                algo,
            )
        _notify_progress(path, algo)

    def _record_failure(path: Path, file_size: int, algo: str, reason: Optional[str] = None) -> None:
        with stats_lock:
            stats.record_file_skip(path, reason or _("Compression failed"), file_size, file_size)
            _algo_field(stats, algo).bytes_compressed += file_size
            if reason is None:
                stats.record_error(_("Failed to compress {path}").format(path=path))
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
            except WorkerStopped:
                raise
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
            if not verified and compressed_size <= 0:
                compressed_size = fallback_size
            _record_success(path, compressed_size, algo, verified)

    def _compress_single(
        path: Path,
        file_size: int,
        algo: str,
        *,
        timeout: int = _SINGLE_FILE_TIMEOUT_SECONDS,
    ) -> None:
        if _is_blocked(str(path)):
            _record_failure(path, file_size, algo, bypassio_reason)
            return
        if _is_wof_unattached(str(path)):
            _record_failure(path, file_size, algo, wof_reason)
            return

        success, reason = compress_file_with_reason(path, algo, timeout=timeout)

        if not success:
            if reason:
                nonlocal bypassio_warned
                _block_parent(str(path))
                if not bypassio_warned:
                    bypassio_warned = True
                    logging.warning(
                        _("Skipping DirectStorage game {path}: compact.exe would disable BypassIO").format(
                            path=path.parent
                        )
                    )
            _record_failure(path, file_size, algo, reason)
            return

        _finalize_success(path, file_size, algo, context='fallback')

    grouped: dict[str, list[tuple[str, int]]] = {}
    for path_str, size, algorithm in plan:
        grouped.setdefault(algorithm, []).append((path_str, size))
    for algorithm, entries in grouped.items():
        bucket = _algo_field(stats, algorithm)
        bucket.files_planned = len(entries)
        bucket.bytes_planned = sum(size for _, size in entries)

    for algorithm, entries in grouped.items():
        workers = lzx_workers if algorithm == 'LZX' else xp_workers
        batch_size, max_chars = _batch_limits()

        if stage_callback:
            try:
                stage_callback(algorithm, len(entries))
            except Exception:  # pragma: no cover - defensive logging
                logging.debug("Stage callback failed for %s", algorithm, exc_info=True)

        executor = ThreadPoolExecutor(max_workers=workers)
        try:
            futures: dict = {}
            retry_futures: list = []
            batch: list[tuple[str, int]] = []
            batch_chars = 0

            def _submit_retry(path_str: str, file_size: int) -> None:
                # Retries go through the pool. A failed batch keeps the
                # stage at full parallelism instead of one serial worker.
                future = executor.submit(
                    _compress_single,
                    Path(path_str),
                    file_size,
                    algorithm,
                    timeout=timeout_for_bytes(file_size, _SINGLE_FILE_TIMEOUT_SECONDS),
                )
                retry_futures.append(future)

            for path_str, file_size in entries:
                if _is_wof_unattached(path_str):
                    _record_failure(Path(path_str), file_size, algorithm, wof_reason)
                    continue
                path_length = len(_compact_path(path_str)) + 3  # for quotes and space
                if batch and (len(batch) >= batch_size or batch_chars + path_length > max_chars):
                    batch_bytes = sum(size for _, size in batch)
                    futures[executor.submit(
                        _compact_batch, algorithm, [p for p, _ in batch], batch_bytes
                    )] = list(batch)
                    batch = []
                    batch_chars = 0

                batch.append((path_str, file_size))
                batch_chars += path_length

            if batch:
                batch_bytes = sum(size for _, size in batch)
                futures[executor.submit(
                    _compact_batch, algorithm, [p for p, _ in batch], batch_bytes
                )] = list(batch)

            for future in as_completed(futures):
                batch = futures[future]
                try:
                    result = future.result()
                except WorkerStopped:
                    raise
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
                        _submit_retry(path_str, file_size)
                    continue

                if result.returncode != 0:
                    logging.debug(
                        "Batch compact returned %s for %s with %s files. Falling back to single-file attempts.",
                        result.returncode,
                        algorithm,
                        len(batch),
                    )
                    volume = _wof_unattached_volume(batch[0][0], result) if batch else None
                    if volume is not None:
                        # The volume cannot compress anything: skip the batch.
                        _record_wof_error(volume)
                        for path_str, file_size in batch:
                            path = Path(path_str)
                            compressed, compressed_size = is_file_compressed(path)
                            if compressed:
                                _record_success(path, compressed_size, algorithm, verified=True)
                            else:
                                _record_failure(path, file_size, algorithm, wof_reason)
                        continue
                    for path_str, file_size in batch:
                        path = Path(path_str)
                        if _is_blocked(path_str):
                            _record_failure(path, file_size, algorithm, bypassio_reason)
                            continue
                        # One bad path fails the whole batch. Verify first,
                        # retry only what is left.
                        compressed, compressed_size = is_file_compressed(path)
                        if compressed:
                            _record_success(path, compressed_size, algorithm, verified=True)
                        else:
                            _submit_retry(path_str, file_size)
                    continue

                for path_str, file_size in batch:
                    _finalize_success(Path(path_str), file_size, algorithm, context='batch')

            for future in as_completed(retry_futures):
                try:
                    future.result()
                except WorkerStopped:
                    raise
                except Exception:
                    logging.debug("Single-file retry raised", exc_info=True)
        except BaseException:
            # Queued batches are dropped. The ones inside compact.exe finish.
            cancel.set()
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            executor.shutdown(wait=True)


def _uncompact(path_strs: Sequence[str], *, exe: bool) -> subprocess.CompletedProcess:
    args = [_compact_executable(), '/u', '/a']
    if exe:
        args.append('/exe')
    args.extend(_compact_path(path_str) for path_str in path_strs)
    return _run_compact(args)


def execute_decompression_plan(
    plan: Sequence[tuple[str, int]],
    *,
    workers: int,
    progress_callback: Optional[Callable[[Path, bool], None]] = None,
) -> None:
    if not plan:
        return

    batch_size, max_chars = _batch_limits()
    worker_count = max(1, workers)
    # Set when the run is abandoned: queued batches must not start compact.exe.
    cancel = threading.Event()

    def _decompress_batch(paths: list[str]) -> subprocess.CompletedProcess:
        if cancel.is_set():
            raise WorkerStopped()
        return _uncompact(paths, exe=True)

    def _decompress_one(path_str: str) -> None:
        path = Path(path_str)
        try:
            result = _uncompact([path_str], exe=True)
            still = is_file_compressed(path_str)[0]
            if result.returncode != 0 or still:
                result = _uncompact([path_str], exe=False)
                still = is_file_compressed(path_str)[0]
            if progress_callback is not None:
                progress_callback(path, result.returncode == 0 and not still)
        except WorkerStopped:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            logging.error("Error decompressing %s: %s", path_str, exc)
            if progress_callback is not None:
                progress_callback(path, False)

    executor = ThreadPoolExecutor(max_workers=worker_count)
    try:
        futures: dict = {}
        batch: list[str] = []
        batch_chars = 0
        for path_str, _size in plan:
            path_length = len(_compact_path(path_str)) + 3
            if batch and (len(batch) >= batch_size or batch_chars + path_length > max_chars):
                futures[executor.submit(_decompress_batch, list(batch))] = list(batch)
                batch = []
                batch_chars = 0
            batch.append(path_str)
            batch_chars += path_length
        if batch:
            futures[executor.submit(_decompress_batch, list(batch))] = list(batch)

        leftover: list[str] = []
        for future in as_completed(futures):
            group = futures[future]
            try:
                result = future.result()
            except WorkerStopped:
                raise
            except Exception as exc:
                logging.error(
                    "Batch decompress exception (%s files): %s. Retrying individually.",
                    len(group),
                    exc,
                )
                leftover.extend(group)
                continue
            if result.returncode != 0:
                leftover.extend(group)
                continue
            for path_str in group:
                if is_file_compressed(path_str)[0]:
                    leftover.append(path_str)
                elif progress_callback is not None:
                    progress_callback(Path(path_str), True)

        for path_str in leftover:
            _decompress_one(path_str)
    except BaseException:
        cancel.set()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        executor.shutdown(wait=True)
