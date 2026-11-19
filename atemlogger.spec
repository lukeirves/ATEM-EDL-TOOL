# -*- mode: python ; coding: utf-8 -*-

import os

codesign_identity = os.environ.get('ATEMLOGGER_CODESIGN_IDENTITY') or None

a = Analysis(
    ['atemlogger7.py'],
    pathex=[],
    binaries=[],
    datas=[],
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
    [],
    exclude_binaries=True,
    name='ATEM-EDL-TOOL',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    target_arch=None,
    codesign_identity=codesign_identity,
    entitlements_file=None,
    icon=['assets/atem-logger.icns'],
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='ATEM-EDL-TOOL')
app = BUNDLE(
    coll,
    name='ATEM-EDL-TOOL.app',
    icon='assets/atem-logger.icns',
    bundle_identifier='com.teliotortay.atemlogger',
    info_plist={
        'CFBundleShortVersionString': '0.5.7',
        'CFBundleVersion': '0.5.7',
        'NSLocalNetworkUsageDescription': 'ATEM-EDL-TOOL connects to your ATEM switcher and HyperDeck on the local network to read video inputs and timecode.',
    },
)
