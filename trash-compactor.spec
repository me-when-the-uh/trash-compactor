# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[('locales', 'locales'), ('src', 'src')],
    hiddenimports=[
        'src',
        'src.launch',
        'src.console',
        'src.runtime',
        'src.i18n',
        'src.config',
        'src.file_utils',
        'src.compression_module',
        'src.skip_logic',
        'src.stats',
        'src.timer',
        'src.workers',
        'src.compression',
        'src.compression.compression_executor',
        'src.compression.compression_planner',
        'src.compression.entropy'
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
    name='trash-compactor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
)
