from __future__ import annotations

import json
import ipaddress
import math
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from .constants import (
    APP_VERSION,
    DEFAULT_DISCOVERY_PORT,
    DEFAULT_TRANSFER_PORT,
    DISCOVERY_MAGIC,
    DISCOVERY_QUERY,
    DISCOVERY_REPLY,
    PROTOCOL_VERSION,
)
from .utils import get_local_ipv4_interfaces, validate_port


def _scan_timeout(value: float) -> float:
    duration = float(value)
    if not math.isfinite(duration):
        raise ValueError("扫描时间必须是有限数字")
    return max(0.1, min(duration, 15.0))


def _display_text(value: Any, limit: int = 128) -> str:
    if not isinstance(value, str):
        return ""
    # Responses come from the network and eventually reach Tk text widgets.
    # Strip control characters and invalid Unicode rather than crashing Tk.
    value = value.encode("utf-8", "replace").decode("utf-8")
    return "".join(character for character in value if character.isprintable())[:limit].strip()


def _parse_json_int(value: str) -> int:
    if len(value.lstrip("-")) > 4096:
        raise ValueError("发现消息整数过长")
    return int(value)


def _encode(payload: dict[str, Any]) -> bytes:
    return DISCOVERY_MAGIC + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _decode(data: bytes) -> dict[str, Any] | None:
    if not data.startswith(DISCOVERY_MAGIC) or len(data) > 8192:
        return None
    try:
        payload = json.loads(
            data[len(DISCOVERY_MAGIC) :].decode("utf-8"),
            parse_int=_parse_json_int,
            parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"非法数字：{value}")),
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


class DiscoveryResponder:
    def __init__(
        self,
        info_provider: Callable[[], dict[str, Any]],
        *,
        discovery_port: int = DEFAULT_DISCOVERY_PORT,
    ) -> None:
        self.info_provider = info_provider
        self.discovery_port = discovery_port
        self._stop_event = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            udp.settimeout(0.5)
            udp.bind(("0.0.0.0", self.discovery_port))
        except OSError:
            udp.close()
            raise
        self._socket = udp
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, args=(udp,), name="lanbridge-discovery", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._socket:
            try:
                self._socket.close()
            except OSError:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.5)
        self._socket = None
        self._thread = None

    def _run(self, udp: socket.socket) -> None:
        while not self._stop_event.is_set():
            try:
                data, address = udp.recvfrom(8193)
            except socket.timeout:
                continue
            except OSError:
                break
            request = _decode(data)
            if not request or request.get("type") != DISCOVERY_QUERY:
                continue
            if type(request.get("protocol_version")) is not int or request.get("protocol_version") != PROTOCOL_VERSION:
                continue
            try:
                response = {
                    "type": DISCOVERY_REPLY,
                    "app_version": APP_VERSION,
                    "protocol_version": PROTOCOL_VERSION,
                    **self.info_provider(),
                }
                encoded = _encode(response)
                if len(encoded) <= 8192:
                    udp.sendto(encoded, address)
            except (OSError, TypeError, ValueError, RecursionError):
                continue


def discover_receivers(
    *,
    timeout: float = 1.5,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
    cancel_event: threading.Event | None = None,
    on_receiver: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    duration = _scan_timeout(timeout)
    discovery_port = validate_port(discovery_port)
    if cancel_event is not None and cancel_event.is_set():
        return []
    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[tuple[str, int], dict[str, Any]] = {}
    try:
        udp.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp.bind(("0.0.0.0", 0))
        query = _encode({"type": DISCOVERY_QUERY, "protocol_version": PROTOCOL_VERSION})
        targets = {"255.255.255.255", "127.0.0.1"}
        for address, mask in get_local_ipv4_interfaces():
            try:
                network = ipaddress.IPv4Network(f"{address}/{mask}", strict=False)
                if network.prefixlen < 31:
                    targets.add(str(network.broadcast_address))
            except ValueError:
                continue
        for target_ip in sorted(targets):
            try:
                udp.sendto(query, (target_ip, discovery_port))
            except OSError:
                continue
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline and not (cancel_event is not None and cancel_event.is_set()):
            udp.settimeout(min(0.2, max(0.01, deadline - time.monotonic())))
            try:
                data, address = udp.recvfrom(8193)
            except socket.timeout:
                continue
            except OSError as exc:
                # Windows reports a UDP ICMP port-unreachable as 10054. This
                # is normal when the local receiver has not been started.
                if getattr(exc, "winerror", None) == 10054 or exc.errno in (10054, 10061, 111):
                    continue
                break
            response = _decode(data)
            if not response or response.get("type") != DISCOVERY_REPLY:
                continue
            if type(response.get("protocol_version")) is not int or response.get("protocol_version") != PROTOCOL_VERSION:
                continue
            try:
                port = validate_port(response.get("port"))
            except ValueError:
                continue
            item = {
                "type": DISCOVERY_REPLY,
                "protocol_version": PROTOCOL_VERSION,
                "port": port,
                "ip": address[0],
                "hostname": _display_text(response.get("hostname")) or "未知主机",
                "mac": _display_text(response.get("mac"), 64),
                "app_version": _display_text(response.get("app_version"), 32),
                "requires_code": response.get("requires_code") is True,
            }
            key = (address[0], port)
            if key in found or len(found) < 512:
                found[key] = item
                if on_receiver is not None:
                    on_receiver(dict(item))
    finally:
        udp.close()
    return sorted(found.values(), key=lambda item: (str(item.get("hostname", "")), item["ip"], item["port"]))


def discover_lan_hosts(
    *,
    port: int = DEFAULT_TRANSFER_PORT,
    timeout: float = 4.0,
    cancel_event: threading.Event | None = None,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
) -> list[dict[str, Any]]:
    """Scan in a caller-owned background thread; see lan_scan for the contract."""
    from .lan_scan import discover_lan_hosts as scan

    return scan(port=port, timeout=timeout, cancel_event=cancel_event, discovery_port=discovery_port)
