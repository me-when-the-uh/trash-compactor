import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from itertools import cycle
from pathlib import Path
from typing import List, Optional
import shutil

from .i18n import _


class ProgressTimer:
    def __init__(self, label: str = "Working") -> None:
        self._label = label
        self._message = ""
        self.processed = 0
        self.total = 0

        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._render_interval = 0.1
        self._last_line_length = 0
        self._last_output = ""
        self._start_time: float = 0.0

    def _spin(self) -> None:
        next_tick = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            delay = max(0.0, next_tick - now)
            if delay:
                self._stop_event.wait(delay)
                if self._stop_event.is_set():
                    break
            with self._lock:
                line = self._render_line()
                self._write(line)
            next_tick = time.monotonic() + self._render_interval

    def start(self, total: int = 0) -> None:
        with self._lock:
            self.total = max(0, total)
            self.processed = 0 if self.total == 0 else min(self.processed, self.total)
            self._start_time = time.monotonic()
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._spin, daemon=True)
            self._thread.start()

    def set_label(self, label: str) -> None:
        with self._lock:
            self._label = label

    def set_total(self, total: int) -> None:
        with self._lock:
            self.total = max(0, total)
            self.processed = 0 if self.total == 0 else min(self.processed, self.total)

    def set_message(self, message: str) -> None:
        with self._lock:
            self._message = message

    def update(self, processed: int, current_file: Optional[str] = None) -> None:
        with self._lock:
            self.processed = min(max(0, processed), self.total or processed)
            if current_file is not None:
                self._message = current_file

    def stop(self, final_message: Optional[str] = None) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._clear_line()
        if final_message:
            sys.stdout.write(final_message)
        sys.stdout.flush()

    def _render_line(self) -> str:
        elapsed = time.monotonic() - self._start_time
        
        rate_str = ""
        if elapsed > 0 and self.processed > 0:
            rate = self.processed / elapsed
            rate_str = f" @ {rate:.0f}/s"
            
        progress = f"({self.processed}/{self.total}{rate_str})" if self.total else (f"({self.processed}{rate_str})" if self.processed else "")
        
        parts = [f"[{elapsed:6.1f}s]", self._label]
        if progress:
            parts.append(progress)
        if self._message:
            parts.append(self._message)
        line = " ".join(parts)
        cols = shutil.get_terminal_size((120, 20)).columns
        # Prevent line wrapping: on Windows a wrapped spinner line breaks \r-based redraw.
        if cols > 8 and len(line) >= cols:
            keep = max(1, cols - 4)
            line = line[:keep] + "..."
        return line

    def _write(self, content: str) -> None:
        if content == self._last_output:
            return
        sys.stdout.write('\r')
        sys.stdout.write(content)
        pad = self._last_line_length - len(content)
        if pad > 0:
            sys.stdout.write(' ' * pad)
        sys.stdout.flush()
        self._last_line_length = len(content)
        self._last_output = content

    def _clear_line(self) -> None:
        if self._last_line_length:
            sys.stdout.write('\r' + ' ' * self._last_line_length + '\r')
            self._last_line_length = 0
            self._last_output = ""


@dataclass
class DirectorySkipRecord:
    path: str
    relative_path: str
    reason: str
    category: str
    average_entropy: Optional[float] = None
    estimated_savings: Optional[float] = None
    sampled_files: int = 0
    sampled_bytes: int = 0

    def display_name(self) -> str:
        """Relative path under the scan root, or the absolute path when that would be '.'."""
        relative = (self.relative_path or "").strip()
        if not relative or relative in {".", os.curdir}:
            return self.path
        return relative

    def format_line(self) -> str:
        name = self.display_name()
        if os.path.normcase(os.path.normpath(name)) == os.path.normcase(os.path.normpath(self.path)):
            return self.path
        return f"{name} ({self.path})"


_SKIP_BUCKET_FIELDS: dict = {
    "extension": ("extension", "extension_logical", "extension_hint"),
    "too_small": ("too_small", "too_small_logical", "too_small_hint"),
    "already_compressed": ("already_compressed", "already_compressed_logical", "already_compressed_hint"),
    "error": ("error", "error_logical", "error_hint"),
    "magic": ("magic", "magic_logical", "magic_hint"),
}


@dataclass
class SkipBulkLedger:
    extension: int = 0
    extension_logical: int = 0
    extension_hint: int = 0
    too_small: int = 0
    too_small_logical: int = 0
    too_small_hint: int = 0
    already_compressed: int = 0
    already_compressed_logical: int = 0
    already_compressed_hint: int = 0
    error: int = 0
    error_logical: int = 0
    error_hint: int = 0
    magic: int = 0
    magic_logical: int = 0
    magic_hint: int = 0

    def add(self, category: str, hint: int, logical: int) -> None:
        fields = _SKIP_BUCKET_FIELDS.get(category)
        if fields is None:
            return
        count_field, logical_field, hint_field = fields
        setattr(self, count_field, getattr(self, count_field) + 1)
        setattr(self, logical_field, getattr(self, logical_field) + logical)
        setattr(self, hint_field, getattr(self, hint_field) + (hint if hint > 0 else logical))


@dataclass
class EntropySampleRecord:
    path: str
    relative_path: str
    average_entropy: float
    estimated_savings: float
    sampled_files: int
    sampled_bytes: int
    total_bytes: int
    lz4_certain_files: int = 0
    sampled_paths: List[str] = field(default_factory=list)
    lz4_certain_paths: List[str] = field(default_factory=list)


@dataclass
class AlgorithmStats:
    files_planned: int = 0
    files_compressed: int = 0
    bytes_planned: int = 0
    bytes_projected: int = 0
    bytes_compressed: int = 0


@dataclass
class CompressionStats:
    compressed_files: int = 0
    skipped_files: int = 0
    already_compressed_files: int = 0
    excluded_files: int = 0
    total_original_size: int = 0
    total_on_disk_size: int = 0
    total_compressed_size: int = 0
    total_skipped_size: int = 0
    total_skipped_physical_size: int = 0
    already_compressed_logical_size: int = 0
    already_compressed_physical_size: int = 0
    excluded_logical_size: int = 0
    excluded_physical_size: int = 0
    errors: List[str] = field(default_factory=list)
    directory_skips: List[DirectorySkipRecord] = field(default_factory=list)
    entropy_samples: List[EntropySampleRecord] = field(default_factory=list)
    base_dir: Optional[Path] = None
    entropy_directories_sampled: int = 0
    entropy_directories_below_threshold: int = 0
    skip_extension_files: int = 0
    skip_low_savings_files: int = 0
    min_savings_percent: float = 0.0
    entropy_report_threshold_bytes: int = 0
    entropy_projected_original_bytes: int = 0
    entropy_projected_size: int = 0
    entropy_projected_size_conservative: int = 0
    lz4_certain_incompressible_files: int = 0
    algo_lzx: AlgorithmStats = field(default_factory=AlgorithmStats)
    algo_xpress16k: AlgorithmStats = field(default_factory=AlgorithmStats)
    algo_xpress8k: AlgorithmStats = field(default_factory=AlgorithmStats)
    algo_xpress4k: AlgorithmStats = field(default_factory=AlgorithmStats)

    def by_algorithm_breakdown(self) -> List[dict]:
        buckets = [
            ("LZX", self.algo_lzx),
            ("XPRESS16K", self.algo_xpress16k),
            ("XPRESS8K", self.algo_xpress8k),
            ("XPRESS4K", self.algo_xpress4k),
        ]
        out: List[dict] = []
        for name, b in buckets:
            if b.files_planned == 0 and b.files_compressed == 0:
                continue
            out.append({
                "name": name,
                "files": b.files_compressed or b.files_planned,
                "original": b.bytes_planned,
                "post": b.bytes_compressed or b.bytes_projected,
            })
        return out

    def set_base_dir(self, base_dir: Path) -> None:
        self.base_dir = base_dir

    def record_file_skip(
        self,
        file_path: Path,
        reason: str,
        size_hint: int,
        original_size: int,
        *,
        already_compressed: bool = False,
        category: Optional[str] = None,
    ) -> None:
        self._record_skip(
            size_hint,
            original_size,
            already_compressed,
            self._classify_skip(reason, already_compressed, category),
        )

    def record_file_skip_counters(
        self,
        size_hint: int,
        original_size: int,
        *,
        already_compressed: bool = False,
        category: Optional[str] = None,
    ) -> None:
        self._record_skip(size_hint, original_size, already_compressed, category)

    def record_bulk_skips(self, ledger: SkipBulkLedger) -> None:
        self._record_skip_batch(
            ledger.extension,
            ledger.extension_hint,
            ledger.extension_logical,
            already_compressed=False,
            category="extension",
        )
        self._record_skip_batch(
            ledger.too_small,
            ledger.too_small_hint,
            ledger.too_small_logical,
            already_compressed=False,
            category="too_small",
        )
        self._record_skip_batch(
            ledger.already_compressed,
            ledger.already_compressed_hint,
            ledger.already_compressed_logical,
            already_compressed=True,
            category="already_compressed",
        )
        self._record_skip_batch(
            ledger.error,
            ledger.error_hint,
            ledger.error_logical,
            already_compressed=False,
            category="error",
        )
        self._record_skip_batch(
            ledger.magic,
            ledger.magic_hint,
            ledger.magic_logical,
            already_compressed=False,
            category="magic",
        )

    def _record_skip_batch(
        self,
        count: int,
        hint_total: int,
        logical_total: int,
        *,
        already_compressed: bool,
        category: Optional[str],
    ) -> None:
        if count <= 0:
            return
        self.skipped_files += count
        if hint_total > 0:
            self.total_skipped_physical_size += hint_total
            self.total_compressed_size += hint_total
        if logical_total > 0:
            self.total_skipped_size += logical_total
        if already_compressed:
            self.already_compressed_files += count
            if logical_total > 0:
                self.already_compressed_logical_size += logical_total
            if hint_total > 0:
                self.already_compressed_physical_size += hint_total
        else:
            self.excluded_files += count
            if logical_total > 0:
                self.excluded_logical_size += logical_total
            if hint_total > 0:
                self.excluded_physical_size += hint_total
        if category == "extension":
            self.skip_extension_files += count

    def _record_skip(
        self,
        size_hint: int,
        original_size: int,
        already_compressed: bool,
        category: Optional[str],
    ) -> None:
        resolved_hint = size_hint if size_hint > 0 else original_size
        self._record_skip_batch(
            1,
            resolved_hint,
            original_size,
            already_compressed=already_compressed,
            category=category,
        )
        if category == 'high_entropy':
            self.skip_low_savings_files += 1

    def _classify_skip(
        self,
        reason: str,
        already_compressed: bool,
        category: Optional[str],
    ) -> str:
        if already_compressed:
            return 'already_compressed'
        if category:
            return category

        lowered = reason.lower()
        if 'extension' in lowered:
            return 'extension'
        if 'high entropy' in lowered or 'savings' in lowered:
            return 'high_entropy'
        return 'generic'


def accumulate_stats(target: CompressionStats, source: CompressionStats) -> None:
    target.compressed_files += source.compressed_files
    target.skipped_files += source.skipped_files
    target.already_compressed_files += source.already_compressed_files
    target.total_original_size += source.total_original_size
    target.total_compressed_size += source.total_compressed_size
    target.total_skipped_size += source.total_skipped_size
    target.total_skipped_physical_size += source.total_skipped_physical_size
    target.already_compressed_logical_size += source.already_compressed_logical_size
    target.already_compressed_physical_size += source.already_compressed_physical_size
    target.skip_extension_files += source.skip_extension_files
    target.skip_low_savings_files += source.skip_low_savings_files
    target.errors.extend(source.errors)
    target.entropy_projected_original_bytes += source.entropy_projected_original_bytes or source.total_original_size
    target.entropy_projected_size += source.entropy_projected_size or source.total_compressed_size
    target.entropy_projected_size_conservative += (
        source.entropy_projected_size_conservative or source.total_compressed_size
    )
    target.lz4_certain_incompressible_files += source.lz4_certain_incompressible_files
    target.directory_skips.extend(source.directory_skips)


def apply_entropy_projection(stats: CompressionStats, plan: list[tuple[str, int, str]]) -> None:
    from .config import DRY_RUN_CONSERVATIVE_FACTORS, SIZE_THRESHOLDS

    # Files above the largest size threshold would use LZX when enabled.
    lzx_size_threshold = SIZE_THRESHOLDS[-1][0]  # 1MB

    stats.entropy_projected_original_bytes = stats.total_original_size
    entropy_map = {Path(record.path): record for record in stats.entropy_samples}

    projected_lzx = 0.0
    projected_xpress = 0.0
    for path_str, size, algo in plan:
        record = entropy_map.get(Path(path_str).parent)
        bucket = _algo_field(stats, algo)
        bucket.files_planned += 1
        bucket.bytes_planned += size
        if record:
            factor = max(0.0, record.estimated_savings / 100.0)
            base_compressed = size * (1.0 - factor)

            is_large_file = algo == 'LZX' or (algo == 'XPRESS16K' and size > lzx_size_threshold)

            if is_large_file:
                lzx_factor = DRY_RUN_CONSERVATIVE_FACTORS.get('LZX', 0.95)
                xpress_factor = DRY_RUN_CONSERVATIVE_FACTORS.get('XPRESS16K', 1.0)
                projected_lzx += base_compressed * lzx_factor
                projected_xpress += base_compressed * xpress_factor
                bucket.bytes_projected += int(round(base_compressed * (
                    lzx_factor if algo == 'LZX' else xpress_factor
                )))
            else:
                # Non-large files: same factor for both projections.
                algo_factor = DRY_RUN_CONSERVATIVE_FACTORS.get(algo, 1.0)
                projected_lzx += base_compressed * algo_factor
                projected_xpress += base_compressed * algo_factor
                bucket.bytes_projected += int(round(base_compressed * algo_factor))
        else:
            projected_lzx += size
            projected_xpress += size
            bucket.bytes_projected += size

    skipped = stats.total_compressed_size
    stats.entropy_projected_size = int(round(projected_lzx + skipped))
    stats.entropy_projected_size_conservative = int(round(projected_xpress + skipped))


def _algo_field(stats: CompressionStats, name: str) -> "AlgorithmStats":
    return {
        "LZX": stats.algo_lzx,
        "XPRESS16K": stats.algo_xpress16k,
        "XPRESS8K": stats.algo_xpress8k,
        "XPRESS4K": stats.algo_xpress4k,
    }[name]


def log_estimated_savings(
    original_bytes: int,
    compressed_lzx_bytes: int,
    compressed_xpress_bytes: int,
    *,
    active_large_algorithm: str = "LZX",
) -> None:
    from .cli_log import pretty_size

    original = max(0, int(original_bytes))
    lzx = max(0, int(compressed_lzx_bytes))
    xpress = max(0, int(compressed_xpress_bytes))
    if original <= 0:
        return

    active = (active_large_algorithm or "").upper()

    def _log_line(comp_bytes: int, label: str) -> None:
        savings = max(0, original - comp_bytes)
        ratio = round((savings / original) * 100) if original > 0 else 0
        logging.info(
            "\t%s -> %s (%d%% %s)",
            pretty_size(original),
            pretty_size(comp_bytes),
            ratio,
            label,
        )

    logging.info(_("\nEstimated savings:"))
    if active == "LZX":
        _log_line(lzx, _("with LZX"))
    else:
        _log_line(xpress, _("with XPRESS"))


def print_dry_run_summary(
    *,
    min_savings_percent: float,
    projected_original_bytes: int,
    projected_compressed_lzx_bytes: int,
    projected_compressed_xpress_bytes: int,
    title: Optional[str] = None,
) -> None:
    logging.info("")
    logging.info(title or _("Dry Run Summary"))
    logging.info("-----------------------")
    logging.info(_("Minimum savings threshold: %.1f%%"), float(min_savings_percent))

    from .config import COMPRESSION_ALGORITHMS

    log_estimated_savings(
        projected_original_bytes,
        projected_compressed_lzx_bytes,
        projected_compressed_xpress_bytes,
        active_large_algorithm=COMPRESSION_ALGORITHMS.get('large', 'LZX'),
    )


def print_compression_summary(stats: CompressionStats) -> None:
    from .cli_log import pretty_size

    logging.info(_("\nCompression Summary"))
    logging.info("------------------")
    logging.info(_("Files compressed: %s"), stats.compressed_files)
    logging.info(_("Files skipped: %s"), stats.skipped_files)
    logging.info(_("  %s are compressed with Trash-Compactor"), stats.already_compressed_files)
    logging.info(_("  %s have compressed file types"), stats.skip_extension_files)
    logging.info(
        _("  %s fall below %.1f%% projected savings"),
        stats.skip_low_savings_files,
        stats.min_savings_percent,
    )

    if stats.compressed_files == 0:
        logging.info(_("\nThis directory may have already been compressed."))
        return

    total_original = stats.total_original_size
    total_compressed = stats.total_compressed_size
    logging.info(_("\nOriginal size: %s"), pretty_size(total_original))

    if total_original > 0:
        space_saved = max(0, total_original - total_compressed)
        ratio = (space_saved / total_original) * 100
        logging.info(_("Space saved: %s"), pretty_size(space_saved))
        logging.info(_("Overall compression ratio: %.2f%%"), ratio)
        logging.info(_("Size after compression: %s"), pretty_size(total_compressed))

    if stats.errors:
        logging.info(_("\nErrors encountered:"))
        for error in stats.errors:
            logging.error(error)


def log_by_algorithm(
    stats: CompressionStats,
    lzx_disabled: bool,
    lzx_disabled_reason: Optional[str] = None,
) -> None:
    from .cli_log import pretty_size

    logging.info("")
    logging.info(_("By algorithm:"))
    if lzx_disabled:
        reason = lzx_disabled_reason or "disabled"
        logging.info("  LZX       : %s", _("disabled ({reason})").format(reason=reason))
    for name, b in [
        ("LZX", stats.algo_lzx),
        ("XPRESS16K", stats.algo_xpress16k),
        ("XPRESS8K", stats.algo_xpress8k),
        ("XPRESS4K", stats.algo_xpress4k),
    ]:
        if lzx_disabled and name == "LZX":
            continue
        if b.files_planned == 0 and b.files_compressed == 0:
            continue
        files = b.files_compressed or b.files_planned
        post = b.bytes_compressed or b.bytes_projected
        if b.bytes_planned > 0 and post > 0:
            saved_pct = (1 - post / b.bytes_planned) * 100
        else:
            saved_pct = 0.0
        logging.info(
            "  %-10s: %5d files, %s -> %s (%5.1f%% saved)",
            name,
            files,
            pretty_size(b.bytes_planned),
            pretty_size(post),
            saved_pct,
        )

