# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build file. Run from the repository root:
#   pyinstaller packaging/instructional_assistant.spec
# The profiles folder is bundled. The API key is not; put a .env file
# next to the .exe or let the app ask for the key at startup.

a = Analysis(
    ['../instructional_assistant.py'],
    pathex=[],
    binaries=[],
    datas=[('../profiles', 'profiles')],
    hiddenimports=[],
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
    name='InstructionalAssistant',
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
)
