# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys

sys.path.insert(0, "src")

from version import VERSION as APP_VERSION

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

_major, _minor, _build = (int(p) for p in APP_VERSION.split(".")[:3])
_version_info = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=(_major, _minor, _build, 0),
        prodvers=(_major, _minor, _build, 0),
        mask=0x3F,
        flags=0x0,
        OS=0x40004,
        fileType=0x1,
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo(
            [
                StringTable(
                    "040904B0",
                    [
                        StringStruct("CompanyName", "Trash-Compactor"),
                        StringStruct("FileDescription", "Trash-Compactor"),
                        StringStruct("FileVersion", APP_VERSION),
                        StringStruct("InternalName", "trash-compactor"),
                        StringStruct("OriginalFilename", "trash-compactor.exe"),
                        StringStruct("ProductName", "Trash-Compactor"),
                        StringStruct("ProductVersion", APP_VERSION),
                    ],
                )
            ]
        ),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)


def _fast_walk_bundle():
    binaries = []
    datas = []
    try:
        import fast_walk as module

        package_dir = Path(module.__file__).resolve().parent
        for artifact in package_dir.glob('fast_walk*.pyd'):
            binaries.append((str(artifact), 'fast_walk'))
        init_py = package_dir / '__init__.py'
        if init_py.is_file():
            datas.append((str(init_py), 'fast_walk'))
        return binaries, datas
    except ImportError:
        pass

    dll = Path('fast_walk/target/release/fast_walk.dll')
    if dll.is_file():
        binaries.append((str(dll), 'fast_walk'))
    return binaries, datas


_fast_walk_binaries, _fast_walk_datas = _fast_walk_bundle()

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=_fast_walk_binaries,
    datas=[('locales', 'locales'), *_fast_walk_datas],
    hiddenimports=[
        'fast_walk',
        'webview',
        'webview.platforms',
        'webview.platforms.edgechromium',
        'webview.http',
        'bottle',
        'proxy_tools',
        'clr_loader',
        'pythonnet',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    name='trash-compactor-user',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=_version_info,
)
