from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.app_paths import get_app_paths, load_settings, save_settings


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"LANBRIDGE_DATA_DIR": temp.name})
        env.start()
        self.addCleanup(env.stop)

    def test_concurrent_saves_leave_one_complete_document(self) -> None:
        barrier = threading.Barrier(4)
        state = threading.local()
        real_replace = os.replace

        def synchronized_replace(source, destination):
            if not getattr(state, "started", False):
                state.started = True
                barrier.wait(timeout=3)
            real_replace(source, destination)

        documents = [{"value": str(index) * 8192} for index in range(4)]
        with patch("lanbridge.app_paths.os.replace", side_effect=synchronized_replace):
            with ThreadPoolExecutor(max_workers=4) as executor:
                paths = list(executor.map(lambda doc: save_settings("sender", doc), documents))
        self.assertIn(json.loads(paths[0].read_text(encoding="utf-8")), documents)
        self.assertEqual([], list(paths[0].parent.glob("*.tmp")))

    def test_failed_replace_preserves_settings_and_removes_temporary_file(self) -> None:
        path = save_settings("sender", {"value": "old"})
        with patch("lanbridge.app_paths.os.replace", side_effect=OSError("read only")):
            with self.assertRaises(OSError):
                save_settings("sender", {"value": "new"})
        self.assertEqual({"value": "old"}, load_settings("sender", {}))
        self.assertEqual([], list(path.parent.glob("*.tmp")))

    def test_deeply_nested_corrupt_settings_use_defaults(self) -> None:
        path = get_app_paths().settings / "sender.json"
        path.write_text("[" * 2000 + "]" * 2000, encoding="utf-8")
        self.assertEqual({"port": 53317}, load_settings("sender", {"port": 53317}))

    def test_wrong_setting_types_and_unknown_keys_are_ignored(self) -> None:
        path = get_app_paths().settings / "receiver.json"
        path.write_text(
            json.dumps({"listen_port": True, "auto_start": "false", "download_directory": [], "unknown": 1}),
            encoding="utf-8",
        )
        defaults = {"listen_port": 53317, "auto_start": True, "download_directory": "C:/Downloads"}
        self.assertEqual(defaults, load_settings("receiver", defaults))

    def test_oversized_settings_file_is_not_loaded(self) -> None:
        path = get_app_paths().settings / "sender.json"
        path.write_text(json.dumps({"host": "x" * (1024 * 1024)}), encoding="utf-8")
        self.assertEqual({"host": "127.0.0.1"}, load_settings("sender", {"host": "127.0.0.1"}))


if __name__ == "__main__":
    unittest.main()
