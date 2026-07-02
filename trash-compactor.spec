# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path


def _assert_fast_walk_bundled() -> None:
    try:
        import fast_walk as module

        package_dir = Path(module.__file__).resolve().parent
        if not any(package_dir.glob("fast_walk*.pyd")):
            raise SystemExit(
                "fast_walk .pyd not found. Build the wheel first:\n"
                "  cd fast_walk && maturin build --release\n"
                "  pip install target/wheels/fast_walk-*.whl --force-reinstall"
            )
    except ImportError:
        dll = Path("fast_walk/target/release/fast_walk.dll")
        if not dll.is_file():
            raise SystemExit(
                "fast_walk is not installed and fast_walk/target/release/fast_walk.dll "
                "is missing. Install the wheel before running PyInstaller."
            )


_assert_fast_walk_bundled()

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('locales', 'locales'),
        ('src/gui/ui', 'src/gui/ui'),
    ],
    hiddenimports=['fast_walk', 'fast_walk.fast_walk'],
    hookspath=['hooks'],
    hooksconfig={},
    runtime_hooks=['hooks/rthook_fast_walk.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='trash-compactor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[
        'fast_walk*.pyd',
        'python313.dll',
        'vcruntime140.dll',
        'vcruntime140_1.dll',
    ],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)