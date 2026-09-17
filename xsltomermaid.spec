# PyInstaller spec for xsltomermaid.
#
# Build a standalone executable that bundles Python, PySide6 (incl. QtWebEngine),
# and the vendored mermaid.js. Run with:
#
#     uv run pyinstaller xsltomermaid.spec
#
# Output:
#   * one-file  -> dist/xsltomermaid[.exe]              (default)
#   * one-dir   -> dist/xsltomermaid/xsltomermaid[.exe] (set XSLTOMERMAID_ONEFILE=0)
#
# QtWebEngine is large and, in rare setups, misbehaves when packed one-file. If the
# "Rendered diagram" tab is blank in the one-file build, rebuild one-dir:
#
#     XSLTOMERMAID_ONEFILE=0 uv run pyinstaller xsltomermaid.spec   (bash)
#     set XSLTOMERMAID_ONEFILE=0 && uv run pyinstaller xsltomermaid.spec   (cmd)

import os

ONEFILE = os.environ.get("XSLTOMERMAID_ONEFILE", "1") != "0"

# Bundle the vendored mermaid.js and the app icon next to the app.
datas = [
    ("vendor/mermaid.min.js", "vendor"),
    ("assets/app_icon.ico", "assets"),
    ("assets/app_icon.png", "assets"),
]

ICON = "assets/app_icon.ico"

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="xsltomermaid",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        runtime_tmpdir=None,
        console=False,  # windowed GUI app
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="xsltomermaid",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=ICON,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="xsltomermaid",
    )
