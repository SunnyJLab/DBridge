# -*- mode: python ; coding: utf-8 -*-
# PyInstaller build spec for the Windows release (one-folder, windowed).
#
#   pyinstaller --noconfirm --clean packaging/DBridge.spec
#
# Output: dist/DBridge/DBridge.exe (+ _internal/). The installer (DBridge.iss)
# and the portable zip are both built from this folder.

from pathlib import Path

ROOT = Path(SPECPATH).parent

# oracledb's thin driver is compiled (Cython). Modules imported from inside
# those .pyd files are invisible to PyInstaller's static analysis, so list them
# explicitly: cryptography (thin-mode authentication, impl/thin/crypto.pyx; its
# own PyInstaller hook then pulls in the native bindings) and the stdlib modules.
ORACLEDB_RUNTIME_IMPORTS = [
    "cryptography.x509",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.ciphers",
    "cryptography.hazmat.primitives.asymmetric.padding",
    "cryptography.hazmat.primitives.kdf.pbkdf2",
    "array",
    "asyncio",
    "base64",
    "collections",
    "copy",
    "datetime",
    "decimal",
    "getpass",
    "hashlib",
    "inspect",
    "json",
    "locale",
    "random",
    "re",
    "secrets",
    "select",
    "socket",
    "ssl",
    "string",
    "subprocess",
    "threading",
    "uuid",
    "warnings",
]

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "theme_sv"), "theme_sv")],
    hiddenimports=ORACLEDB_RUNTIME_IMPORTS,
    excludes=["unittest", "pydoc", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DBridge",
    console=False,
    upx=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    name="DBridge",
    upx=False,
)
