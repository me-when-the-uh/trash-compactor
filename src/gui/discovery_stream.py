import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..compression.file_scan import fast_walk_available, iter_files
from ..i18n import _
from ..stats import CompressionStats
from .progress import (
    SCAN_STOP_CHECK_EVERY_FILES,
    UI_STATUS_INTERVAL_SECONDS,
    UI_SUMMARY_INTERVAL_SECONDS,
    scan_progress_percent,
)
from .summary import build_live_analysis_timing

if TYPE_CHECKING:
    from .backend import GuiBackend


class GuiDiscoveryStream:
    """Yield scan inputs; optionally pre-buffer the Rust walk before the check phase."""

    __slots__ = (
        "_backend",
        "_base_dir",
        "_stats",
        "_progress_kwargs",
        "_overall_start",
        "_buffer",
        "count",
        "walk_seconds",
        "complete",
        "buffering",
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
        self._buffer: list | None = None
        self.count = 0
        self.walk_seconds = 0.0
        self.complete = False
        self.buffering = False
        self._check_phase = False
        self._walk_start = 0.0
        self._last_scan_update = 0.0
        self._last_summary_update = 0.0

    def enter_check_phase(self) -> None:
        self._check_phase = True

    def notify_check_progress(self, processed: int) -> None:
        if processed > self._backend._check_processed:
            self.buffering = False
        self._backend._check_processed = processed

    def _sync_buffering_state(self) -> None:
        if not self._check_phase or self.complete:
            self.buffering = False
            return
        if self._buffer is not None:
            self.buffering = False
            return
        processed = self._backend._check_processed
        if processed > 0 and processed >= self.count - 1:
            self.buffering = True

    def prefill_walk(self) -> bool:
        if not fast_walk_available():
            return False

        self._walk_start = time.perf_counter()
        self._last_scan_update = self._walk_start
        self._last_summary_update = self._walk_start
        self._buffer = []

        for entry in iter_files(
            self._base_dir,
            self._stats,
            0,
            self._backend.min_savings,
            collect_entropy=False,
        ):
            self._buffer.append(entry)
            self.count += 1
            if self.count % SCAN_STOP_CHECK_EVERY_FILES == 0:
                self._backend._check_pause_stop()

            now = time.perf_counter()
            walk_elapsed = max(0.001, now - self._walk_start)
            if now - self._last_scan_update > UI_STATUS_INTERVAL_SECONDS:
                self._last_scan_update = now
                self._backend._send_progress(
                    _("Scanning directory... {count} files found ({rate:.0f} files/s)").format(
                        count=self.count,
                        rate=self.count / walk_elapsed,
                    ),
                    scan_progress_percent(self.count),
                    **self._progress_kwargs,
                )

            if now - self._last_summary_update > UI_SUMMARY_INTERVAL_SECONDS:
                self._last_summary_update = now
                timing = build_live_analysis_timing(
                    scan_seconds=walk_elapsed,
                    total_files=self.count,
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

        self.walk_seconds = max(0.001, time.perf_counter() - self._walk_start)
        return True

    def __iter__(self):
        self._walk_start = time.perf_counter()
        self._last_scan_update = self._walk_start
        self._last_summary_update = self._walk_start
        try:
            if self._buffer is not None:
                for index, entry in enumerate(self._buffer):
                    if index % SCAN_STOP_CHECK_EVERY_FILES == 0:
                        self._backend._check_pause_stop()
                    yield entry
                return

            for entry in iter_files(
                self._base_dir,
                self._stats,
                0,
                self._backend.min_savings,
                collect_entropy=False,
            ):
                self.count += 1
                if self.count % SCAN_STOP_CHECK_EVERY_FILES == 0:
                    self._backend._check_pause_stop()

                now = time.perf_counter()
                walk_elapsed = max(0.001, now - self._walk_start)
                if self._check_phase:
                    self._sync_buffering_state()
                    processed = self._backend._check_processed
                    if (
                        (self.buffering or processed < self.count)
                        and now - self._last_scan_update > UI_STATUS_INTERVAL_SECONDS
                    ):
                        self._last_scan_update = now
                        self._backend._send_check_phase_progress(
                            processed,
                            self.count,
                            scanning_more=self.buffering,
                            **self._progress_kwargs,
                        )
                elif now - self._last_scan_update > UI_STATUS_INTERVAL_SECONDS:
                    self._last_scan_update = now
                    self._backend._send_progress(
                        _("Scanning directory... {count} files found ({rate:.0f} files/s)").format(
                            count=self.count,
                            rate=self.count / walk_elapsed,
                        ),
                        scan_progress_percent(self.count),
                        **self._progress_kwargs,
                    )

                if (
                    not self._check_phase
                    and now - self._last_summary_update > UI_SUMMARY_INTERVAL_SECONDS
                ):
                    self._last_summary_update = now
                    timing = build_live_analysis_timing(
                        scan_seconds=walk_elapsed,
                        total_files=self.count,
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
                yield entry
        finally:
            self.complete = True
            self.buffering = False
            if self._buffer is None:
                self.walk_seconds = max(0.001, time.perf_counter() - self._walk_start)