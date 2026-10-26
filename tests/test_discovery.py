from __future__ import annotations

import socket
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.constants import DISCOVERY_MAGIC, DISCOVERY_QUERY
from lanbridge.discovery import DiscoveryResponder, _decode, _encode, discover_receivers


def unused_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DiscoveryTests(unittest.TestCase):
    def test_discovery_datagram_round_trip_and_invalid_inputs(self) -> None:
        payload = {"type": DISCOVERY_QUERY, "hostname": "测试主机"}
        self.assertEqual(payload, _decode(_encode(payload)))
        self.assertIsNone(_decode(b"wrong-prefix"))
        self.assertIsNone(_decode(DISCOVERY_MAGIC + b"{"))
        self.assertIsNone(_decode(DISCOVERY_MAGIC + b"[]"))
        self.assertIsNone(_decode(DISCOVERY_MAGIC + b"x" * 8192))
        self.assertIsNone(_decode(DISCOVERY_MAGIC + b"[" * 2000 + b"]" * 2000))
        self.assertIsNone(_decode(DISCOVERY_MAGIC + b'{"number":' + b"9" * 5000 + b"}"))

    def test_mixed_port_types_do_not_break_device_sorting(self) -> None:
        responses = [
            {"type": "receiver", "protocol_version": 1, "hostname": "same", "port": port}
            for port in (53317, "53318", True, 12.5, float("inf"), None)
        ]

        class ReplySocket:
            closed = False

            def setsockopt(self, *args):
                pass

            def settimeout(self, *args):
                pass

            def bind(self, *args):
                pass

            def sendto(self, *args):
                pass

            def recvfrom(self, *args):
                if responses:
                    return _encode(responses.pop(0)), ("127.0.0.1", 53318)
                raise socket.timeout()

            def close(self):
                self.closed = True

        sock = ReplySocket()
        with patch("lanbridge.discovery.socket.socket", return_value=sock):
            found = discover_receivers(timeout=0.1)
        self.assertEqual([53317, 53318], [item["port"] for item in found])
        self.assertTrue(sock.closed)

    def test_loopback_discovery_finds_responder(self) -> None:
        port = unused_udp_port()
        responder = DiscoveryResponder(
            lambda: {
                "hostname": "unit-test-receiver",
                "mac": "00-11-22-33-44-55",
                "port": 54321,
                "requires_code": True,
            },
            discovery_port=port,
        )
        responder.start()
        try:
            found = discover_receivers(timeout=0.8, discovery_port=port)
        finally:
            responder.stop()

        matching = [item for item in found if item.get("hostname") == "unit-test-receiver"]
        self.assertTrue(matching, found)
        self.assertEqual(54321, matching[0]["port"])
        self.assertEqual("127.0.0.1", matching[0]["ip"])
        self.assertTrue(matching[0]["requires_code"])

    def test_network_fields_are_sanitized_and_boolean_protocol_version_is_rejected(self) -> None:
        responses = [
            {"type": "receiver", "protocol_version": True, "hostname": "INVALID", "port": 53317},
            {"type": "receiver", "protocol_version": 1, "hostname": {"unexpected": "object"}, "port": 53318, "mac": ["bad"], "requires_code": "false"},
            {"type": "receiver", "protocol_version": 1, "hostname": "safe\x00\nname", "port": 53319},
        ]

        class ReplySocket:
            def setsockopt(self, *args):
                pass

            def settimeout(self, *args):
                pass

            def bind(self, *args):
                pass

            def sendto(self, *args):
                pass

            def recvfrom(self, *args):
                if responses:
                    return _encode(responses.pop(0)), ("192.168.1.2", 53318)
                raise socket.timeout()

            def close(self):
                pass

        with patch("lanbridge.discovery.socket.socket", return_value=ReplySocket()), patch("lanbridge.discovery.get_local_ipv4_interfaces", return_value=[]):
            found = discover_receivers(timeout=0.1)
        self.assertEqual(2, len(found))
        self.assertEqual({53318, 53319}, {item["port"] for item in found})
        self.assertEqual({"未知主机", "safename"}, {item["hostname"] for item in found})
        self.assertTrue(all(item["requires_code"] is False for item in found))
        self.assertTrue(all(item["mac"] == "" for item in found))

    def test_bad_info_provider_does_not_kill_responder(self) -> None:
        port = unused_udp_port()

        def broken_provider():
            raise ValueError("temporary identity failure")

        responder = DiscoveryResponder(broken_provider, discovery_port=port)
        responder.start()
        try:
            self.assertEqual([], discover_receivers(timeout=0.2, discovery_port=port))
            responder.info_provider = lambda: {"hostname": "recovered", "port": 54321}
            found = discover_receivers(timeout=0.5, discovery_port=port)
        finally:
            responder.stop()

        self.assertTrue(any(item.get("hostname") == "recovered" for item in found), found)


if __name__ == "__main__":
    unittest.main()
