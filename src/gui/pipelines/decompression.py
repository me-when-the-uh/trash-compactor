import stat
import time
from pathlib import Path
from typing import TYPE_CHECKING

from ...compression.compression_executor import execute_decompression_plan
from ...compression.file_scan import CAT_ALREADY_COMPRESSED, iter_files
from ...exclusions import add_user_exclusion, load_persisted_exclusions
from ...i18n import _
from ...stats import CompressionStats
from ...workers import xp_worker_count
from ..message_types import ExclusionsResponse, WarningResponse
from ..progress import (
    SCAN_PROGRESS_EVERY_FILES,
    SCAN_STOP_CHECK_EVERY_FILES,
    UI_STATUS_INTERVAL_SECONDS,
    remaining_progress_percent,
    scan_progress_percent,
)

if TYPE_CHECKING:
    from ..backend import GuiBackend

FILE_ATTRIBUTE_COMPRESSED = getattr(stat, "FILE_ATTRIBUTE_COMPRESSED", 0x800)


def run_decompression_pipeline(backend: "GuiBackend", *, add_exclusion: bool = True) -> None:
    backend._configure_worker_environment()
    backend._clear_analysis_state()
    backend._clear_quick_analysis_results()

    if not backend.current_folder:
        backend._send(WarningResponse(_("Warning"), _("No folder selected")))
        return

    base_dir = Path(backend.current_folder).resolve()
    stats = CompressionStats()
    stats.set_base_dir(base_dir)

    backend._send_progress(_("Scanning directory..."), -1.0)

    compressed: list[tuple[str, int]] = []
    processed = 0
    scan_start = time.perf_counter()
    last_update = scan_start

    for path, size, attributes, _algo, category, hint in iter_files(
        base_dir,
        stats,
        include_user_exclusions=False,
    ):
        processed += 1
        if processed % SCAN_STOP_CHECK_EVERY_FILES == 0:
            backend._check_pause_stop()
        if (
            category == CAT_ALREADY_COMPRESSED
            or attributes & FILE_ATTRIBUTE_COMPRESSED
            or 0 < hint < size
        ):
            compressed.append((path, size))

        now = time.perf_counter()
        if processed % SCAN_PROGRESS_EVERY_FILES == 0 or now - last_update > UI_STATUS_INTERVAL_SECONDS:
            last_update = now
            elapsed = max(0.001, now - scan_start)
            backend._send_progress(
                _("Scanning directory... {count} files found ({rate:.0f} files/s)").format(
                    count=processed,
                    rate=processed / elapsed,
                ),
                scan_progress_percent(processed),
            )

    backend._check_pause_stop()
    total = len(compressed)
    if not total:
        backend._send_progress(_("Nothing to decompress!"), 0.0, final=True, decompressing=True)
        return

    backend._send_progress(
        _("Decompressing... {decompressed}/{total} ({rate:.0f} files/s)").format(
            decompressed=0,
            total=total,
            rate=0,
        ),
        100.0,
        decompressing=True,
    )

    done = [0]
    exec_start = time.perf_counter()

    def _on_progress(_path: Path, _ok: bool) -> None:
        backend._check_pause_stop()
        done[0] += 1
        n = done[0]
        if n % 4 != 0 and n != total:
            return
        elapsed = max(0.001, time.perf_counter() - exec_start)
        backend._send_progress(
            _("Decompressing... {decompressed}/{total} ({rate:.0f} files/s)").format(
                decompressed=n,
                total=total,
                rate=n / elapsed,
            ),
            remaining_progress_percent(n, total),
            decompressing=True,
            final=n == total,
        )

    execute_decompression_plan(
        compressed,
        workers=xp_worker_count(),
        progress_callback=_on_progress,
    )

    if add_exclusion:
        error = add_user_exclusion(str(base_dir))
        if error:
            backend._send(WarningResponse(_("Warning"), error))
        else:
            backend._send(ExclusionsResponse(paths=load_persisted_exclusions()))

    backend._send_progress(_("Decompression complete"), 0.0, final=True, decompressing=True)
