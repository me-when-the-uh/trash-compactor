"""Run inside dev or frozen builds to verify fast_walk loads."""
from __future__ import annotations

import sys

from src.compression.file_scan import fast_walk_available


def main() -> int:
    frozen = getattr(sys, "frozen", False)
    meipass = getattr(sys, "_MEIPASS", "")
    print(f"frozen={frozen} meipass={meipass!r}")
    print(f"fast_walk_available={fast_walk_available()}")
    if not fast_walk_available():
        return 1
    import fast_walk

    print(f"fast_walk_file={fast_walk.__file__}")
    print(f"walk_files={fast_walk.walk_files}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())