from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.constants import PROTOCOL_MAGIC, PROTOCOL_VERSION
from lanbridge.protocol import FRAME_HEADER, ProtocolError, encode_frame, recv_frame, verify_auth_digest


class MemorySocket:
    def __init__(self, data: bytes):
        self.data = data

    def recv(self, size: int) -> bytes:
        result, self.data = self.data[:size], self.data[size:]
        return result


class ProtocolRegressionTests(unittest.TestCase):
    def test_malformed_digests_are_rejected_without_type_error(self) -> None:
        for supplied in ("码" * 64, "a" * 63, "a" * 65, "g" * 64, "\ud800" * 64, None, 123):
            with self.subTest(supplied=repr(supplied)):
                self.assertFalse(verify_auth_digest(b"key", bytes(32), {}, supplied))

    def test_invalid_json_values_raise_protocol_errors(self) -> None:
        cases = (
            b'{"name":"\\ud800"}',
            b'{"\\udfff":"name"}',
            b'{"nested":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
            b'{"number":' + b"1" * 6000 + b"}",
            b'{"number":1e9999}',
        )
        for body in cases:
            with self.subTest(body=body[:60]):
                header = FRAME_HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, len(body))
                with self.assertRaises(ProtocolError) as raised:
                    recv_frame(MemorySocket(header + body))
                self.assertEqual("INVALID_JSON", raised.exception.code)

    def test_invalid_outgoing_values_raise_protocol_errors(self) -> None:
        nested: dict = {}
        for _ in range(2000):
            nested = {"nested": nested}
        for value in ({"name": "\ud800"}, {"number": float("inf")}, nested):
            with self.assertRaises(ProtocolError):
                encode_frame(value)


if __name__ == "__main__":
    unittest.main()
