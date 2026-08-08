# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Observation Workbench.

Build locally with:
    pyinstaller packaging/observation_workbench.spec --noconfirm --clean

Produces a single-directory onedir bundle under dist/ObservationWorkbench/
(dist/ObservationWorkbench.app on macOS). GitHub Actions
(.github/workflows/build-executables.yml) runs this same spec on
Windows/macOS/Linux runners to produce release artifacts.
"""
import sys
import tomllib
from pathlib import Path

ROOT = Path(SPECPATH).parent

with open(ROOT / "pyproject.toml", "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]

APP_NAME = "ObservationWorkbench"

# Windows uses .ico, macOS uses .icns; PyInstaller ignores icon= on Linux.
ICON_ICO = str(ROOT / "packaging" / "icon" / "icon.ico")
ICON_ICNS = str(ROOT / "packaging" / "icon" / "icon.icns")

block_cipher = None

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # Help ▸ View Readme reads README.md from the bundle root at runtime.
    datas=[(str(ROOT / "README.md"), ".")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=ICON_ICO if sys.platform == "win32" else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=ICON_ICNS,
        bundle_identifier="com.alanrockefeller.observationworkbench",
        info_plist={
            "CFBundleShortVersionString": VERSION,
            "CFBundleVersion": VERSION,
            "NSHighResolutionCapable": True,
        },
    )
