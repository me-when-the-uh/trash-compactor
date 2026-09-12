from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Union

from .i18n import _
from .stats import CompressionStats

def pretty_size(n: int) -> str:
    try:
        value = int(n)
    except (TypeError, ValueError):
        return f"{n} B"
    if value < 1024:
        return f"{value} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024.0
        if value < 1024.0:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PB"


class _NullCliLog:
    """Stand-in used when --log-file is not in effect. Every method is a no-op."""

    def header(self, *_args, **_kwargs) -> None: pass
    def mode(self, *_args, **_kwargs) -> None: pass
    def settings(self, *_args, **_kwargs) -> None: pass
    def dry_run_summary(self, *_args, **_kwargs) -> None: pass
    def compression_summary(self, *_args, **_kwargs) -> None: pass
    def timing(self, *_args, **_kwargs) -> None: pass
    def by_algorithm(self, *_args, **_kwargs) -> None: pass
    def skipped_directories(self, *_args, **_kwargs) -> None: pass
    def errors(self, *_args, **_kwargs) -> None: pass
    def finish(self, *_args, **_kwargs) -> None: pass
    def disable(self) -> None: pass


class CliLog:
    """Mirrors the end-of-run stdout blocks into a log file. Not a dashboard: no in-place updates, no spinners."""

    def __init__(self, path: Path) -> None:
        self._fh = open(path, "w", encoding="utf-8", buffering=1)
        self._disabled = False

    @classmethod
    def enable(cls, path: Optional[Path]) -> Union["CliLog", "_NullCliLog"]:
        if path is None:
            return _NullCliLog()
        try:
            return cls(path)
        except OSError as exc:
            logging.warning("Cannot open log file %s: %s", path, exc)
            return _NullCliLog()

    def _write(self, line: str = "") -> None:
        if self._disabled:
            return
        try:
            self._fh.write(line + "\n")
            self._fh.flush()
        except OSError as exc:
            logging.warning("Log write failed: %s", exc)
            self._disabled = True

    def header(self, version: str) -> None:
        self._write(
            f"=== trash-compactor v{version} started "
            f"{datetime.now().isoformat(timespec='seconds')} ==="
        )

    def mode(self, mode_name: str, target: Union[str, Sequence[str]]) -> None:
        if isinstance(target, (list, tuple)):
            self._write(f"Mode: {mode_name}  Targets: {len(target)}")
            for t in target:
                self._write(f"  - {t}")
        else:
            self._write(f"Mode: {mode_name}")
            self._write(f"Target: {target}")

    def settings(self, min_savings: float, lzx: str, hdd: str) -> None:
        self._write(
            f"Min savings: {min_savings:.1f}%   LZX: {lzx}   HDD: {hdd}"
        )
        self._write("")

    def dry_run_summary(
        self,
        stats: CompressionStats,
        min_savings: float,
        active_large: str,
    ) -> None:
        self._write("Dry Run Summary")
        self._write("-----------------------")
        self._write(f"Minimum savings threshold: {min_savings:.1f}%")
        proj = (
            stats.entropy_projected_size
            if active_large == "LZX"
            else stats.entropy_projected_size_conservative
        )
        self._write(
            f"Estimated savings: {pretty_size(stats.entropy_projected_original_bytes)} -> "
            f"{pretty_size(proj)}"
        )

    def compression_summary(self, stats: CompressionStats) -> None:
        self._write("")
        self._write("Compression Summary")
        self._write("------------------")
        self._write(f"Files compressed: {stats.compressed_files}")
        self._write(f"Files skipped: {stats.skipped_files}")
        self._write(f"  {stats.already_compressed_files} are compressed with Trash-Compactor")
        self._write(f"  {stats.skip_extension_files} have compressed file types")
        self._write(
            f"  {stats.skip_low_savings_files} fall below "
            f"{stats.min_savings_percent:.1f}% projected savings"
        )

        if stats.compressed_files == 0:
            self._write("")
            self._write("This directory may have already been compressed.")
            return

        self._write(f"Original size: {pretty_size(stats.total_original_size)}")

        if stats.total_original_size > 0:
            space_saved = max(0, stats.total_original_size - stats.total_compressed_size)
            ratio = (space_saved / stats.total_original_size) * 100
            self._write(f"Space saved: {pretty_size(space_saved)}")
            self._write(f"Overall compression ratio: {ratio:.2f}%")
            self._write(f"Size after compression: {pretty_size(stats.total_compressed_size)}")

        if stats.errors:
            self._write("")
            self._write("Errors encountered:")
            for e in stats.errors:
                self._write(f"  {e}")

    def by_algorithm(
        self,
        stats: CompressionStats,
        lzx_disabled: bool,
        lzx_disabled_reason: Optional[str],
    ) -> None:
        self._write("")
        self._write(_("By algorithm:"))
        if lzx_disabled:
            self._write(f"  LZX       : disabled ({lzx_disabled_reason or 'disabled'})")
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
            self._write(
                f"  {name:<10}: {files:>5} files, "
                f"{pretty_size(b.bytes_planned)} -> {pretty_size(post)} "
                f"({saved_pct:>5.1f}% saved)"
            )

    def timing(self, monitor) -> None:
        self._write("")
        self._write("Performance summary")
        s = monitor.stats
        self._write(f"  elapsed total  {s.total_time:.3f}s")
        if s.file_scan_time:
            self._write(f"  scan duration  {s.file_scan_time:.3f}s")
        if s.entropy_analysis_time:
            self._write(f"  entropy check  {s.entropy_analysis_time:.3f}s")
        if getattr(s, "work_duration", 0):
            self._write(f"  work duration  {s.work_duration:.3f}s")
        if s.total_files:
            self._write(f"  files handled  {s.total_files}")
        if s.files_compressed:
            self._write(f"    compressed   {s.files_compressed}")
        if s.files_skipped:
            self._write(f"    skipped      {s.files_skipped}")

    def skipped_directories(self, stats: CompressionStats, verbosity: int) -> None:
        if not stats.directory_skips:
            return
        by_cat: dict = {}
        for r in stats.directory_skips:
            by_cat.setdefault(r.category, []).append(r)
        if "directstorage" not in by_cat and verbosity < 1:
            return
        self._write("")
        if "directstorage" in by_cat:
            self._write(
                f"Skipped {len(by_cat['directstorage'])} DirectStorage game directories:"
            )
            for r in by_cat["directstorage"]:
                self._write(f"  - {r.format_line()}")
        if verbosity < 1:
            return
        if "high_entropy" in by_cat:
            self._write(
                f"Skipped {len(by_cat['high_entropy'])} directories due to low expected savings:"
            )
            for r in by_cat["high_entropy"]:
                self._write(f"  - {r.relative_path} - {r.reason}")
        if "user" in by_cat:
            self._write(
                f"Skipped {len(by_cat['user'])} user-excluded directories:"
            )
            for r in by_cat["user"]:
                self._write(f"  - {r.relative_path} - {r.reason}")

    def errors(self, stats: CompressionStats) -> None:
        if not stats.errors:
            return
        self._write("")
        self._write("Errors:")
        for e in stats.errors:
            self._write(f"  {e}")

    def finish(self, exit_code: int) -> None:
        self._write(
            f"=== finished: {datetime.now().isoformat(timespec='seconds')}   "
            f"exit code: {exit_code} ==="
        )
        try:
            self._fh.close()
        except OSError:
            pass
        self._disabled = True

    def disable(self) -> None:
        try:
            self._fh.close()
        except OSError:
            pass
        self._disabled = True


_instance: Union[CliLog, _NullCliLog] = _NullCliLog()


def get_cli_log() -> Union[CliLog, _NullCliLog]:
    return _instance


def set_cli_log(inst: Union[CliLog, _NullCliLog]) -> None:
    global _instance
    _instance = inst
