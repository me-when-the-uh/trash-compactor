"""Native acceleration module for trash-compactor (PyO3 extension).

The compiled extension (``fast_walk*.pyd``) provides the parallel directory
walk and entropy probing. It is built with ``maturin`` and installed into
site-packages. This re-export keeps development runs working when the
repository itself is on ``sys.path``. The extension's public names are
re-exported here and ``EXTENSION_PATH`` is the file that got loaded.
"""

import importlib.util
import os
import sys


def _find_extension():
    """Locate the compiled ``fast_walk`` extension file."""
    candidates = []
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(here)

    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        candidates.append(os.path.join(meipass, "fast_walk"))

    import site

    for sp in getattr(site, "getsitepackages", lambda: [])():
        candidates.append(os.path.join(sp, "fast_walk"))

    for directory in candidates:
        try:
            entries = os.listdir(directory)
        except OSError:
            continue
        for name in entries:
            if name.startswith("fast_walk") and name.endswith((".pyd", ".dll")):
                return os.path.join(directory, name)
    return None


EXTENSION_PATH = _find_extension()
if EXTENSION_PATH is None:
    raise ImportError(
        "fast_walk native module not found. Build and install it first:\n"
        "  cd fast_walk && maturin build --release\n"
        "  pip install target\\wheels\\fast_walk-*.whl --force-reinstall"
    )

# The extension exports PyInit_fast_walk, so the loader must see the module
# named exactly "fast_walk".  Load it under that name and stash it privately.
_spec = importlib.util.spec_from_file_location("fast_walk", EXTENSION_PATH)
_ext = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_ext)
sys.modules["fast_walk._native"] = _ext

for _name in dir(_ext):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_ext, _name)

del _name, _ext, _spec, _find_extension
