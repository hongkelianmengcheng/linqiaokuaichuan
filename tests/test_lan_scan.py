from __future__ import annotations

import os
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.discovery import DiscoveryResponder, _scan_timeout, discover_lan_hosts
from lanbridge.lan_scan import (
    MAX_SCAN_HOSTS,
    _parse_netbios_reply,
    _probe_host,
    _resolve_hostname,
    _scan_candidates,
)


class LanScanTests(unittest.TestCase):
    def test_candidates_follow_actual_netmask_and_skip_local_network_broadcast(self) -> None:
        found = _scan_candidates([("192.168.5.6", "255.255.255.252")], ["192.168.5.4", "192.168.5.7", "192.168.8.1"])
        self.assertEqual(["192.168.5.5"], found)

    def test_point_to_point_and_unknown_netmasks(self) -> None:
        self.assertEqual(["192.168.5.7"], _scan_candidates([("192.168.5.6", "255.255.255.254")], []))
        self.assertEqual([], _scan_candidates([("192.168.5.6", "255.255.255.255")], []))

    def test_large_network_is_bounded_and_cached_neighbors_are_prioritized(self) -> None:
        found = _scan_candidates(
            [("10.23.45.67", "255.0.0.0"), ("172.16.3.4", "255.255.0.0"), ("192.168.8.9", "255.255.255.0")],
            ["10.99.1.3", "203.0.113.1", "10.99.1.3"],
        )
        self.assertLessEqual(len(found), MAX_SCAN_HOSTS)
        self.assertEqual("10.99.1.3", found[0])
        self.assertNotIn("203.0.113.1", found)
        self.assertEqual(len(found), len(set(found)))
        for prefix in ("10.23.45.", "172.16.3.", "192.168.8."):
            self.assertTrue(any(address.startswith(prefix) for address in found))

    def test_23_bit_subnet_is_scanned_entirely(self) -> None:
        found = _scan_candidates([("192.168.4.2", "255.255.254.0")], [])
        self.assertEqual(509, len(found))
        self.assertIn("192.168.5.254", found)

    def test_invalid_interfaces_do_not_turn_into_default_route_scans(self) -> None:
        self.assertEqual([], _scan_candidates([("127.0.0.1", "255.0.0.0"), ("0.0.0.0", "0.0.0.0"), ("bad", "bad"), ("192.168.1.3", "0.0.0.0")], []))

    def test_timeout_rejects_non_finite_input_and_is_bounded(self) -> None:
        for value in (float("inf"), float("nan"), "bad"):
            with self.assertRaises(ValueError):
                _scan_timeout(value)
        self.assertEqual(15.0, _scan_timeout(99999))

    def test_cancelled_scan_never_touches_network(self) -> None:
        cancellation = threading.Event()
        cancellation.set()
        with patch("lanbridge.lan_scan.get_local_ipv4_interfaces") as interfaces:
            self.assertEqual([], discover_lan_hosts(cancel_event=cancellation))
        interfaces.assert_not_called()

    def test_mid_scan_cancellation_returns_promptly(self) -> None:
        cancellation = threading.Event()
        timer = threading.Timer(0.12, cancellation.set)
        with patch("lanbridge.lan_scan.get_local_ipv4_interfaces", return_value=[]), patch("lanbridge.lan_scan._windows_neighbor_addresses", return_value=[]), patch("lanbridge.lan_scan.discover_receivers", return_value=[]):
            started = time.monotonic()
            timer.start()
            discover_lan_hosts(timeout=4, cancel_event=cancellation)
            elapsed = time.monotonic() - started
        timer.join()
        self.assertLess(elapsed, 0.6)

    def test_open_tcp_service_is_not_misidentified_as_receiver(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            self.assertEqual("tcp", _probe_host("127.0.0.1", listener.getsockname()[1], 0.3))

    def test_scan_merges_generic_host_into_receiver_with_auto_selected_port(self) -> None:
        def advertisements(**kwargs):
            kwargs["on_receiver"]({"ip": "192.168.5.5", "port": 53329, "hostname": "RECEIVER", "requires_code": True})
            return []

        with patch("lanbridge.lan_scan.get_local_ipv4_interfaces", return_value=[("192.168.5.6", "255.255.255.252")]), patch("lanbridge.lan_scan._windows_neighbor_addresses", return_value=[]), patch("lanbridge.lan_scan.discover_receivers", side_effect=advertisements), patch("lanbridge.lan_scan._probe_host", return_value="tcp"), patch("lanbridge.lan_scan._resolve_hostname", return_value="GENERIC"):
            found = discover_lan_hosts(timeout=0.2)
        receiver = found[0]
        self.assertEqual("192.168.5.5", receiver["ip"])
        self.assertTrue(receiver["is_receiver"])
        self.assertEqual("RECEIVER", receiver["hostname"])
        self.assertEqual(53329, receiver["port"])
        self.assertTrue(receiver["requires_code"])
        self.assertEqual(2, len(found))

    def test_generic_online_host_has_safe_unknown_name_and_receiver_flag_false(self) -> None:
        with patch("lanbridge.lan_scan.get_local_ipv4_interfaces", return_value=[("192.168.5.6", "255.255.255.252")]), patch("lanbridge.lan_scan._windows_neighbor_addresses", return_value=[]), patch("lanbridge.lan_scan.discover_receivers", return_value=[]), patch("lanbridge.lan_scan._probe_host", return_value="ping"), patch("lanbridge.lan_scan._resolve_hostname", return_value="未知主机"):
            found = discover_lan_hosts(timeout=0.2)
        remote = next(item for item in found if item["ip"] == "192.168.5.5")
        self.assertFalse(remote["is_receiver"])
        self.assertTrue(remote["online"])
        self.assertEqual("未知主机", remote["hostname"])

    def test_dead_hosts_are_not_shown_even_when_present_in_arp_cache(self) -> None:
        with patch("lanbridge.lan_scan.get_local_ipv4_interfaces", return_value=[("192.168.5.6", "255.255.255.252")]), patch("lanbridge.lan_scan._windows_neighbor_addresses", return_value=["192.168.5.5"]), patch("lanbridge.lan_scan.discover_receivers", return_value=[]), patch("lanbridge.lan_scan._probe_host", return_value=""):
            found = discover_lan_hosts(timeout=0.1)
        self.assertEqual(["192.168.5.6"], [item["ip"] for item in found])

    def test_slow_dns_does_not_hold_scan_or_cancel_open(self) -> None:
        release = threading.Event()
        entered = threading.Event()

        def slow_name(_address):
            entered.set()
            release.wait(2)
            return "LATE-NAME"

        try:
            with patch("lanbridge.lan_scan.get_local_ipv4_interfaces", return_value=[("192.168.5.6", "255.255.255.252")]), patch("lanbridge.lan_scan._windows_neighbor_addresses", return_value=[]), patch("lanbridge.lan_scan.discover_receivers", return_value=[]), patch("lanbridge.lan_scan._probe_host", return_value="ping"), patch("lanbridge.lan_scan._resolve_hostname", side_effect=slow_name):
                started = time.monotonic()
                found = discover_lan_hosts(timeout=0.2)
                self.assertTrue(entered.is_set())
                self.assertLess(time.monotonic() - started, 0.7)
                remote = next(item for item in found if item["ip"] == "192.168.5.5")
                self.assertEqual("未知主机", remote["hostname"])
        finally:
            release.set()

    def test_real_udp_receiver_is_returned_before_short_scan_deadline(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as temporary:
            temporary.bind(("127.0.0.1", 0))
            discovery_port = temporary.getsockname()[1]
        responder = DiscoveryResponder(lambda: {"hostname": "REAL-RECEIVER", "port": 55555}, discovery_port=discovery_port)
        responder.start()
        try:
            with patch("lanbridge.lan_scan.get_local_ipv4_interfaces", return_value=[]), patch("lanbridge.lan_scan._windows_neighbor_addresses", return_value=[]):
                found = discover_lan_hosts(timeout=0.25, discovery_port=discovery_port)
        finally:
            responder.stop()
        self.assertTrue(any(item["hostname"] == "REAL-RECEIVER" and item["is_receiver"] and item["port"] == 55555 for item in found), found)

    def test_ip_literal_from_reverse_dns_is_reported_as_unknown(self) -> None:
        with patch("lanbridge.lan_scan._netbios_hostname", return_value=""), patch("lanbridge.lan_scan.socket.gethostbyaddr", return_value=("192.168.5.5", [], [])):
            self.assertEqual("未知主机", _resolve_hostname("192.168.5.5"))

    def test_netbios_selects_unique_workstation_and_rejects_malformed_packets(self) -> None:
        transaction = 1234
        names = b"\x02" + b"WORKGROUP      " + b"\x00\x80\x00" + b"MY-COMPUTER    " + b"\x00\x04\x00"
        data = struct.pack("!6H", transaction, 0x8400, 0, 1, 0, 0) + b"\x00" + struct.pack("!HHIH", 0x21, 1, 0, len(names)) + names
        self.assertEqual("MY-COMPUTER", _parse_netbios_reply(data, transaction))
        self.assertEqual("", _parse_netbios_reply(data, transaction + 1))
        for size in range(len(data)):
            self.assertEqual("", _parse_netbios_reply(data[:size], transaction))
        for _sample in range(200):
            self.assertEqual("", _parse_netbios_reply(os.urandom(120), transaction))


if __name__ == "__main__":
    unittest.main()
