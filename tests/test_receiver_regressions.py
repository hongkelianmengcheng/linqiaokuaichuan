from __future__ import annotations

import hashlib
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge import receiver_service
from lanbridge.protocol import ProtocolError, RemoteError, client_handshake, recv_frame, send_frame
from lanbridge.receiver_service import ReceiverServer


class MemoryActivity:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    warning = info
    error = info


class MemoryTransfers:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


class ReceiverRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.downloads = Path(self.temp.name) / "received"
        self.events: list[dict] = []
        self.activity = MemoryActivity()
        self.transfers = MemoryTransfers()
        discovery = patch.object(receiver_service, "DiscoveryResponder")
        discovery.start()
        self.addCleanup(discovery.stop)
        self.server = ReceiverServer(
            self.downloads,
            activity_logger=self.activity,
            transfer_logger=self.transfers,
            event_callback=self.events.append,
        )
        self.port = self.server.start(0)
        self.addCleanup(self.server.stop)

    def open_transfer(self, path="report.bin", size=4, session="reusedsession"):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        self.addCleanup(sock.close)
        ready = client_handshake(
            sock,
            action="transfer",
            session_id=session,
            sender_identity={"hostname": "regression-client", "mac": "test"},
            offer={"relative_path": path, "size": size},
        )
        self.assertEqual("ready", ready["type"])
        return sock, ready

    def send_finish(self, sock, payload=b"data", **overrides) -> None:
        sock.sendall(payload)
        send_frame(sock, {
            "type": "finish",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "bytes_sent": len(payload),
            "source_changed": False,
            **overrides,
        })

    def wait_workers(self) -> None:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.server._connection_lock:
                if not self.server._workers:
                    return
            time.sleep(0.01)
        self.fail("receiver workers did not finish")

    def assert_no_partials(self) -> None:
        self.assertEqual([], list(self.downloads.rglob("*.part")))

    def test_existing_predictable_partial_file_is_not_truncated(self) -> None:
        existing = self.downloads / ".report.bin.reusedsession.part"
        existing.write_bytes(b"unrelated existing file")
        sock, _ = self.open_transfer()
        self.send_finish(sock)
        self.assertEqual("complete", recv_frame(sock)["type"])
        self.wait_workers()
        self.assertEqual(b"unrelated existing file", existing.read_bytes())
        self.assertEqual(b"data", (self.downloads / "report.bin").read_bytes())
        self.assertEqual([existing], list(self.downloads.glob("*.part")))

    def test_file_created_after_ready_is_not_overwritten(self) -> None:
        sock, ready = self.open_transfer()
        destination = self.downloads / ready["saved_relative_path"]
        destination.write_bytes(b"created by another application")
        self.send_finish(sock)
        result = recv_frame(sock)
        self.assertEqual("error", result["type"])
        self.wait_workers()
        self.assertEqual(b"created by another application", destination.read_bytes())
        self.assert_no_partials()

    def test_logging_failure_does_not_turn_saved_file_into_failure(self) -> None:
        with patch.object(self.transfers, "write", side_effect=OSError("log disk full")), \
                patch.object(self.activity, "info", side_effect=OSError("log unavailable")):
            sock, _ = self.open_transfer()
            self.send_finish(sock)
            self.assertEqual("complete", recv_frame(sock)["type"])
            self.wait_workers()
        kinds = [event["kind"] for event in self.events]
        self.assertIn("transfer_complete", kinds)
        self.assertNotIn("transfer_error", kinds)
        self.assertEqual(b"data", (self.downloads / "report.bin").read_bytes())

    def test_logging_failure_does_not_hide_rejected_transfer_event(self) -> None:
        with patch.object(self.transfers, "write", side_effect=OSError("log disk full")):
            sock, _ = self.open_transfer()
            self.send_finish(sock, sha256="0" * 64)
            self.assertEqual("CHECKSUM_MISMATCH", recv_frame(sock)["code"])
            self.wait_workers()
        self.assertIn("transfer_error", [event["kind"] for event in self.events])
        self.assertFalse((self.downloads / "report.bin").exists())
        self.assert_no_partials()

    def test_logging_and_activity_failures_do_not_hide_rejected_transfer_event(self) -> None:
        with patch.object(self.transfers, "write", side_effect=OSError("log disk full")), \
                patch.object(self.activity, "error", side_effect=OSError("logger unavailable")):
            sock, _ = self.open_transfer()
            self.send_finish(sock, sha256="0" * 64)
            self.assertEqual("CHECKSUM_MISMATCH", recv_frame(sock)["code"])
            self.wait_workers()
        self.assertIn("transfer_error", [event["kind"] for event in self.events])
        self.assertFalse((self.downloads / "report.bin").exists())
        self.assert_no_partials()

    def test_missing_acknowledgement_does_not_mark_saved_file_failed(self) -> None:
        original_send = receiver_service.send_frame

        def disconnect_on_complete(sock, message):
            if message.get("type") == "complete":
                raise ConnectionResetError("sender disconnected before acknowledgement")
            return original_send(sock, message)

        with patch.object(receiver_service, "send_frame", side_effect=disconnect_on_complete):
            sock, _ = self.open_transfer()
            self.send_finish(sock)
            with self.assertRaises(ProtocolError):
                recv_frame(sock)
            self.wait_workers()
        self.assertEqual(["success"], [record["status"] for record in self.transfers.records])
        self.assertIn("transfer_complete", [event["kind"] for event in self.events])
        self.assertEqual(b"data", (self.downloads / "report.bin").read_bytes())

    def test_invalid_offer_sizes_are_rejected_without_coercion(self) -> None:
        for size in (True, 1.5, "4", None, -1, 2**64):
            with self.subTest(size=size), self.assertRaises(RemoteError) as raised:
                self.open_transfer(size=size)
            self.assertEqual("INVALID_SIZE", raised.exception.code)
        self.wait_workers()
        self.assertEqual([], list(self.downloads.iterdir()))

    def test_invalid_paths_are_rejected_without_stringification_or_truncation(self) -> None:
        for path in ({"name": "report.bin"}, ["report.bin"], "a" * 2049, "."):
            with self.subTest(path=path), self.assertRaises(RemoteError) as raised:
                self.open_transfer(path=path)
            self.assertEqual("INVALID_PATH", raised.exception.code)
        self.wait_workers()
        self.assertEqual([], list(self.downloads.iterdir()))

    def test_invalid_session_ids_are_rejected_without_stringification(self) -> None:
        for session in (True, 1, None, "", "x" * 129):
            with self.subTest(session=repr(session)), self.assertRaises(RemoteError) as raised:
                self.open_transfer(session=session)
            self.assertEqual("INVALID_SESSION", raised.exception.code)
        self.wait_workers()
        self.assertEqual([], list(self.downloads.iterdir()))

    def test_invalid_finish_byte_counts_are_rejected_without_coercion(self) -> None:
        for byte_count in (True, 1.5, "1", None):
            with self.subTest(byte_count=byte_count):
                sock, _ = self.open_transfer(size=1)
                self.send_finish(sock, payload=b"x", bytes_sent=byte_count)
                self.assertEqual("SIZE_MISMATCH", recv_frame(sock)["code"])
                self.wait_workers()
                self.assertFalse((self.downloads / "report.bin").exists())
                self.assert_no_partials()

    def test_concurrent_same_name_transfers_keep_both_payloads(self) -> None:
        first, first_ready = self.open_transfer(session="first")
        second, second_ready = self.open_transfer(session="second")
        self.assertNotEqual(first_ready["saved_relative_path"], second_ready["saved_relative_path"])
        self.send_finish(second, payload=b"two!")
        self.assertEqual("complete", recv_frame(second)["type"])
        self.send_finish(first, payload=b"one!")
        self.assertEqual("complete", recv_frame(first)["type"])
        self.wait_workers()
        self.assertEqual(b"one!", (self.downloads / first_ready["saved_relative_path"]).read_bytes())
        self.assertEqual(b"two!", (self.downloads / second_ready["saved_relative_path"]).read_bytes())
        self.assert_no_partials()

    def test_stop_discards_incomplete_transfer_and_restart_accepts_new_one(self) -> None:
        sock, _ = self.open_transfer(size=100)
        sock.sendall(b"unfinished")
        previous_stop = self.server._stop_event
        self.server.stop()
        self.assertFalse((self.downloads / "report.bin").exists())
        self.assert_no_partials()
        self.port = self.server.start(0)
        self.assertTrue(previous_stop.is_set())
        sock, _ = self.open_transfer()
        self.send_finish(sock)
        self.assertEqual("complete", recv_frame(sock)["type"])
        self.wait_workers()

    def test_stop_before_commit_rejects_even_empty_file(self) -> None:
        original_recv = receiver_service.recv_frame

        def stop_on_finish(sock):
            message = original_recv(sock)
            if message.get("type") == "finish":
                self.server._stop_event.set()
            return message

        with patch.object(receiver_service, "recv_frame", side_effect=stop_on_finish):
            sock, _ = self.open_transfer(size=0)
            self.send_finish(sock, payload=b"")
            self.assertEqual("SERVER_STOPPED", recv_frame(sock)["code"])
            self.wait_workers()
        self.assertFalse((self.downloads / "report.bin").exists())
        self.assert_no_partials()

    def test_short_disk_writes_do_not_silently_truncate_file(self) -> None:
        original_tempfile = receiver_service.tempfile.NamedTemporaryFile

        def short_write_file(*args, **kwargs):
            handle = original_tempfile(*args, **kwargs)
            original_write = handle.write
            handle.write = lambda data: original_write(data[:2])
            return handle

        with patch.object(receiver_service.tempfile, "NamedTemporaryFile", side_effect=short_write_file):
            sock, _ = self.open_transfer()
            self.send_finish(sock)
            self.assertEqual("complete", recv_frame(sock)["type"])
            self.wait_workers()
        self.assertEqual(b"data", (self.downloads / "report.bin").read_bytes())

    def test_socket_closed_during_shutdown_does_not_escape_handler(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.close()
        self.server._handle_connection(sock, ("127.0.0.1", 1), threading.Event())

    def test_invalid_start_port_is_rejected_before_opening_socket(self) -> None:
        self.server.stop()
        with patch.object(receiver_service.socket, "socket") as create_socket:
            for port in (-1, 65536, True, 1.5, "not a port"):
                with self.subTest(port=port), self.assertRaises(ValueError):
                    self.server.start(port)
            create_socket.assert_not_called()

    def test_max_connections_requires_a_positive_integer(self) -> None:
        for value in (True, 0, -1, 1.5, "4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ReceiverServer(self.downloads, max_connections=value)

    def test_successful_auth_checks_do_not_leak_rate_limit_entries(self) -> None:
        for index in range(2000):
            self.assertFalse(self.server._auth_limited(f"10.0.{index // 256}.{index % 256}"))
        self.assertEqual({}, self.server._auth_failures)

    def test_concurrent_starts_create_only_one_listener(self) -> None:
        candidate = ReceiverServer(
            self.downloads / "concurrent-start",
            activity_logger=self.activity,
            transfer_logger=self.transfers,
        )
        original_socket = socket.socket
        entered = threading.Event()
        release = threading.Event()
        call_lock = threading.Lock()
        created: list[socket.socket] = []
        call_count = 0
        results: list[int] = []
        errors: list[Exception] = []

        def delayed_socket(*args, **kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
                current_call = call_count
            if current_call == 1:
                entered.set()
                release.wait(2)
            result = original_socket(*args, **kwargs)
            created.append(result)
            return result

        def start_server() -> None:
            try:
                results.append(candidate.start(0))
            except Exception as exc:
                errors.append(exc)

        first = threading.Thread(target=start_server, daemon=True)
        second = threading.Thread(target=start_server, daemon=True)
        try:
            with patch.object(receiver_service.socket, "socket", side_effect=delayed_socket):
                first.start()
                self.assertTrue(entered.wait(1))
                second.start()
                time.sleep(0.1)
                with call_lock:
                    self.assertEqual(1, call_count)
                release.set()
                first.join(3)
                second.join(3)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual([], errors)
            self.assertEqual(2, len(results))
            self.assertEqual(results[0], results[1])
        finally:
            release.set()
            candidate.stop()
            for opened in created:
                try:
                    opened.close()
                except OSError:
                    pass
            first.join(3)
            second.join(3)

    def test_accept_thread_start_failure_releases_listener(self) -> None:
        candidate = ReceiverServer(
            self.downloads / "thread-start-failure",
            activity_logger=self.activity,
            transfer_logger=self.transfers,
        )
        with patch.object(receiver_service.threading.Thread, "start", side_effect=RuntimeError("no threads")):
            with self.assertRaisesRegex(RuntimeError, "no threads"):
                candidate.start(0)
        self.assertIsNone(candidate._listener)
        self.assertIsNone(candidate._accept_thread)
        self.assertEqual(0, candidate.port)

    def test_restart_cleans_listener_left_by_failed_accept_loop(self) -> None:
        candidate = ReceiverServer(
            self.downloads / "stale-listener",
            activity_logger=self.activity,
            transfer_logger=self.transfers,
        )
        try:
            candidate.start(0)
            stale_listener = candidate._listener
            stale_thread = candidate._accept_thread
            self.assertIsNotNone(stale_listener)
            self.assertIsNotNone(stale_thread)
            stale_listener.close()
            stale_thread.join(2)
            self.assertFalse(candidate.is_running)

            restarted_port = candidate.start(0)
            self.assertGreater(restarted_port, 0)
            self.assertTrue(candidate.is_running)
            self.assertIsNot(stale_listener, candidate._listener)
        finally:
            candidate.stop()


if __name__ == "__main__":
    unittest.main()
