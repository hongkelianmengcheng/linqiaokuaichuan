from __future__ import annotations

import ctypes
import ipaddress
import os
import re
import socket
import struct
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    *(f"{prefix}{index}" for prefix in ("COM", "LPT") for index in "¹²³"),
}
INVALID_WINDOWS_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def local_time_display(utc_value: str) -> str:
    try:
        parsed = datetime.fromisoformat(utc_value.replace("Z", "+00:00"))
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return str(utc_value)


def format_mac(node: int | None = None) -> str:
    value = uuid.getnode() if node is None else node
    return "-".join(f"{(value >> offset) & 0xFF:02X}" for offset in range(40, -1, -8))


def local_identity() -> dict[str, str]:
    return {"hostname": socket.gethostname(), "mac": format_mac()}


def get_local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    if sys.platform == "win32":
        addresses.update(address for address, _mask in _windows_ipv4_interfaces())
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
            address = info[4][0]
            if address and not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        if address and not address.startswith("127."):
            addresses.add(address)
    except OSError:
        pass
    finally:
        probe.close()
    return sorted(addresses)


def _windows_ipv4_interfaces() -> list[tuple[str, str]]:
    """Read actual interface addresses/masks without running a console process.

    GetIpAddrTable is available on Windows 7 and its fixed-width rows have the
    same layout on 32-bit and 64-bit Windows.
    """
    if sys.platform != "win32":
        return []
    try:
        get_table = ctypes.WinDLL("iphlpapi").GetIpAddrTable
        get_table.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int)
        get_table.restype = ctypes.c_ulong
        size = ctypes.c_ulong(0)
        if get_table(None, ctypes.byref(size), False) != 122:
            return []
        for _attempt in range(3):
            if not 4 <= size.value <= 1024 * 1024:
                return []
            buffer = ctypes.create_string_buffer(size.value)
            status = get_table(buffer, ctypes.byref(size), False)
            if status == 122:
                continue
            if status:
                return []
            count = struct.unpack_from("<I", buffer.raw)[0]
            if count > (len(buffer) - 4) // 24:
                return []
            result = []
            for index in range(count):
                offset = 4 + index * 24
                address = socket.inet_ntoa(buffer.raw[offset : offset + 4])
                mask = socket.inet_ntoa(buffer.raw[offset + 8 : offset + 12])
                # Do not scan unconfigured/disconnected, loopback, or default
                # routes. Validation also rejects non-contiguous masks.
                interface = ipaddress.IPv4Interface(f"{address}/{mask}")
                if not interface.ip.is_loopback and not interface.ip.is_unspecified:
                    if interface.network.prefixlen and not interface.ip.is_multicast:
                        result.append((address, mask))
            return sorted(set(result))
    except (AttributeError, OSError, ValueError, struct.error):
        return []
    return []


def get_local_ipv4_interfaces() -> list[tuple[str, str]]:
    """Return (IPv4, real netmask), using /32 when the mask is unavailable.

    The conservative fallback still permits UDP discovery; it never guesses a
    /24 and accidentally scans addresses outside the attached network.
    """
    interfaces = _windows_ipv4_interfaces()
    if interfaces:
        return interfaces
    if sys.platform != "win32":
        try:
            import fcntl

            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                for _index, name in socket.if_nameindex():
                    request = struct.pack("256s", name.encode("utf-8")[:15])
                    try:
                        address = socket.inet_ntoa(fcntl.ioctl(probe, 0x8915, request)[20:24])
                        mask = socket.inet_ntoa(fcntl.ioctl(probe, 0x891B, request)[20:24])
                        interface = ipaddress.IPv4Interface(f"{address}/{mask}")
                        if not interface.ip.is_loopback and not interface.ip.is_unspecified:
                            interfaces.append((address, mask))
                    except (OSError, ValueError):
                        continue
        except (ImportError, AttributeError, OSError):
            pass
    return sorted(set(interfaces)) or [(address, "255.255.255.255") for address in get_local_ipv4_addresses()]


def format_bytes(value: int | float) -> str:
    size = float(max(0, value))
    units = ("B", "KB", "MB", "GB", "TB", "PB")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} PB"


def format_rate(bytes_per_second: int | float) -> str:
    return f"{format_bytes(bytes_per_second)}/s"


def validate_port(value: Any, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("端口必须是整数")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("端口必须是数字") from exc
    lower = 0 if allow_zero else 1
    if not lower <= port <= 65535:
        raise ValueError(f"端口范围必须是 {lower} 到 65535")
    return port


def sanitize_filename_component(value: str) -> str:
    cleaned = INVALID_WINDOWS_CHARS.sub("_", value).strip().rstrip(". ")
    if not cleaned:
        cleaned = "未命名"
    stem = cleaned.split(".", 1)[0].rstrip().upper()
    if stem in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    if _utf16_length(cleaned) > 180:
        suffix = Path(cleaned).suffix
        # A very long extension must not make the final name exceed the limit.
        suffix_units = _utf16_length(suffix)
        if suffix_units >= 180:
            cleaned = _truncate_utf16(cleaned, 180)
        else:
            stem = cleaned[: -len(suffix)] if suffix else cleaned
            cleaned = f"{_truncate_utf16(stem, 180 - suffix_units)}{suffix}"
        cleaned = cleaned.rstrip(". ") or "未命名"
    return cleaned


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _truncate_utf16(value: str, maximum_units: int) -> str:
    used = 0
    result: list[str] = []
    for character in value:
        units = 2 if ord(character) > 0xFFFF else 1
        if used + units > maximum_units:
            break
        result.append(character)
        used += units
    return "".join(result)


def normalize_relative_path(value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("文件相对路径为空")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("不允许绝对路径")
    # Validate before pathlib collapses '.' and empty components. A collapsed
    # root path could otherwise acquire a collision suffix outside the root.
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("文件路径包含不安全的目录")
    return Path(*(sanitize_filename_component(part) for part in parts))


def safe_destination(root: Path, relative_path: str) -> Path:
    root_resolved = root.expanduser().resolve()
    relative = normalize_relative_path(relative_path)
    destination = (root_resolved / relative).resolve(strict=False)
    try:
        common = os.path.commonpath((str(root_resolved), str(destination)))
    except ValueError as exc:
        raise ValueError("目标路径不在接收目录内") from exc
    if destination == root_resolved or os.path.normcase(common) != os.path.normcase(str(root_resolved)):
        raise ValueError("目标路径不在接收目录内")
    return destination


def collision_candidate(path: Path, index: int) -> Path:
    if index <= 0:
        return path
    return path.with_name(f"{path.stem} ({index}){path.suffix}")


def filesystem_name(path: Path) -> str:
    if sys.platform != "win32":
        return ""
    existing = path.expanduser()
    while not existing.exists() and existing.parent != existing:
        existing = existing.parent
    try:
        volume_path = ctypes.create_unicode_buffer(261)
        if not ctypes.windll.kernel32.GetVolumePathNameW(str(existing), volume_path, len(volume_path)):
            return ""
        fs_name = ctypes.create_unicode_buffer(261)
        if not ctypes.windll.kernel32.GetVolumeInformationW(
            volume_path.value,
            None,
            0,
            None,
            None,
            None,
            fs_name,
            len(fs_name),
        ):
            return ""
        return fs_name.value.upper()
    except (AttributeError, OSError):
        return ""


def configure_tcp_socket(sock: socket.socket) -> None:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass


def resource_path(relative: str) -> Path:
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return bundle_root / relative
