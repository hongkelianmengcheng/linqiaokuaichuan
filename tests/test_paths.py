from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.utils import (
    collision_candidate,
    normalize_relative_path,
    safe_destination,
    sanitize_filename_component,
    validate_port,
)


class PathSafetyTests(unittest.TestCase):
    def test_sanitizes_windows_invalid_characters_and_reserved_names(self) -> None:
        self.assertEqual("报告_最终版_.txt", sanitize_filename_component('报告:最终版?.txt'))
        self.assertEqual("_CON.txt", sanitize_filename_component("CON.txt"))
        self.assertEqual("_lpt9", sanitize_filename_component("lpt9"))
        self.assertEqual("未命名", sanitize_filename_component("...   "))
        self.assertEqual("_COM¹.txt", sanitize_filename_component("COM¹.txt"))
        self.assertEqual("_CON .txt", sanitize_filename_component("CON .txt"))
        self.assertEqual("_CONIN$", sanitize_filename_component("CONIN$"))
        self.assertEqual("_CONOUT$.txt", sanitize_filename_component("CONOUT$.txt"))

    def test_sanitized_component_never_exceeds_documented_limit(self) -> None:
        ordinary = sanitize_filename_component("a" * 300 + ".txt")
        long_extension = sanitize_filename_component("a." + "x" * 300)
        self.assertLessEqual(len(ordinary), 180)
        self.assertLessEqual(len(long_extension), 180)

    def test_sanitized_component_limits_windows_utf16_code_units(self) -> None:
        emoji_name = sanitize_filename_component("\U0001f4c4" * 200 + ".txt")
        self.assertLessEqual(len(emoji_name.encode("utf-16-le")) // 2, 180)
        self.assertTrue(emoji_name.endswith(".txt"))

    def test_normalizes_unicode_nested_relative_path(self) -> None:
        normalized = normalize_relative_path(r"课程资料\第一章\报告:终稿?.docx")
        self.assertEqual(Path("课程资料") / "第一章" / "报告_终稿_.docx", normalized)

    def test_rejects_absolute_and_traversal_paths(self) -> None:
        invalid = (
            "",
            "   ",
            ".",
            "./",
            "./file.txt",
            "folder/./file.txt",
            "folder//file.txt",
            "folder/",
            "../secret.txt",
            "safe/../../secret.txt",
            "/absolute/file.txt",
            r"C:\Windows\system.ini",
            r"\\server\share\file.txt",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_relative_path(value)

    def test_safe_destination_stays_inside_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "downloads"
            root.mkdir()
            result = safe_destination(root, "子目录/文件.txt")
            self.assertEqual(root.resolve() / "子目录" / "文件.txt", result)
            with self.assertRaises(ValueError):
                safe_destination(root, "../outside.txt")

    def test_existing_symlink_cannot_escape_download_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "downloads"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            link = root / "link"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with self.assertRaises(ValueError):
                safe_destination(root, "link/escaped.txt")

    def test_collision_candidate_preserves_extension(self) -> None:
        original = Path("目录") / "archive.tar.gz"
        self.assertEqual(original, collision_candidate(original, 0))
        self.assertEqual(Path("目录") / "archive.tar (2).gz", collision_candidate(original, 2))

    def test_symlink_to_root_cannot_become_sibling_collision_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "downloads"
            root.mkdir()
            try:
                (root / "self").symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            with self.assertRaises(ValueError):
                safe_destination(root, "self")

    def test_port_requires_integer_without_truncation(self) -> None:
        self.assertEqual(53317, validate_port("53317"))
        self.assertEqual(0, validate_port(0, allow_zero=True))
        for value in (True, 53317.9, float("inf"), None, "1.5", 0, 65536):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_port(value)


if __name__ == "__main__":
    unittest.main()
