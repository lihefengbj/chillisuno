# PyInstaller 打包配置（Windows / macOS 共用）
# 用法：pyinstaller --noconfirm chillisuno.spec

import sys

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[("assets", "assets")],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="chillisuno",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/icon.ico" if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    name="chillisuno",
)

# macOS 产出 .app（icon.icns 由 CI 在 mac runner 上用 iconutil 生成）
if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="chillisuno.app",
        icon="assets/icon.icns",
        bundle_identifier="com.chillisuno.app",
    )
