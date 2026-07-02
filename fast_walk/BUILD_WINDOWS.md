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
```
This produces a `.pyd` file in `target/release/` that is bundled by PyInstaller.

```powershell
set TRASH_COMPACTOR_USE_FAST_WALK=1
python main.py
```
## What it does

This extension replaces the single-threaded Python `os.scandir` DFS walk
in `iter_files()` with a parallel `jwalk` + `rayon` implementation to scan directories faster.

## What it does not

- Entropy analysis, which stays in Python via ProcessPoolExecutor
- Per-directory skip logic and incompressible cache
- GUI, i18n, stats, timer modules

## PyInstaller Bundling

```powershell
pip install target\wheels\fast_walk-*.whl --force-reinstall # wildcard may not work in PowerShell
python -m PyInstaller --clean --noconfirm trash-compactor.spec
```

Manual onefile build - add the hooks directory:

```
--additional-hooks-dir hooks --hidden-import fast_walk
```
