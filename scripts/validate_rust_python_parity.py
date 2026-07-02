"""Compare Rust walk_and_filter NTFS checks against Python is_file_compressed."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.compression.file_scan import CAT_ALREADY_COMPRESSED, CAT_DEBUG_EXT, CAT_ELIGIBLE, iter_files
from src.file_utils import is_file_compressed
from src.stats import CompressionStats


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    stats = CompressionStats()
    mismatches = 0
    checked = 0

    for path, size, attributes, _algo, category, hint in iter_files(
        root, stats, verbosity=0, min_savings_percent=15.0
    ):
        if category not in (CAT_ELIGIBLE, CAT_DEBUG_EXT, CAT_ALREADY_COMPRESSED):
            continue
        py_compressed, py_hint = is_file_compressed(path, actual_size=size, attributes=attributes)
        checked += 1
        rust_compressed = category == CAT_ALREADY_COMPRESSED
        if py_compressed != rust_compressed:
            mismatches += 1
            print(f"classification mismatch: {path}")
            continue
        if py_compressed and hint != py_hint:
            mismatches += 1
            print(f"hint mismatch: {path} rust={hint} python={py_hint}")

    print(f"root={root} checked={checked} mismatches={mismatches}")
    return 1 if mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())