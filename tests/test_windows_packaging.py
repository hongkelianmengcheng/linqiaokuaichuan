from __future__ import annotations

import hashlib
import ast
import re
import struct
import tempfile
import unittest
from pathlib import Path

from scripts.verify_windows_artifacts import (
    APP_VERSION,
    INSTALLED_NAMES,
    PORTABLE_NAMES,
    assert_bundled_runtime,
    assert_x64_windows7_compatible,
    verify_checksums,
)


def _write_pe(path: Path, *, machine: int = 0x8664, subsystem: tuple[int, int] = (6, 1)) -> None:
    content = bytearray(512)
    content[:2] = b"MZ"
    struct.pack_into("<I", content, 0x3C, 0x80)
    content[0x80:0x84] = b"PE\0\0"
    struct.pack_into("<H", content, 0x84, machine)
    optional_offset = 0x80 + 24
    struct.pack_into("<H", content, optional_offset, 0x20B)
    struct.pack_into("<HH", content, optional_offset + 40, 6, 1)
    struct.pack_into("<HH", content, optional_offset + 48, *subsystem)
    path.write_bytes(content)


class WindowsPackagingTests(unittest.TestCase):
    def test_accepts_x64_windows_7_pe_header(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "app.exe"
            _write_pe(path)
            assert_x64_windows7_compatible(path)

    def test_rejects_newer_subsystem_and_non_x64(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            newer = Path(temp) / "newer.exe"
            x86 = Path(temp) / "x86.exe"
            _write_pe(newer, subsystem=(10, 0))
            _write_pe(x86, machine=0x14C)
            with self.assertRaisesRegex(ValueError, "newer OS"):
                assert_x64_windows7_compatible(newer)
            with self.assertRaisesRegex(ValueError, "not x64"):
                assert_x64_windows7_compatible(x86)

    def test_bundled_runtime_requires_python38_and_tk(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            valid = Path(temp) / "valid.exe"
            invalid = Path(temp) / "invalid.exe"
            valid.write_bytes(b"python38.dll _tkinter.pyd tcl86t.dll tk86t.dll")
            invalid.write_bytes(b"python312.dll _tkinter.pyd tcl86t.dll tk86t.dll")
            assert_bundled_runtime(valid)
            with self.assertRaisesRegex(ValueError, "python38.dll"):
                assert_bundled_runtime(invalid)

    def test_checksum_manifest_must_match_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp)
            artifact = dist / "app.exe"
            artifact.write_bytes(b"release")
            digest = hashlib.sha256(b"release").hexdigest()
            (dist / "SHA256SUMS.txt").write_text(f"{digest}  app.exe\n", encoding="ascii")
            verify_checksums(dist, ("app.exe",))
            artifact.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                verify_checksums(dist, ("app.exe",))

    def test_chinese_checksum_manifest_accepts_utf8_with_bom_and_rejects_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp)
            name = "邻桥快传-发送端.exe"
            (dist / name).write_bytes(b"release")
            digest = hashlib.sha256(b"release").hexdigest()
            entry = f"{digest}  {name}\n"
            manifest = dist / "SHA256SUMS.txt"
            manifest.write_text(entry, encoding="utf-8-sig")
            verify_checksums(dist, (name,))
            manifest.write_text(entry + entry, encoding="utf-8-sig")
            with self.assertRaisesRegex(ValueError, "more than once"):
                verify_checksums(dist, (name,))

    def test_release_definitions_use_the_application_version_and_chinese_filenames(self) -> None:
        root = Path(__file__).resolve().parents[1]
        build_script = (root / "scripts" / "build_windows.ps1").read_text(encoding="utf-8-sig")
        for role, suffix in (("receiver", "接收端"), ("sender", "发送端")):
            stem = f"邻桥快传-{suffix}"
            executable = stem + ".exe"
            installer = stem + "-安装包.exe"
            with self.subTest(role=role):
                self.assertIn(executable, PORTABLE_NAMES)
                self.assertEqual(INSTALLED_NAMES[installer], executable)
                self.assertIn(executable, build_script)
                self.assertIn(installer, build_script)
                spec = root / "packaging" / f"LanBridge-{role.title()}-x64.spec"
                calls = [node for node in ast.walk(ast.parse(spec.read_text(encoding="utf-8-sig")))
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "EXE"]
                kwargs = {item.arg: item.value for item in calls[0].keywords}
                self.assertEqual(ast.literal_eval(kwargs["name"]), stem)
                info = root / "packaging" / f"version_info_{role}.txt"
                tree = ast.parse(info.read_text(encoding="utf-8-sig"))
                strings = {}
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                        continue
                    if node.func.id == "FixedFileInfo":
                        versions = {item.arg: ast.literal_eval(item.value) for item in node.keywords}
                        self.assertEqual(versions["filevers"], tuple(map(int, APP_VERSION.split("."))))
                        self.assertEqual(versions["prodvers"], versions["filevers"])
                    elif node.func.id == "StringStruct":
                        key, value = map(ast.literal_eval, node.args)
                        strings[key] = value
                self.assertEqual(strings["OriginalFilename"], executable)
                self.assertEqual(strings["FileVersion"], APP_VERSION)
                self.assertEqual(strings["ProductVersion"], APP_VERSION)
                self.assertEqual(strings["ProductName"], "邻桥快传")
                source = (root / "packaging" / f"LanBridge-{role.title()}.iss").read_text(encoding="utf-8-sig")
                defines = dict(re.findall(r'^#define (\w+) "([^"]+)"$', source, re.MULTILINE))
                self.assertEqual(defines["MyAppVersion"], APP_VERSION)
                self.assertEqual(defines["MyAppExeName"], executable)
                self.assertEqual(defines["MyAppName"], stem)
                self.assertIn(f"OutputBaseFilename={stem}-安装包", source)


if __name__ == "__main__":
    unittest.main()
