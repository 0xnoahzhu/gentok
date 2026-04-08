# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec file for etoken app."""

import os
import importlib
from PyInstaller.utils.hooks import collect_submodules

import glob
import sys

playwright_path = os.path.dirname(importlib.import_module('playwright').__file__)

# Find the Playwright browsers directory and bundle Chromium
_browsers_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
if not _browsers_path:
    if sys.platform == "darwin":
        _browsers_path = os.path.join(os.path.expanduser("~"), "Library", "Caches", "ms-playwright")
    elif sys.platform == "win32":
        _browsers_path = os.path.join(os.path.expanduser("~"), "AppData", "Local", "ms-playwright")
    else:
        _browsers_path = os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright")

# Build the datas list
datas = [
    # Templates (read-only, accessed via sys._MEIPASS)
    ('templates', 'templates'),
    # The entire playwright package (driver contains Node.js binary)
    (playwright_path, 'playwright'),
]

# Find and bundle all Playwright Chromium directories
# (both chromium-* and chromium_headless_shell-* are needed)
_bundled_any = False
for _pattern in ("chromium-*", "chromium_headless_shell-*"):
    for _dir in glob.glob(os.path.join(_browsers_path, _pattern)):
        datas.append((_dir, os.path.join("browsers", os.path.basename(_dir))))
        _bundled_any = True

if not _bundled_any:
    print(f"WARNING: No Chromium found in {_browsers_path}. "
          "Run 'playwright install chromium' before building.")
    sys.exit(1)

# Collect all playwright submodules for hidden imports
hidden_imports = [
    # playwright._impl.* modules
    *collect_submodules('playwright._impl'),
    # playwright.async_api.* modules
    *collect_submodules('playwright.async_api'),
    # playwright.sync_api.* modules
    *collect_submodules('playwright.sync_api'),
    # Other dependencies
    'dotenv',
    'flask',
    'jinja2.ext',
]

a = Analysis(
    ['webapp.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

# Filter out UPX from all binaries — it corrupts Playwright's Node binary
for bin_item in a.binaries:
    pass  # UPX handled via upx=False below

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='etoken',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # CRITICAL: UPX corrupts Playwright's Node binary
    console=True,  # Keep console for log output
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,  # CRITICAL: UPX corrupts Playwright's Node binary
    name='etoken',
)
