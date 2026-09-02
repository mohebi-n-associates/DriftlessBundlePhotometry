"""PyInstaller recipe for the self-contained Windows desktop application."""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

ROOT = Path.cwd()
ICON = ROOT / "build" / "windows" / "driftless-photometry.ico"

datas = collect_data_files("driftless_photometry")
hiddenimports = []
binaries = []

# PyNWB and its extensions discover schemas and classes dynamically. Collect their
# package data and submodules explicitly so the frozen application can write, reopen,
# and validate NWB files without a separate Python installation.
for package_name in (
    "hdmf",
    "pynwb",
    "ndx_fiber_photometry",
    "ndx_ophys_devices",
    "nwbinspector",
):
    package_datas, package_binaries, package_hiddenimports = collect_all(package_name)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

for distribution_name in (
    "driftless-bundle-photometry",
    "hdmf",
    "h5py",
    "ndx-fiber-photometry",
    "ndx-ophys-devices",
    "numpy",
    "nwbinspector",
    "platformdirs",
    "pydantic",
    "pynwb",
    "pyqtgraph",
    "pyserial",
):
    datas += copy_metadata(distribution_name)

analysis = Analysis(
    [str(ROOT / "src" / "driftless_photometry" / "desktop.py")],
    pathex=[str(ROOT / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
python_archive = PYZ(analysis.pure)

executable = EXE(
    python_archive,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="DriftlessBundlePhotometry",
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
    icon=str(ICON),
)

application = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DriftlessBundlePhotometry",
)
