"""Verify scan populates on-disk size separately from logical size."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.compression.compression_planner import plan_compression
from src.compression.file_scan import CountingDirEntryIter, iter_files
from src.gui.summary import make_stats_summary
from src.stats import CompressionStats
from src.timer import PerformanceMonitor


def _scan_root(root: Path, *, apply_entropy: bool) -> tuple[CompressionStats, int, int, float]:
    stats = CompressionStats()
    monitor = PerformanceMonitor()
    started = time.perf_counter()
    files = CountingDirEntryIter(
        iter_files(root, stats, verbosity=0, min_savings_percent=15.0)
    )
    plan = plan_compression(
        files,
        stats,
        monitor,
        base_dir=root,
        min_savings_percent=15.0,
        verbosity=0,
        apply_entropy_filter=apply_entropy,
    )
    elapsed = time.perf_counter() - started
    return stats, files.count, len(plan), elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, nargs="?", default=Path.cwd())
    parser.add_argument(
        "--skip-entropy",
        action="store_true",
        help="Only validate scan/check accounting (faster)",
    )
    parser.add_argument(
        "--require-compressed",
        action="store_true",
        help="Fail unless already_compressed_files > 0",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if not root.is_dir():
        print(f"not a directory: {root}")
        return 1

    stats, file_count, plan_count, elapsed = _scan_root(root, apply_entropy=not args.skip_entropy)
    total_compressible_size = max(
        0,
        stats.total_original_size
        - stats.already_compressed_logical_size
        - max(0, stats.total_skipped_size - stats.already_compressed_logical_size),
    )
    summary = make_stats_summary(
        stats,
        plan_count,
        total_compressible_size,
        min_savings_percent=15.0,
        is_analysis=True,
    )

    logical = stats.total_original_size
    on_disk = stats.total_on_disk_size or summary["current_on_disk_size"]
    compressed_files = stats.already_compressed_files
    compressed_physical = stats.already_compressed_physical_size

    print(f"root={root}")
    print(f"files={file_count} plan={plan_count} elapsed={elapsed:.2f}s")
    print(f"logical={logical} on_disk={on_disk}")
    print(f"already_compressed_files={compressed_files}")
    print(f"already_compressed_physical={compressed_physical}")

    if args.require_compressed and compressed_files == 0:
        print("expected already-compressed files but found none")
        return 1

    if compressed_files > 0 and compressed_physical >= stats.already_compressed_logical_size:
        print("already-compressed physical size should be below logical size")
        return 1

    if compressed_files > 0 and on_disk >= logical:
        print("on-disk total should be below logical total when compressed files exist")
        return 1

    print("scan accuracy checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())