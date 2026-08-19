# Building the Rust Fast Walk Extension

## Prerequisites

1. Rust 1.72+ with `cargo` and `rustup` installed
2. Python 3.12+ (64-bit) with requirements.txt installed
3. maturin
4. Visual Studio Build Tools for an MSVC linker

## Build and enable

```powershell
cd fast_walk
maturin build --release
python -m pip install target\wheels\fast_walk-*.whl --force-reinstall
cd ..
```

The wheel install is **mandatory** for source runs: `fast_walk/__init__.py` in the repo re-exports the native module from site-packages and raises a clear error (with these build instructions) when the wheel is missing. The `__init__.py` also makes the package importable from a clean clone.

## What it does

- **`walk_and_filter`** — parallel directory walk (rayon) with inline extension/size/already-compressed classification and algorithm precompute. Replaces the old Python `os.scandir` walk + `_scan_path_fast` check phases.
- **`probe_directories_parallel`** — parallel entropy probing (rayon + LZ4 short-circuit + zlib level 2, mmap for large files). Replaces the old `ProcessPoolExecutor` entropy path.

Entropy sampling budgets are configured in `src/config.py` (`ENTROPY_MAX_FILES`, `ENTROPY_MAX_BYTES`).

## PyInstaller Bundling

```powershell
pip install target\wheels\fast_walk-*.whl --force-reinstall
python -m PyInstaller --clean --noconfirm trash-compactor.spec
```

Hooks in `hooks/` collect the native module for onefile builds. Verify with:

```powershell
set TRASH_COMPACTOR_DIAGNOSTIC=1
dist\trash-compactor.exe
```