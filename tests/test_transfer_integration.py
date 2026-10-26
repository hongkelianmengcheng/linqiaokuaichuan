from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.logging_store import TransferLogger
from lanbridge.models import SendJob
from lanbridge.protocol import (
    AuthenticationError,
    client_handshake,
    derive_access_key,
    recv_frame,
    send_frame,
)
from lanbridge.receiver_service import ReceiverServer
from lanbridge.sender_service import SenderService


class MemoryActivityLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.messages.append(("INFO", message))

    def warning(self, message: str) -> None:
        self.messages.append(("WARNING", message))

    def error(self, message: str) -> None:
        self.messages.append(("ERROR", message))


class TransferIntegrationTests(unittest.TestCase):
    ACCESS_CODE = "unit-test access code"

    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.root = Path(self._temp.name)
        self.data_dir = self.root / "app-data"
        self.downloads = self.root / "received"
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self._env = patch.dict(os.environ, {"LANBRIDGE_DATA_DIR": str(self.data_dir)})
        self._env.start()
        self.events: list[dict[str, object]] = []
        self.receiver = ReceiverServer(
            self.downloads,
            access_key=derive_access_key(self.ACCESS_CODE),
            event_callback=self.events.append,
            activity_logger=MemoryActivityLogger(),
            transfer_logger=TransferLogger("receiver"),
        )
        self.port = self.receiver.start(0, auto_port=False)
        self.sender = SenderService(
            activity_logger=MemoryActivityLogger(),
            transfer_logger=TransferLogger("sender"),
        )

    def tearDown(self) -> None:
        self.receiver.stop()
        self._env.stop()
        self._temp.cleanup()

    def _make_job(self, source_name: str, relative_path: str, payload: bytes) -> SendJob:
        source = self.sources / source_name
        source.write_bytes(payload)
        return SendJob(source_path=source, relative_path=relative_path, size=len(payload))

    def _send(self, job: SendJob, access_code: str | None = None) -> dict[str, object]:
        return self.sender.send_file(
            job,
            "127.0.0.1",
            self.port,
            self.ACCESS_CODE if access_code is None else access_code,
        )

    def _records(self, role: str) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for path in sorted((self.data_dir / "logs").glob(f"{role}-transfers-*.jsonl")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if line:
                    records.append(json.loads(line))
        return records

    def _wait_until(self, predicate, timeout: float = 2.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition was not satisfied before timeout")

    def test_zero_byte_transfer_and_structured_logs(self) -> None:
        job = self._make_job("empty-source.bin", "空目录/空文件.txt", b"")

        result = self._send(job)

        expected_digest = hashlib.sha256(b"").hexdigest()
        destination = self.downloads / str(result["saved_relative_path"])
        self.assertTrue(destination.is_file())
        self.assertEqual(b"", destination.read_bytes())
        self.assertEqual(expected_digest, result["sha256"])
        self.assertEqual(0, result["bytes_received"])

        self._wait_until(lambda: len(self._records("receiver")) == 1)
        sender_record = self._records("sender")[0]
        receiver_record = self._records("receiver")[0]
        for record in (sender_record, receiver_record):
            self.assertEqual("success", record["status"])
            self.assertEqual(job.transfer_id, record["transfer_id"])
            self.assertEqual(expected_digest, record["sha256"])
            self.assertEqual(0, record["bytes_transferred"])
            self.assertTrue(record["sender_ip"])
            self.assertTrue(record["receiver_ip"])
            self.assertTrue(record["event_time_utc"])

    def test_unicode_nested_path_checksum_and_payload(self) -> None:
        payload = ("毕业设计局域网传输\n" * 4096).encode("utf-8") + bytes(range(256))
        job = self._make_job("source.bin", "课程资料/第一章/报告-最终版.bin", payload)

        result = self._send(job)

        destination = self.downloads / str(result["saved_relative_path"])
        self.assertEqual(payload, destination.read_bytes())
        self.assertEqual(hashlib.sha256(payload).hexdigest(), result["sha256"])
        self.assertEqual(len(payload), result["bytes_received"])

    def test_existing_name_gets_collision_suffix_without_overwrite(self) -> None:
        first_payload = b"first version"
        second_payload = b"second version"
        first = self._make_job("first.bin", "重复目录/报告.txt", first_payload)
        second = self._make_job("second.bin", "重复目录/报告.txt", second_payload)

        first_result = self._send(first)
        second_result = self._send(second)

        first_path = self.downloads / str(first_result["saved_relative_path"])
        second_path = self.downloads / str(second_result["saved_relative_path"])
        self.assertNotEqual(first_path, second_path)
        self.assertEqual("报告.txt", first_path.name)
        self.assertEqual("报告 (1).txt", second_path.name)
        self.assertEqual(first_payload, first_path.read_bytes())
        self.assertEqual(second_payload, second_path.read_bytes())

    def test_correct_and_wrong_access_codes(self) -> None:
        wrong_job = self._make_job("denied.bin", "denied.bin", b"must not arrive")

        with self.assertRaises(AuthenticationError) as raised:
            self._send(wrong_job, access_code="wrong access code")

        self.assertEqual("AUTH_FAILED", raised.exception.code)
        self.assertFalse((self.downloads / "denied.bin").exists())
        sender_records = self._records("sender")
        self.assertEqual("failed", sender_records[-1]["status"])
        self.assertEqual("AUTH_FAILED", sender_records[-1]["error_code"])

        probe = self.sender.probe("127.0.0.1", self.port, self.ACCESS_CODE)
        self.assertEqual("pong", probe["type"])
        self.assertEqual(self.port, probe["receiver_port"])

        with self.assertRaises(AuthenticationError) as probe_error:
            self.sender.probe("127.0.0.1", self.port, "wrong access code")
        self.assertEqual("AUTH_FAILED", probe_error.exception.code)

    def test_requested_busy_port_auto_advances(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1) if hasattr(
            socket, "SO_EXCLUSIVEADDRUSE"
        ) else None
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        blocked_port = int(blocker.getsockname()[1])
        alternate = ReceiverServer(
            self.root / "alternate-received",
            access_key=derive_access_key(self.ACCESS_CODE),
            activity_logger=MemoryActivityLogger(),
            transfer_logger=TransferLogger("receiver-alternate"),
        )
        try:
            selected = alternate.start(blocked_port, auto_port=True)
            self.assertNotEqual(blocked_port, selected)
            self.assertGreaterEqual(selected, blocked_port + 1)
            self.assertLessEqual(selected, min(65535, blocked_port + 20))
            self.assertTrue(alternate.is_running)
        finally:
            alternate.stop()
            blocker.close()

    def test_checksum_mismatch_is_rejected_and_partial_file_removed(self) -> None:
        payload = b"payload whose advertised checksum is deliberately wrong"
        relative_path = "校验/损坏.bin"
        session_id = "checksum-mismatch-session"
        offer = {
            "file_name": "损坏.bin",
            "relative_path": relative_path,
            "size": len(payload),
            "modified_ns": 0,
        }
        with socket.create_connection(("127.0.0.1", self.port), timeout=3.0) as sock:
            sock.settimeout(3.0)
            ready = client_handshake(
                sock,
                action="transfer",
                session_id=session_id,
                sender_identity={"hostname": "raw-test-client", "mac": "00-00-00-00-00-01"},
                access_code=self.ACCESS_CODE,
                offer=offer,
            )
            self.assertEqual("ready", ready["type"])
            sock.sendall(payload)
            send_frame(
                sock,
                {
                    "type": "finish",
                    "sha256": "0" * 64,
                    "bytes_sent": len(payload),
                    "source_changed": False,
                },
            )
            response = recv_frame(sock)

        self.assertEqual("error", response["type"])
        self.assertEqual("CHECKSUM_MISMATCH", response["code"])
        self.assertFalse((self.downloads / "校验" / "损坏.bin").exists())
        self.assertEqual([], list(self.downloads.rglob("*.part")))

        self._wait_until(lambda: bool(self._records("receiver")))
        failure = self._records("receiver")[-1]
        self.assertEqual("failed", failure["status"])
        self.assertEqual("CHECKSUM_MISMATCH", failure["error_code"])
        self.assertEqual(len(payload), failure["bytes_transferred"])


if __name__ == "__main__":
    unittest.main()
