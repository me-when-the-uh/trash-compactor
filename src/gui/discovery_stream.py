import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..compression.file_scan import iter_files
from ..i18n import _
from ..stats import CompressionStats
from .progress import (
    SCAN_PROGRESS_EVERY_FILES,
    SCAN_STOP_CHECK_EVERY_FILES,
    UI_STATUS_INTERVAL_SECONDS,
    UI_SUMMARY_INTERVAL_SECONDS,
    scan_progress_percent,
)
from .summary import build_live_analysis_timing

if TYPE_CHECKING:
    from .backend import GuiBackend


class GuiDiscoveryStream:
    """Stream classified Rust scan results into the planner, consumed once."""

    __slots__ = (
        "_backend",
        "_base_dir",
        "_stats",
        "_progress_kwargs",
        "_overall_start",
        "count",
        "walk_seconds",
        "complete",
        "_check_phase",
        "_walk_start",
        "_last_scan_update",
        "_last_summary_update",
    )

    def __init__(
        self,
        backend: "GuiBackend",
        base_dir: Path,
        stats: CompressionStats,
        progress_kwargs: dict[str, Any],
        overall_start: float,
    ) -> None:
        self._backend = backend
        self._base_dir = base_dir
        self._stats = stats
        self._progress_kwargs = progress_kwargs
        self._overall_start = overall_start
        self.count = 0
        self.walk_seconds = 0.0
        self.complete = False
        self._check_phase = False
        self._walk_start = 0.0
        self._last_scan_update = 0.0
        self._last_summary_update = 0.0

    def enter_check_phase(self) -> None:
        self._check_phase = True

    def notify_check_progress(self, processed: int) -> None:
        self._backend._check_processed = processed

    def prefill_walk(self) -> bool:
        self._walk_start = time.perf_counter()
        self._last_scan_update = self._walk_start
        self._last_summary_update = self._walk_start
        self._stream_scan_progress(0)
        self.walk_seconds = max(0.001, time.perf_counter() - self._walk_start)
        return True

    def _stream_scan_progress(self, count: int) -> None:
        # Count-gated: a time gate lets one through every ~20ms when the GUI
        # bridge is slow, serialising the whole walk on the webview.
        if count % SCAN_PROGRESS_EVERY_FILES != 0 and not self.complete:
            return
        now = time.perf_counter()
        walk_elapsed = max(0.001, now - self._walk_start)
        if now - self._last_scan_update > UI_STATUS_INTERVAL_SECONDS or self.complete:
            self._last_scan_update = now
            self._backend._send_progress(
                _("Scanning directory... {count} files found ({rate:.0f} files/s)").format(
                    count=count,
                    rate=count / walk_elapsed,
                ),
                scan_progress_percent(count),
                **self._progress_kwargs,
            )

        if now - self._last_summary_update > UI_SUMMARY_INTERVAL_SECONDS or self.complete:
            self._last_summary_update = now
            timing = build_live_analysis_timing(
                scan_seconds=max(0.001, now - self._overall_start),
                total_files=count,
                total_seconds=max(0.001, now - self._overall_start),
            )
            self._backend._send_folder_summary(
                self._stats,
                0,
                0,
                directory=str(self._base_dir),
                scope="current",
                analysis_timing=timing,
            )

    def __iter__(self):
        try:
            self._walk_start = time.perf_counter()
            self._last_scan_update = self._walk_start
            self._last_summary_update = self._walk_start
            for index, entry in enumerate(iter_files(self._base_dir, self._stats, 0, self._backend.min_savings)):
                self.count += 1
                if index % SCAN_STOP_CHECK_EVERY_FILES == 0:
                    self._backend._check_pause_stop()
                self._stream_scan_progress(self.count)
                yield entry
            # Final update with the completed rate.
            self.complete = True
            self._stream_scan_progress(self.count)
        finally:
            self.complete = True
