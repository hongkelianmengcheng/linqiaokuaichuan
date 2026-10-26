from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.connection_memory import MEMORY_SECONDS, initial_endpoint, recent_endpoints, remember_endpoint


class ConnectionMemoryTests(unittest.TestCase):
    def test_first_launch_uses_local_ip(self):
        self.assertEqual(("192.168.5.8", 53317, False), initial_endpoint({}, ["192.168.5.8"]))

    def test_recent_ip_survives_restart_and_local_ip_change(self):
        settings = {}
        remember_endpoint(settings, "192.168.5.20", 54321)
        self.assertEqual(("192.168.5.20", 54321, True), initial_endpoint(settings, ["10.1.2.3"]))
        self.assertNotIn("access_code", settings)

    def test_expired_memory_falls_back_to_current_local_ip(self):
        settings = {}
        with patch("lanbridge.connection_memory.time.time", return_value=time.time() - MEMORY_SECONDS - 1):
            remember_endpoint(settings, "192.168.5.20", 54321)
        self.assertEqual(("10.1.2.3", 53317, False), initial_endpoint(settings, ["10.1.2.3"]))

    def test_history_is_bounded_deduplicated_and_ordered(self):
        settings = {}
        for index in range(20):
            remember_endpoint(settings, f"192.168.1.{index + 1}", 53317)
        remember_endpoint(settings, "192.168.1.18", 53317)
        history = recent_endpoints(settings)
        self.assertEqual(8, len(history))
        self.assertEqual("192.168.1.18", history[0]["host"])
        self.assertEqual(8, len({item["host"] for item in history}))

    def test_malformed_and_future_history_is_ignored(self):
        valid = {"host": "192.168.1.20", "port": 53317, "used_at": 100.0}
        cases = [None, True, {}, {**valid, "used_at": float("nan")}, {**valid, "used_at": 10**1000}, {**valid, "used_at": 200.0}, {**valid, "port": True}]
        self.assertEqual([valid], recent_endpoints({"recent_endpoints": cases + [valid]}, now=100.0))

    def test_old_release_destination_is_migrated(self):
        self.assertEqual(("192.168.3.4", 60000, True), initial_endpoint({"receiver_host": "192.168.3.4", "receiver_port": 60000}, ["10.0.0.2"]))


if __name__ == "__main__":
    unittest.main()
