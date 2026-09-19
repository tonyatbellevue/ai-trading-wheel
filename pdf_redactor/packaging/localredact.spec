# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for LocalRedact.

Build from the project root (the folder containing run_app.py):

    pyinstaller packaging/localredact.spec --noconfirm

Produces dist/LocalRedact.exe - a single self-contained file with no installer
and no runtime downloads.

Two deliberate choices:

* ``console=False`` so double-clicking the .exe does not flash a terminal.
  The CLI still works via ``python -m app.cli`` from a source checkout.
* networking packages are listed in ``excludes``. Nothing imports them, and
  excluding them means the shipped binary provably cannot make an HTTP request
  even if a future dependency tried.
"""

import os

block_cipher = None

# PyInstaller resolves relative paths inside a .spec against the SPEC FILE's
# directory, not the working directory, so "run_app.py" here would be looked up
# as packaging/run_app.py. Anchor everything to an absolute project root taken
# from SPECPATH (a global PyInstaller injects into spec files) and the spec then
# builds correctly no matter where it is invoked from.
try:
    PROJECT_ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
except NameError:  # pragma: no cover - spec executed outside PyInstaller
    PROJECT_ROOT = os.path.abspath(os.getcwd())

# If a Tesseract install is copied to ./tesseract it is bundled beside the exe,
# giving fully self-contained offline OCR. Leave the folder absent to build
# without OCR; the app degrades gracefully and says so in the UI.
_tesseract_dir = os.path.join(PROJECT_ROOT, "tesseract")
_packaging = os.path.join(PROJECT_ROOT, "packaging")

# The window icon is loaded at runtime from <bundle>/packaging, so the icon
# files have to travel inside the exe as data. The exe's own shell icon is set
# separately by the `icon=` argument to EXE() below - they are two different
# mechanisms and both are needed.
datas = [
    (os.path.join(_packaging, name), "packaging")
    for name in ("localredact.ico", "localredact_256.png", "localredact_64.png")
    if os.path.isfile(os.path.join(_packaging, name))
]
if os.path.isdir(_tesseract_dir):
    datas.append((_tesseract_dir, "tesseract"))

a = Analysis(
    [os.path.join(PROJECT_ROOT, "run_app.py")],
    pathex=[PROJECT_ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "pymupdf",
        "app.gui",
        "app.cli",
        "app.detectors",
        "app.redactor",
        "app.scanner",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # No network stack is needed. Excluding these keeps the binary small and
        # makes the "never uploads anything" claim structural, not just a policy.
        "requests", "urllib3", "http.client", "ftplib", "smtplib",
        "telnetlib", "xmlrpc", "socketserver",
        # Heavy scientific / plotting stacks that PyInstaller sometimes drags in.
        "matplotlib", "scipy", "pandas", "numpy.distutils",
        "IPython", "jupyter", "notebook", "pytest", "setuptools._distutils",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="LocalRedact",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off on purpose: compressed binaries are a common antivirus
    # false-positive trigger, and this tool has to look trustworthy.
    upx=False,
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=(
        os.path.join(PROJECT_ROOT, "packaging", "localredact.ico")
        if os.path.isfile(os.path.join(PROJECT_ROOT, "packaging", "localredact.ico"))
        else None
    ),
    version=(
        os.path.join(PROJECT_ROOT, "packaging", "version_info.txt")
        if os.path.isfile(os.path.join(PROJECT_ROOT, "packaging", "version_info.txt"))
        else None
    ),
)
