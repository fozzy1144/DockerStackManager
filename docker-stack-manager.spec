# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the Windows executable.

    pip install -e ".[dev]"
    pyinstaller docker-stack-manager.spec

Produces a single ``dist/DockerStackManager.exe`` that needs no Python install
on the target machine. Two things here are not automatic and are the usual cause
of a build that runs on the dev box and dies on someone else's:

* CustomTkinter loads its themes and assets from files at runtime, so they have
  to be collected as data — without them the app exits on the first widget.
* keyring finds its backends through entry points, which PyInstaller's static
  analysis cannot see. The Windows backend is named explicitly, and
  ``win32timezone`` comes with it as an indirect pywin32 import.
"""

from PyInstaller.utils.hooks import collect_data_files

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=collect_data_files("customtkinter"),
    hiddenimports=[
        "keyring.backends.Windows",
        "win32timezone",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Test packages get pulled in transitively and are dead weight in a release.
    excludes=["tkinter.test", "test"],
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
    name="DockerStackManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed executables trip antivirus heuristics.
    runtime_tmpdir=None,
    console=False,  # A Tk app has no use for a console window.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
