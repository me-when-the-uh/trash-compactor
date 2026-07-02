from typing import Optional

UI_STATUS_INTERVAL_SECONDS = 0.08
UI_SUMMARY_INTERVAL_SECONDS = 0.08
SCAN_STOP_CHECK_EVERY_FILES = 64
PLAN_PROGRESS_GRANULARITY = 32
ENTROPY_PROGRESS_GRANULARITY = 8
COMPRESSION_PROGRESS_GRANULARITY = 4
_PROGRESS_MARGIN_OF_ERROR = -1.0
_PROGRESS_SCAN_BASELINE_FILES = 100_000 # May overflow above 33% if there are too many files
_PROGRESS_SCAN_END = 100.0 / 3.0 # Tracked until 33%
_PROGRESS_ENTROPY_END = 100.0


def scan_progress_percent(file_count: int) -> float:
    if file_count <= 0:
        return 0.0
    return (file_count / _PROGRESS_SCAN_BASELINE_FILES) * _PROGRESS_SCAN_END


def entropy_progress_percent(processed: int, total: int) -> float:
    if total:
        span = _PROGRESS_ENTROPY_END - _PROGRESS_SCAN_END
        return _PROGRESS_SCAN_END + (processed / total) * span
    return _PROGRESS_SCAN_END


def scale_quick_progress(
    local_pct: Optional[float],
    dir_index: Optional[int],
    dir_total: Optional[int],
) -> Optional[float]:
    if local_pct is not None and local_pct < 0:
        return _PROGRESS_MARGIN_OF_ERROR
    if not dir_index or not dir_total or dir_total <= 0 or local_pct is None:
        return local_pct
    segment = 100.0 / dir_total
    return ((dir_index - 1) * segment) + (local_pct / 100.0) * segment