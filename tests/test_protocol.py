from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.constants import MAX_FRAME_SIZE, PROTOCOL_MAGIC, PROTOCOL_VERSION
from lanbridge.protocol import (
    FRAME_HEADER,
    AuthenticationError,
    ProtocolError,
    RemoteError,
    access_key_from_hex,
    access_key_to_hex,
    auth_digest,
    derive_access_key,
    encode_frame,
    raise_for_error,
    recv_exact,
    recv_frame,
    verify_auth_digest,
)


class FragmentedSocket:
    """Minimal socket double that deliberately returns short reads."""

    def __init__(self, payload: bytes, fragment_size: int = 1) -> None:
        self._payload = bytearray(payload)
        self._fragment_size = fragment_size

    def recv(self, requested: int) -> bytes:
        if not self._payload:
            return b""
        count = min(requested, self._fragment_size, len(self._payload))
        result = bytes(self._payload[:count])
        del self._payload[:count]
        return result


class ProtocolFrameTests(unittest.TestCase):
    def test_frame_round_trip_preserves_unicode_and_nested_values(self) -> None:
        payload = {
            "type": "offer",
            "name": "资料/毕业设计-测试.bin",
            "size": 2**40 + 17,
            "flags": [True, False],
            "meta": {"说明": "局域网传输"},
        }

        decoded = recv_frame(FragmentedSocket(encode_frame(payload), fragment_size=3))

        self.assertEqual(payload, decoded)

    def test_recv_exact_handles_one_byte_reads(self) -> None:
        self.assertEqual(b"abcdef", recv_exact(FragmentedSocket(b"abcdef"), 6))

    def test_recv_exact_rejects_negative_length_and_early_close(self) -> None:
        with self.assertRaises(ProtocolError):
            recv_exact(FragmentedSocket(b""), -1)
        with self.assertRaises(ProtocolError) as raised:
            recv_exact(FragmentedSocket(b"abc", fragment_size=2), 4)
        self.assertEqual("CONNECTION_CLOSED", raised.exception.code)

    def test_rejects_bad_magic(self) -> None:
        frame = FRAME_HEADER.pack(b"NOPE", PROTOCOL_VERSION, 2) + b"{}"
        with self.assertRaises(ProtocolError) as raised:
            recv_frame(FragmentedSocket(frame))
        self.assertEqual("BAD_MAGIC", raised.exception.code)

    def test_rejects_unsupported_version(self) -> None:
        frame = FRAME_HEADER.pack(PROTOCOL_MAGIC, (PROTOCOL_VERSION + 1) % 256, 2) + b"{}"
        with self.assertRaises(ProtocolError) as raised:
            recv_frame(FragmentedSocket(frame))
        self.assertEqual("VERSION_MISMATCH", raised.exception.code)

    def test_rejects_oversized_frame_before_reading_body(self) -> None:
        frame = FRAME_HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, MAX_FRAME_SIZE + 1)
        with self.assertRaises(ProtocolError) as raised:
            recv_frame(FragmentedSocket(frame))
        self.assertEqual("FRAME_TOO_LARGE", raised.exception.code)

    def test_rejects_invalid_json_utf8_and_non_object(self) -> None:
        cases = (
            (b"{", "INVALID_JSON"),
            (b"\xff", "INVALID_JSON"),
            (json.dumps(["not", "an", "object"]).encode("utf-8"), "INVALID_MESSAGE"),
        )
        for body, expected_code in cases:
            with self.subTest(body=body):
                frame = FRAME_HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, len(body)) + body
                with self.assertRaises(ProtocolError) as raised:
                    recv_frame(FragmentedSocket(frame, fragment_size=2))
                self.assertEqual(expected_code, raised.exception.code)

    def test_encode_rejects_non_json_value_and_oversized_payload(self) -> None:
        with self.assertRaises(ProtocolError):
            encode_frame({"bad": object()})
        with self.assertRaises(ProtocolError) as raised:
            encode_frame({"data": "x" * (MAX_FRAME_SIZE + 1)})
        self.assertEqual("FRAME_TOO_LARGE", raised.exception.code)


class ProtocolAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hello = {
            "type": "hello",
            "action": "transfer",
            "session_id": "unit-session",
            "sender": {"hostname": "测试电脑", "mac": "00-11-22-33-44-55"},
            "offer": {"relative_path": "目录/文件.bin", "size": 123},
        }
        self.nonce = bytes(range(32))
        self.key = derive_access_key("  correct horse battery staple  ")

    def test_access_key_normalization_and_hex_round_trip(self) -> None:
        self.assertEqual(self.key, derive_access_key("correct horse battery staple"))
        self.assertEqual(self.key, access_key_from_hex(access_key_to_hex(self.key)))
        self.assertEqual(b"", derive_access_key("   "))
        self.assertEqual(b"", access_key_from_hex(""))
        for invalid in ("not-hex", "00"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                access_key_from_hex(invalid)

    def test_digest_accepts_correct_key_and_rejects_wrong_key(self) -> None:
        supplied = auth_digest(self.key, self.nonce, self.hello)
        self.assertTrue(verify_auth_digest(self.key, self.nonce, self.hello, supplied))
        self.assertFalse(
            verify_auth_digest(
                derive_access_key("wrong code"),
                self.nonce,
                self.hello,
                supplied,
            )
        )
        self.assertFalse(verify_auth_digest(self.key, self.nonce, self.hello, 123))

    def test_digest_binds_transfer_offer_and_identity(self) -> None:
        supplied = auth_digest(self.key, self.nonce, self.hello)
        for field, value in (
            ("session_id", "another-session"),
            ("action", "probe"),
            ("sender", {"hostname": "attacker"}),
            ("offer", {"relative_path": "other.bin", "size": 123}),
        ):
            changed = copy.deepcopy(self.hello)
            changed[field] = value
            with self.subTest(field=field):
                self.assertFalse(verify_auth_digest(self.key, self.nonce, changed, supplied))

    def test_empty_server_key_accepts_any_supplied_digest(self) -> None:
        self.assertTrue(verify_auth_digest(b"", self.nonce, self.hello, "anything"))

    def test_remote_error_mapping(self) -> None:
        with self.assertRaises(AuthenticationError) as auth_error:
            raise_for_error({"type": "error", "code": "AUTH_FAILED", "message": "bad"})
        self.assertEqual("AUTH_FAILED", auth_error.exception.code)

        with self.assertRaises(RemoteError) as remote_error:
            raise_for_error({"type": "error", "code": "NO_SPACE", "message": "full"})
        self.assertEqual("NO_SPACE", remote_error.exception.code)


if __name__ == "__main__":
    unittest.main()
