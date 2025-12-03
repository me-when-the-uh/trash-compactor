import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from itertools import cycle
from pathlib import Path
from typing import List, Optional

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

    def format_path(self, full_path: str, base_dir: str) -> str:
        try:
            rel_path = os.path.relpath(full_path, base_dir)
        except Exception:
            rel_path = os.path.basename(full_path)

        parts = rel_path.split(os.sep)
        if len(parts) <= 2:
            return "/".join(parts)

        # This thingy keeps the spinner readable even for deeply nested files
        head, tail = parts[0], parts[-1]
        middle = '...'
        return f"{head}/{middle}/{tail}"

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
        progress = f"({self.processed}/{self.total})" if self.total else ""
        parts = [f"[{elapsed:6.1f}s]", self._label]
        if progress:
            parts.append(progress)
        if self._message:
            parts.append(self._message)
        return " ".join(parts)

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


@dataclass
class EntropySampleRecord:
    path: str
    relative_path: str
    average_entropy: float
    estimated_savings: float
    sampled_files: int
    sampled_bytes: int
    total_bytes: int


@dataclass
class FileSkipRecord:
    path: str
    relative_path: str
    reason: str
    category: str = "generic"


@dataclass
class CompressionStats:
    compressed_files: int = 0
    skipped_files: int = 0
    already_compressed_files: int = 0
    total_original_size: int = 0
    total_compressed_size: int = 0
    total_skipped_size: int = 0
    errors: List[str] = field(default_factory=list)
    directory_skips: List[DirectorySkipRecord] = field(default_factory=list)
    entropy_samples: List[EntropySampleRecord] = field(default_factory=list)
    file_skips: List[FileSkipRecord] = field(default_factory=list)
    base_dir: Optional[Path] = None
    entropy_directories_sampled: int = 0
    entropy_directories_below_threshold: int = 0
    skip_extension_files: int = 0
    skip_low_savings_files: int = 0
    min_savings_percent: float = 0.0
    entropy_report_threshold_bytes: int = 0
    entropy_projected_original_bytes: int = 0
    entropy_projected_compressed_bytes: int = 0
    entropy_projected_compressed_bytes_conservative: int = 0

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
        resolved_hint = size_hint if size_hint > 0 else original_size
        self.skipped_files += 1
        if resolved_hint > 0:
            self.total_compressed_size += resolved_hint
        if original_size > 0:
            self.total_skipped_size += original_size
        if already_compressed:
            self.already_compressed_files += 1

        resolved_category = self._classify_skip(reason, already_compressed, category)
        if resolved_category == 'extension':
            self.skip_extension_files += 1
        elif resolved_category == 'high_entropy':
            self.skip_low_savings_files += 1

        relative = str(file_path)
        base = self.base_dir
        if base is not None:
            try:
                relative = str(file_path.relative_to(base))
            except ValueError:
                try:
                    relative = str(file_path.resolve().relative_to(base))
                except Exception:
                    relative = str(file_path)

        self.file_skips.append(
            FileSkipRecord(
                path=str(file_path),
                relative_path=relative,
                reason=reason,
                category=resolved_category,
            )
        )

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


@dataclass
class LegacyCompressionStats:
    total_files: int = 0
    branded_files: int = 0
    still_unmarked: int = 0
    errors: List[str] = field(default_factory=list)


def _format_sample_bytes(value: int) -> str:
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    if value > 0:
        return f"{value} B"
    return "0 B"


def _format_summary_size(value: int) -> str:
    if value >= 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024 * 1024):.1f}GB"
    return f"{value / (1024 * 1024):.1f}MB"


def print_entropy_dry_run(stats: CompressionStats, min_savings_percent: float, verbosity: int = 0) -> None:
    logging.info(_("\nEntropy Dry Run Summary"))
    logging.info("-----------------------")

    samples = sorted(stats.entropy_samples, key=lambda record: record.estimated_savings, reverse=True)
    if not samples:
        threshold_bytes = stats.entropy_report_threshold_bytes
        if threshold_bytes:
            logging.info(
                _("No directories exceeded the reporting threshold of %.1f MB."),
                threshold_bytes / (1024 * 1024),
            )
        else:
            logging.info(_("No eligible directories were analysed."))
        return

    logging.info(_("Minimum savings threshold: %.1f%%"), min_savings_percent)
    analysed = stats.entropy_directories_sampled or len(samples)
    logging.info(
        _("Analysed %s directories (%s below threshold)."),
        analysed,
        stats.entropy_directories_below_threshold,
    )
    
    if verbosity >= 1:
        threshold_bytes = stats.entropy_report_threshold_bytes
        if threshold_bytes:
            logging.info(
                _("Reporting directories with total size >= %.1f MB."),
                threshold_bytes / (1024 * 1024),
            )
        logging.info(_("Directories ordered by projected savings:"))

        for index, record in enumerate(samples, start=1):
            status_note = _(" [below threshold]") if record.estimated_savings < min_savings_percent else ""
            logging.info(
                " %2d. %s (~%.1f%% savings, entropy %.2f, %s files, %s sampled, %s total)%s",
                index,
                record.relative_path,
                record.estimated_savings,
                record.average_entropy,
                record.sampled_files,
                _format_sample_bytes(record.sampled_bytes),
                _format_sample_bytes(record.total_bytes),
                status_note,
            )

    if stats.entropy_projected_original_bytes:
        original = stats.entropy_projected_original_bytes
        base_compressed = stats.entropy_projected_compressed_bytes
        alt_compressed = stats.entropy_projected_compressed_bytes_conservative

        def _log_savings_line(comp_bytes: int, label: str) -> None:
            savings = max(0, original - comp_bytes)
            ratio = round((savings / original) * 100) if original > 0 else 0
            logging.info(
                "\t%s -> %s (%d%% %s)",
                _format_summary_size(original),
                _format_summary_size(comp_bytes),
                ratio,
                label,
            )

        logging.info(_("\nEstimated savings:"))
        _log_savings_line(base_compressed, _("with LZX"))
        _log_savings_line(alt_compressed, _("w/o LZX"))


def print_compression_summary(stats: CompressionStats) -> None:
    logging.info(_("\nCompression Summary"))
    logging.info("------------------")
    logging.info(_("Files compressed: %s"), stats.compressed_files)
    logging.info(_("Files skipped: %s"), stats.skipped_files)
    logging.info(
        _("     %s are compressed with Trash-Compactor"),
        stats.already_compressed_files,
    )
    logging.info(
        _("     %s have compressed file types"),
        stats.skip_extension_files,
    )
    logging.info(
        _("     %s fall below %.1f%% projected savings"),
        stats.skip_low_savings_files,
        stats.min_savings_percent,
    )

    if stats.compressed_files == 0:
        logging.info(_("\nThis directory may have already been compressed."))
        return

    total_original = stats.total_original_size
    total_compressed = stats.total_compressed_size
    logging.info(_("\nOriginal size: %.2f MB"), total_original / (1024 * 1024))

    if total_original > 0:
        space_saved = max(0, total_original - total_compressed)
        ratio = (space_saved / total_original) * 100
        logging.info(_("Space saved: %.2f MB"), space_saved / (1024 * 1024))
        logging.info(_("Overall compression ratio: %.2f%%"), ratio)
        logging.info(_("Size after compression: %.2f MB"), total_compressed / (1024 * 1024))

    if stats.errors:
        logging.info(_("\nErrors encountered:"))
        for error in stats.errors:
            logging.error(error)

    # if stats.file_skips:
    #     logging.info("\nSkipped files detail:")
    #     for record in stats.file_skips:
    #         logging.info(" - %s: %s", record.relative_path, record.reason)

