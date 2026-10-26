"""Verify LanBridge Windows release artifacts and their bundled runtime."""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import struct
import subprocess
import tempfile
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.constants import APP_NAME, APP_VERSION


PORTABLE_NAMES = (
    "邻桥快传-接收端.exe",
    "邻桥快传-发送端.exe",
)
SETUP_NAMES = (
    "邻桥快传-接收端-安装包.exe",
    "邻桥快传-发送端-安装包.exe",
)
INSTALLED_NAMES = {
    setup: portable for setup, portable in zip(SETUP_NAMES, PORTABLE_NAMES)
}


def pe_metadata(path: Path) -> tuple[int, tuple[int, int], tuple[int, int]]:
    with path.open("rb") as handle:
        if handle.read(2) != b"MZ":
            raise ValueError(f"{path.name} is not a PE file")
        handle.seek(0x3C)
        pe_offset = struct.unpack("<I", handle.read(4))[0]
        handle.seek(pe_offset)
        if handle.read(4) != b"PE\0\0":
            raise ValueError(f"{path.name} has an invalid PE header")
        machine = struct.unpack("<H", handle.read(2))[0]
        handle.seek(pe_offset + 24)
        optional = handle.read(72)
    if len(optional) < 72 or struct.unpack_from("<H", optional, 0)[0] not in (0x10B, 0x20B):
        raise ValueError(f"{path.name} has an invalid PE optional header")
    os_version = struct.unpack_from("<HH", optional, 40)
    subsystem_version = struct.unpack_from("<HH", optional, 48)
    return machine, os_version, subsystem_version


def assert_windows7_compatible(path: Path) -> None:
    machine, os_version, subsystem_version = pe_metadata(path)
    if machine not in (0x14C, 0x8664):
        raise ValueError(f"{path.name} has an unsupported PE machine (0x{machine:04x})")
    if os_version > (6, 1) or subsystem_version > (6, 1):
        raise ValueError(
            f"{path.name} requires a newer OS in its PE header: "
            f"OS={os_version}, subsystem={subsystem_version}"
        )


def assert_x64_windows7_compatible(path: Path) -> None:
    assert_windows7_compatible(path)
    machine, _os_version, _subsystem_version = pe_metadata(path)
    if machine != 0x8664:
        raise ValueError(f"{path.name} is not x64 (machine=0x{machine:04x})")


def assert_bundled_runtime(path: Path) -> None:
    payload = path.read_bytes()
    required = (b"python38.dll", b"_tkinter.pyd", b"tcl86t.dll", b"tk86t.dll")
    missing = [marker.decode("ascii") for marker in required if marker not in payload]
    if missing:
        raise ValueError(f"{path.name} lacks bundled runtime markers: {', '.join(missing)}")
    incompatible = (
        b"python39.dll",
        b"python310.dll",
        b"python311.dll",
        b"python312.dll",
        b"python313.dll",
    )
    for marker in incompatible:
        if marker in payload:
            raise ValueError(f"{path.name} unexpectedly contains {marker.decode('ascii')}")


def assert_release_metadata(path: Path, *, installed_name: str | None = None) -> None:
    # pefile is a build-time dependency of PyInstaller; release applications
    # never import it. Verify both numeric and displayed Windows versions.
    import pefile

    with pefile.PE(str(path), fast_load=True) as pe:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
        expected_version = tuple(int(part) for part in APP_VERSION.split("."))
        fixed_info = getattr(pe, "VS_FIXEDFILEINFO", [])
        if not fixed_info:
            raise ValueError(f"{path.name} has no Windows version information")
        fixed = fixed_info[0]
        for prefix in ("File", "Product"):
            high = getattr(fixed, f"{prefix}VersionMS")
            low = getattr(fixed, f"{prefix}VersionLS")
            actual = (high >> 16, high & 0xFFFF, low >> 16, low & 0xFFFF)
            if actual != expected_version:
                raise ValueError(f"{path.name} has {prefix} version {actual}, expected {expected_version}")
        strings = {}
        for entries in getattr(pe, "FileInfo", []):
            for entry in entries:
                for table in getattr(entry, "StringTable", []):
                    strings.update({
                        key.decode("utf-8"): value.decode("utf-8").rstrip("\0 ")
                        for key, value in table.entries.items()
                    })
    expected_name = installed_name or path.name
    expected = {
        "FileVersion": APP_VERSION,
        "ProductVersion": APP_VERSION,
        "ProductName": APP_NAME,
        "FileDescription": Path(expected_name).stem,
    }
    if installed_name is None:
        expected["OriginalFilename"] = path.name
    for key, value in expected.items():
        if strings.get(key) != value:
            raise ValueError(f"{path.name} has incorrect {key}: {strings.get(key)!r}; expected {value!r}")


def smoke_test(path: Path, data_dir: Path) -> None:
    env = os.environ.copy()
    env["LANBRIDGE_DATA_DIR"] = str(data_dir)
    result = subprocess.run(
        [str(path), "--smoke-test"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{path.name} smoke test exited with {result.returncode}")


def verify_checksums(dist: Path, names: tuple[str, ...]) -> None:
    checksum_path = dist / "SHA256SUMS.txt"
    expected = {}
    for line in checksum_path.read_text(encoding="utf-8-sig").splitlines():
        digest, name = line.split("  ", 1)
        if name in expected:
            raise ValueError(f"SHA256SUMS.txt lists {name} more than once")
        expected[name] = digest
    if set(expected) != set(names):
        raise ValueError("SHA256SUMS.txt does not list exactly the release artifacts")
    for name in names:
        actual = hashlib.sha256((dist / name).read_bytes()).hexdigest()
        if actual != expected[name]:
            raise ValueError(f"SHA-256 mismatch for {name}")


def verify_installer(setup: Path, temp_root: Path) -> None:
    install_dir = temp_root / setup.stem
    result = subprocess.run(
        [
            str(setup),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/SP-",
            "/NOICONS",
            "/LANBRIDGEVERIFY=1",
            f"/DIR={install_dir}",
        ],
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"{setup.name} silent installation exited with {result.returncode}")
    installed = install_dir / INSTALLED_NAMES[setup.name]
    uninstallers = list(install_dir.glob("unins*.exe"))
    if len(uninstallers) != 1:
        raise RuntimeError(f"{setup.name} did not create exactly one uninstaller")
    try:
        if not installed.is_file():
            raise RuntimeError(f"{setup.name} did not install the expected executable")
        portable = setup.parent / installed.name
        if hashlib.sha256(installed.read_bytes()).digest() != hashlib.sha256(portable.read_bytes()).digest():
            raise RuntimeError(f"{setup.name} installed a different executable from the portable release")
        assert_x64_windows7_compatible(installed)
        assert_bundled_runtime(installed)
        assert_release_metadata(installed)
        smoke_test(installed, temp_root / f"data-{setup.stem}")
    finally:
        result = subprocess.run(
            [str(uninstallers[0]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            timeout=90,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"{setup.name} silent uninstallation exited with {result.returncode}")
    if installed.exists():
        raise RuntimeError(f"{setup.name} left the application executable after uninstallation")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, default=Path("dist"))
    parser.add_argument("--skip-installers", action="store_true")
    args = parser.parse_args()
    dist = args.dist.resolve()
    names = PORTABLE_NAMES if args.skip_installers else PORTABLE_NAMES + SETUP_NAMES
    for name in names:
        path = dist / name
        if not path.is_file() or path.stat().st_size < 1_000_000:
            raise ValueError(f"missing or invalid release artifact: {name}")
        if name in PORTABLE_NAMES:
            assert_x64_windows7_compatible(path)
            assert_release_metadata(path)
        else:
            # Inno Setup may use an x86 bootstrapper for an x64-only install.
            assert_windows7_compatible(path)
            assert_release_metadata(path, installed_name=INSTALLED_NAMES[name])
    for name in PORTABLE_NAMES:
        assert_bundled_runtime(dist / name)
    verify_checksums(dist, names)
    with tempfile.TemporaryDirectory(prefix="lanbridge-verify-") as temp:
        temp_root = Path(temp)
        for name in PORTABLE_NAMES:
            smoke_test(dist / name, temp_root / f"data-{Path(name).stem}")
        if not args.skip_installers:
            for name in SETUP_NAMES:
                verify_installer(dist / name, temp_root)
        shutil.rmtree(temp_root, ignore_errors=True)
    print(
        f"Verified {len(names)} Windows artifacts: {APP_VERSION}, Chinese product names, x64, bundled Python 3.8/Tk, "
        "offline smoke tests passed."
    )


if __name__ == "__main__":
    main()
