import os
import sys


def _preload_fast_walk() -> None:
    if not getattr(sys, "frozen", False):
        return
    if os.getenv("TRASH_COMPACTOR_USE_FAST_WALK", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return

    import importlib

    meipass = getattr(sys, "_MEIPASS", "")
    pkg_dir = os.path.join(meipass, "fast_walk")
    if os.path.isdir(pkg_dir) and meipass and meipass not in sys.path:
        sys.path.insert(0, meipass)

    importlib.import_module("fast_walk.fast_walk")


_preload_fast_walk()