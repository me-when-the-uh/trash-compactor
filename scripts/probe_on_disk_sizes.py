"""Print scan on-disk vs logical totals for a directory."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.compression.compression_planner import plan_compression
from src.compression.file_scan import CAT_ALREADY_COMPRESSED, CountingDirEntryIter, iter_files
from src.gui.summary import make_stats_summary
from src.stats import CompressionStats
from src.timer import PerformanceMonitor


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    stats = CompressionStats()
    monitor = PerformanceMonitor()
    files = CountingDirEntryIter(iter_files(root, stats, 0, 15.0))
    plan = plan_compression(
        files,
        stats,
        monitor,
        base_dir=root,
        min_savings_percent=15.0,
        verbosity=0,
        apply_entropy_filter=False,
    )
    excluded_logical = max(0, stats.total_skipped_size - stats.already_compressed_logical_size)
    total_compressible = max(
        0,
        stats.total_original_size - stats.already_compressed_logical_size - excluded_logical,
    )
    summary = make_stats_summary(
        stats,
        len(plan),
        total_compressible,
        min_savings_percent=15.0,
        is_analysis=True,
    )
    print(f"root={root}")
    print(f"files={files.count}")
    print(f"logical={stats.total_original_size}")
    print(f"on_disk={summary['current_on_disk_size']}")
    print(f"already_compressed_files={stats.already_compressed_files}")
    print(f"already_compressed_logical={stats.already_compressed_logical_size}")
    print(f"already_compressed_physical={stats.already_compressed_physical_size}")
    print(f"plan_candidates={len(plan)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())