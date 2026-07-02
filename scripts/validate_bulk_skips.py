"""Verify record_bulk_skips matches per-file record_file_skip_counters totals."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.stats import CompressionStats, SkipBulkLedger


def _apply_per_file(stats: CompressionStats, rows: list[tuple[str, int, str]]) -> None:
    for hint, size, category, already_compressed in rows:
        stats.record_file_skip_counters(
            hint,
            size,
            already_compressed=already_compressed,
            category=category,
        )


def _apply_bulk(stats: CompressionStats, rows: list[tuple[str, int, str]]) -> None:
    ledger = SkipBulkLedger()
    for hint, size, category, _already_compressed in rows:
        ledger.add(category, hint, size)
    stats.record_bulk_skips(ledger)


def _counter_fields(stats: CompressionStats) -> dict[str, int]:
    return {
        "skipped_files": stats.skipped_files,
        "total_skipped_physical_size": stats.total_skipped_physical_size,
        "total_compressed_size": stats.total_compressed_size,
        "total_skipped_size": stats.total_skipped_size,
        "already_compressed_files": stats.already_compressed_files,
        "already_compressed_logical_size": stats.already_compressed_logical_size,
        "already_compressed_physical_size": stats.already_compressed_physical_size,
        "excluded_files": stats.excluded_files,
        "excluded_logical_size": stats.excluded_logical_size,
        "excluded_physical_size": stats.excluded_physical_size,
        "skip_extension_files": stats.skip_extension_files,
    }


def main() -> int:
    fixture = [
        (900, 1000, "extension", False),
        (0, 512, "too_small", False),
        (300, 1000, "already_compressed", True),
        (0, 2048, "error", False),
        (750, 800, "extension", False),
    ]

    per_file = CompressionStats()
    _apply_per_file(per_file, fixture)

    bulk = CompressionStats()
    _apply_bulk(bulk, fixture)

    expected = _counter_fields(per_file)
    actual = _counter_fields(bulk)
    if expected != actual:
        print("bulk skip counters diverged")
        for key in expected:
            if expected[key] != actual[key]:
                print(f"  {key}: per_file={expected[key]} bulk={actual[key]}")
        return 1

    print("record_bulk_skips matches per-file counters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())