"""Small, bounded connection history; credentials are never persisted."""
from __future__ import annotations

import math
import time
from typing import Any

from .constants import DEFAULT_TRANSFER_PORT
from .utils import validate_port

MEMORY_SECONDS = 7 * 24 * 60 * 60
MAX_RECENT_ENDPOINTS = 8


def recent_endpoints(settings: dict[str, Any], now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    result = []
    seen = set()
    values = settings.get("recent_endpoints", [])
    if not isinstance(values, list):
        return result
    for item in values[:64]:
        if not isinstance(item, dict):
            continue
        host = item.get("host")
        stamp = item.get("used_at")
        if not isinstance(host, str) or not host.strip() or len(host) > 253:
            continue
        if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
            continue
        try:
            stamp = float(stamp)
        except (OverflowError, ValueError):
            continue
        if not math.isfinite(stamp):
            continue
        if not 0 <= now - stamp <= MEMORY_SECONDS:
            continue
        try:
            port = validate_port(item.get("port"))
        except ValueError:
            continue
        key = (host.strip(), port)
        if key in seen:
            continue
        seen.add(key)
        result.append({"host": key[0], "port": port, "used_at": float(stamp)})
        if len(result) >= MAX_RECENT_ENDPOINTS:
            break
    return result


def initial_endpoint(settings: dict[str, Any], local_ips: list[str]) -> tuple[str, int, bool]:
    recent = recent_endpoints(settings)
    if recent:
        return recent[0]["host"], recent[0]["port"], True
    # One-time migration of the previous release's saved destination.
    host = settings.get("receiver_host", "")
    if not settings.get("memory_initialized") and isinstance(host, str) and host.strip() not in {"", "127.0.0.1"}:
        try:
            return host.strip(), validate_port(settings.get("receiver_port")), True
        except ValueError:
            pass
    return (local_ips[0] if local_ips else "127.0.0.1"), DEFAULT_TRANSFER_PORT, False


def remember_endpoint(settings: dict[str, Any], host: str, port: int) -> None:
    host = host.strip()
    if not host or len(host) > 253:
        return
    port = validate_port(port)
    previous = recent_endpoints(settings)
    current = {"host": host, "port": port, "used_at": time.time()}
    settings.update({
        "receiver_host": host,
        "receiver_port": port,
        "memory_initialized": True,
        "recent_endpoints": [current] + [item for item in previous if (item["host"], item["port"]) != (host, port)][:MAX_RECENT_ENDPOINTS - 1],
    })
