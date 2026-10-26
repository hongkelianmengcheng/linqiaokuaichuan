from __future__ import annotations

import hashlib
import hmac
import json
import math
import secrets
import socket
import struct
from typing import Any

from .constants import MAX_FRAME_SIZE, PROTOCOL_MAGIC, PROTOCOL_VERSION

FRAME_HEADER = struct.Struct("!4sBI")
MAX_JSON_DEPTH = 128
MAX_JSON_INTEGER_DIGITS = 4096


def _parse_json_int(value: str) -> int:
    if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("控制消息整数过长")
    return int(value)


def _validate_json_value(value: Any, depth: int = 0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise ValueError("控制消息嵌套层级过深")
    if isinstance(value, str):
        value.encode("utf-8")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ValueError("控制消息包含非有限数字")
    elif isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("控制消息键必须是字符串")
            key.encode("utf-8")
            _validate_json_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1)


class ProtocolError(Exception):
    def __init__(self, message: str, code: str = "PROTOCOL_ERROR") -> None:
        super().__init__(message)
        self.code = code


class RemoteError(ProtocolError):
    pass


class AuthenticationError(ProtocolError):
    pass


def recv_exact(sock: socket.socket, length: int) -> bytes:
    if length < 0:
        raise ProtocolError("无效的数据长度")
    chunks = bytearray()
    while len(chunks) < length:
        chunk = sock.recv(length - len(chunks))
        if not chunk:
            raise ProtocolError("连接意外关闭", "CONNECTION_CLOSED")
        chunks.extend(chunk)
    return bytes(chunks)


def encode_frame(payload: dict[str, Any]) -> bytes:
    try:
        _validate_json_value(payload)
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError("控制消息无法编码") from exc
    if len(body) > MAX_FRAME_SIZE:
        raise ProtocolError("控制消息过大", "FRAME_TOO_LARGE")
    return FRAME_HEADER.pack(PROTOCOL_MAGIC, PROTOCOL_VERSION, len(body)) + body


def send_frame(sock: socket.socket, payload: dict[str, Any]) -> None:
    sock.sendall(encode_frame(payload))


def recv_frame(sock: socket.socket) -> dict[str, Any]:
    raw_header = recv_exact(sock, FRAME_HEADER.size)
    magic, version, length = FRAME_HEADER.unpack(raw_header)
    if magic != PROTOCOL_MAGIC:
        raise ProtocolError("目标端口不是邻桥快传服务", "BAD_MAGIC")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(
            f"协议版本不兼容（远端 {version}，本机 {PROTOCOL_VERSION}）",
            "VERSION_MISMATCH",
        )
    if length > MAX_FRAME_SIZE:
        raise ProtocolError("控制消息超过安全上限", "FRAME_TOO_LARGE")
    try:
        payload = json.loads(
            recv_exact(sock, length).decode("utf-8"),
            parse_int=_parse_json_int,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"非法数字：{value}")),
        )
        _validate_json_value(payload)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ProtocolError("控制消息格式无效", "INVALID_JSON") from exc
    if not isinstance(payload, dict):
        raise ProtocolError("控制消息必须是对象", "INVALID_MESSAGE")
    return payload


def derive_access_key(access_code: str) -> bytes:
    normalized = access_code.strip()
    return hashlib.sha256(normalized.encode("utf-8")).digest() if normalized else b""


def access_key_to_hex(access_key: bytes) -> str:
    return access_key.hex() if access_key else ""


def access_key_from_hex(value: str) -> bytes:
    if not value:
        return b""
    try:
        decoded = bytes.fromhex(value)
    except ValueError as exc:
        raise ValueError("保存的访问码摘要无效") from exc
    if len(decoded) != hashlib.sha256().digest_size:
        raise ValueError("保存的访问码摘要长度无效")
    return decoded


def new_nonce() -> bytes:
    return secrets.token_bytes(32)


def _auth_document(hello: dict[str, Any]) -> bytes:
    selected = {
        "action": hello.get("action"),
        "offer": hello.get("offer"),
        "sender": hello.get("sender"),
        "session_id": hello.get("session_id"),
    }
    return json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def auth_digest(access_key: bytes, nonce: bytes, hello: dict[str, Any]) -> str:
    return hmac.new(access_key, nonce + b"\0" + _auth_document(hello), hashlib.sha256).hexdigest()


def verify_auth_digest(access_key: bytes, nonce: bytes, hello: dict[str, Any], supplied: str) -> bool:
    if not access_key:
        return True
    if not isinstance(supplied, str) or len(supplied) != 64 or not supplied.isascii():
        return False
    expected = auth_digest(access_key, nonce, hello)
    return hmac.compare_digest(expected, supplied)


def raise_for_error(message: dict[str, Any]) -> None:
    if message.get("type") != "error":
        return
    code = str(message.get("code") or "REMOTE_ERROR")
    text = str(message.get("message") or "远端返回错误")
    if code == "AUTH_FAILED":
        raise AuthenticationError(text, code)
    raise RemoteError(text, code)


def client_handshake(
    sock: socket.socket,
    *,
    action: str,
    session_id: str,
    sender_identity: dict[str, Any],
    access_code: str = "",
    offer: dict[str, Any] | None = None,
) -> dict[str, Any]:
    hello: dict[str, Any] = {
        "type": "hello",
        "action": action,
        "session_id": session_id,
        "sender": sender_identity,
    }
    if offer is not None:
        hello["offer"] = offer
    send_frame(sock, hello)

    challenge = recv_frame(sock)
    raise_for_error(challenge)
    if challenge.get("type") != "challenge":
        raise ProtocolError("接收端未返回身份验证挑战", "BAD_SEQUENCE")
    nonce_hex = challenge.get("nonce")
    try:
        nonce = bytes.fromhex(str(nonce_hex))
    except ValueError as exc:
        raise ProtocolError("接收端挑战数据无效", "INVALID_CHALLENGE") from exc
    if len(nonce) != 32:
        raise ProtocolError("接收端挑战长度无效", "INVALID_CHALLENGE")

    key = derive_access_key(access_code)
    supplied = auth_digest(key, nonce, hello) if challenge.get("requires_code") else ""
    send_frame(sock, {"type": "auth", "digest": supplied})
    response = recv_frame(sock)
    raise_for_error(response)
    return response


def send_error(sock: socket.socket, code: str, message: str) -> None:
    try:
        send_frame(sock, {"type": "error", "code": code, "message": message})
    except (OSError, ProtocolError):
        pass
