from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.models import SendJob
from lanbridge.protocol import ProtocolError
from lanbridge.sender_service import SenderService, TransferCancelled


class MemoryActivity:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def error(self, message: str) -> None:
        self.messages.append(message)


class MemoryTransfers:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def write(self, record: dict) -> None:
        self.records.append(record)


class RecordingSocket:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.on_send = None
        self.shutdown_requested = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def getsockname(self):
        return "127.0.0.1", 40000

    def getpeername(self):
        return "127.0.0.1", 53317

    def settimeout(self, timeout):
        pass

    def sendall(self, data: bytes) -> None:
        self.chunks.append(data)
        if self.on_send:
            self.on_send()

    def shutdown(self, how) -> None:
        self.shutdown_requested.set()


class SenderRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / "source.bin"
        self.payload = b"abcdefgh"
        self.source.write_bytes(self.payload)
        self.job = SendJob(self.source, "source.bin", len(self.payload))
        self.activity = MemoryActivity()
        self.transfers = MemoryTransfers()
        self.sender = SenderService(activity_logger=self.activity, transfer_logger=self.transfers)
        self.sock = RecordingSocket()
        self.complete = {
            "type": "complete",
            "transfer_id": self.job.transfer_id,
            "sha256": hashlib.sha256(self.payload).hexdigest(),
            "bytes_received": len(self.payload),
        }
        # TestCase.enterContext was added after Python 3.8; manage the stack
        # explicitly so the regression suite runs with the Win7 build Python.
        stack = ExitStack()
        self.addCleanup(stack.close)
        prefix = "lanbridge.sender_service."
        self.connect = stack.enter_context(patch(prefix + "socket.create_connection", return_value=self.sock))
        stack.enter_context(patch(prefix + "configure_tcp_socket"))
        stack.enter_context(patch.object(self.sender, "_identity", return_value={"hostname": "test", "mac": "test"}))
        self.handshake = stack.enter_context(patch(prefix + "client_handshake", return_value={"type": "ready"}))
        self.finish = stack.enter_context(patch(prefix + "send_frame"))
        self.receive = stack.enter_context(patch(prefix + "recv_frame", return_value=self.complete))
        stack.enter_context(patch(prefix + "CHUNK_SIZE", 4))

    def send(self, **kwargs):
        return self.sender.send_file(self.job, "127.0.0.1", 53317, **kwargs)

    def test_source_growth_never_overruns_offered_size(self) -> None:
        def grow_once():
            self.sock.on_send = None
            with self.source.open("ab") as handle:
                handle.write(b"appended while sending")

        self.sock.on_send = grow_once
        with self.assertRaises(ProtocolError) as raised:
            self.send()

        self.assertEqual("SOURCE_CHANGED", raised.exception.code)
        self.assertEqual(self.payload, b"".join(self.sock.chunks))
        self.assertTrue(self.finish.call_args.args[1]["source_changed"])
        self.receive.assert_not_called()
        self.assertEqual("failed", self.transfers.records[0]["status"])

    def test_source_truncation_does_not_send_a_frame_as_file_data(self) -> None:
        def truncate_once():
            self.sock.on_send = None
            with self.source.open("r+b") as handle:
                handle.truncate(4)

        self.sock.on_send = truncate_once
        with self.assertRaises(ProtocolError) as raised:
            self.send()

        self.assertEqual("SOURCE_CHANGED", raised.exception.code)
        self.assertEqual(self.payload[:4], b"".join(self.sock.chunks))
        self.finish.assert_not_called()
        self.receive.assert_not_called()

    def test_replaced_source_with_identical_size_and_timestamp_is_rejected(self) -> None:
        original = self.source.stat()

        def replace_during_handshake(*args, **kwargs):
            replacement = self.root / "replacement.bin"
            replacement.write_bytes(b"12345678")
            os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
            os.replace(replacement, self.source)
            return {"type": "ready"}

        self.handshake.side_effect = replace_during_handshake
        with self.assertRaises(ProtocolError) as raised:
            self.send()

        self.assertEqual("SOURCE_CHANGED", raised.exception.code)
        self.assertEqual([], self.sock.chunks)

    def test_missing_source_is_logged(self) -> None:
        self.source.unlink()
        with self.assertRaises(FileNotFoundError):
            self.send()

        self.connect.assert_not_called()
        self.assertEqual(1, len(self.transfers.records))
        self.assertEqual("FileNotFoundError", self.transfers.records[0]["error_code"])

    def test_already_cancelled_missing_source_is_cancelled(self) -> None:
        self.source.unlink()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TransferCancelled):
            self.send(cancel_event=cancel)

        self.connect.assert_not_called()
        self.assertEqual("cancelled", self.transfers.records[0]["status"])

    def test_callback_failure_does_not_change_transfer_result(self) -> None:
        def broken_callback(payload):
            raise RuntimeError("view is unavailable")

        result = self.send(progress_callback=broken_callback)

        self.assertEqual("complete", result["type"])
        self.assertEqual(["success"], [record["status"] for record in self.transfers.records])
        self.assertTrue(any("view is unavailable" in message for message in self.activity.messages))

    def test_current_size_is_reported_before_connection(self) -> None:
        self.job.size = 100
        progress = []
        self.send(progress_callback=progress.append)
        self.assertEqual(0, progress[0]["bytes"])
        self.assertEqual(len(self.payload), progress[0]["total"])
        self.assertEqual(len(self.payload), self.job.size)

    def test_invalid_ports_are_rejected_before_network_access(self) -> None:
        for port in (True, 1.5, 0, -1, 65536, "1.5"):
            with self.subTest(operation="send", port=port), self.assertRaises(ValueError):
                self.sender.send_file(self.job, "127.0.0.1", port)
            self.connect.assert_not_called()

            with self.subTest(operation="probe", port=port), self.assertRaises(ValueError):
                self.sender.probe("127.0.0.1", port)
            self.connect.assert_not_called()

    def test_cancellation_during_failed_connection_is_logged_as_cancelled(self) -> None:
        cancel = threading.Event()

        def connection_failure(*args, **kwargs):
            cancel.set()
            raise TimeoutError("Connection timed out")

        self.connect.side_effect = connection_failure
        with self.assertRaises(TransferCancelled):
            self.send(cancel_event=cancel)
        self.assertEqual("cancelled", self.transfers.records[0]["status"])

    def test_log_failure_does_not_change_remote_success(self) -> None:
        with patch.object(self.transfers, "write", side_effect=OSError("disk is full")):
            result = self.send()

        self.assertEqual("complete", result["type"])
        self.assertTrue(any("disk is full" in message for message in self.activity.messages))

    def test_log_and_activity_failures_do_not_change_remote_success(self) -> None:
        with patch.object(self.transfers, "write", side_effect=OSError("disk is full")), \
                patch.object(self.activity, "error", side_effect=OSError("logger unavailable")), \
                patch.object(self.activity, "info", side_effect=OSError("logger unavailable")):
            result = self.send()

        self.assertEqual("complete", result["type"])

    def test_log_failure_preserves_original_transfer_error(self) -> None:
        self.source.unlink()
        with patch.object(self.transfers, "write", side_effect=OSError("disk is full")):
            with self.assertRaises(FileNotFoundError):
                self.send()

    def test_invalid_completion_is_not_logged_as_success(self) -> None:
        cases = (
            ("transfer_id", "unrelated-transfer", "INVALID_SESSION"),
            ("sha256", "0" * 64, "CHECKSUM_MISMATCH"),
            ("bytes_received", 1, "SIZE_MISMATCH"),
            ("bytes_received", "8", "SIZE_MISMATCH"),
        )
        for field, value, code in cases:
            with self.subTest(field=field, value=value):
                self.receive.return_value = {**self.complete, field: value}
                with self.assertRaises(ProtocolError) as raised:
                    self.send()
                self.assertEqual(code, raised.exception.code)
                self.assertEqual("failed", self.transfers.records[-1]["status"])

    def test_cancel_interrupts_blocked_handshake_and_completion_wait(self) -> None:
        for blocked_call in (self.handshake, self.receive):
            with self.subTest(blocked_call=blocked_call):
                cancel = threading.Event()
                entered = threading.Event()
                self.sock.shutdown_requested.clear()
                errors: list[Exception] = []

                def blocked(*args, **kwargs):
                    entered.set()
                    if not self.sock.shutdown_requested.wait(3):
                        raise AssertionError("Cancellation did not interrupt the socket")
                    raise ProtocolError("Connection closed", "CONNECTION_CLOSED")

                def run():
                    try:
                        self.send(cancel_event=cancel)
                    except Exception as exc:
                        errors.append(exc)

                blocked_call.side_effect = blocked
                worker = threading.Thread(target=run, daemon=True)
                worker.start()
                try:
                    self.assertTrue(entered.wait(1))
                    cancel.set()
                    worker.join(timeout=1)
                    self.assertFalse(worker.is_alive())
                    self.assertEqual(1, len(errors))
                    self.assertIsInstance(errors[0], TransferCancelled)
                    self.assertEqual("cancelled", self.transfers.records[-1]["status"])
                    self.assertFalse(any(t.name == "lanbridge-send-cancel" for t in threading.enumerate()))
                finally:
                    cancel.set()
                    self.sock.shutdown_requested.set()
                    worker.join(timeout=3)
                    blocked_call.side_effect = None

    def test_cancel_after_last_chunk_does_not_commit_file(self) -> None:
        cancel = threading.Event()

        def cancel_after_data(payload):
            if payload["bytes"] == payload["total"]:
                cancel.set()

        with self.assertRaises(TransferCancelled):
            self.send(cancel_event=cancel, progress_callback=cancel_after_data)

        self.finish.assert_not_called()
        self.assertEqual("cancelled", self.transfers.records[0]["status"])


if __name__ == "__main__":
    unittest.main()
