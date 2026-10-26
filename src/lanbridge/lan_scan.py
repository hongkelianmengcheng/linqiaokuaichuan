"""Bounded LAN discovery for the sender's background worker.

UDP advertisements identify LanBridge receivers. TCP/ICMP responses establish
only that a host is alive; the sender must perform its normal protocol probe
after selection. Firewalls can make a live computer undiscoverable.
"""
from __future__ import annotations

import ctypes
import errno
import ipaddress
import queue
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any

from .constants import DEFAULT_DISCOVERY_PORT, DEFAULT_TRANSFER_PORT
from .discovery import _display_text, _scan_timeout, discover_receivers
from .utils import get_local_ipv4_interfaces, validate_port

MAX_SCAN_HOSTS = 512
MAX_PROBE_WORKERS = 32
# OS reverse DNS has no portable per-call timeout. These daemon workers may
# finish after a scan, but this global bound prevents repeated scans from
# accumulating an unlimited number of blocked DNS calls.
_DNS_SLOTS = threading.BoundedSemaphore(8)


def _windows_neighbor_addresses() -> list[str]:
    """Return ARP entries as candidates, never as proof a host is online."""
    if sys.platform != "win32":
        return []
    try:
        get_table = ctypes.WinDLL("iphlpapi").GetIpNetTable
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
            raw = buffer.raw
            count = struct.unpack_from("<I", raw)[0]
            if count > (len(raw) - 4) // 24:
                return []
            return [
                socket.inet_ntoa(raw[4 + index * 24 + 16 : 4 + index * 24 + 20])
                for index in range(count)
                if struct.unpack_from("<I", raw, 4 + index * 24 + 20)[0] in (3, 4)
            ]
    except (AttributeError, OSError, ValueError, struct.error):
        return []
    return []


def _scan_candidates(interfaces: list[tuple[str, str]], neighbors: list[str]) -> list[str]:
    networks = []
    local = set()
    windows = []
    for address, mask in interfaces:
        try:
            interface = ipaddress.IPv4Interface(f"{address}/{mask}")
        except ValueError:
            continue
        if interface.ip.is_loopback or interface.ip.is_unspecified or interface.ip.is_multicast:
            continue
        local.add(str(interface.ip))
        network = interface.network
        if network.prefixlen == 0:
            continue
        networks.append(network)
        # A /16 or /8 must not create tens of thousands of jobs. Nearby /24
        # addresses and cached neighbors are useful within a short UI scan.
        window = network if network.prefixlen >= 23 else ipaddress.IPv4Network(f"{address}/24", strict=False)
        windows.append(iter(window.hosts()))

    candidates = []
    seen = set(local)

    def add(address: str) -> None:
        if address not in seen and len(candidates) < MAX_SCAN_HOSTS:
            seen.add(address)
            candidates.append(address)

    for address in neighbors:
        try:
            candidate = ipaddress.IPv4Address(address)
        except ValueError:
            continue
        if candidate.is_loopback or candidate.is_unspecified or candidate.is_multicast:
            continue
        if any(candidate in network and (network.prefixlen >= 31 or candidate not in (network.network_address, network.broadcast_address)) for network in networks):
            add(str(candidate))
    # Round robin gives each connected adapter a fair share of the bound.
    while windows and len(candidates) < MAX_SCAN_HOSTS:
        remaining = []
        for addresses in windows:
            candidate = next(addresses, None)
            if candidate is not None:
                add(str(candidate))
                remaining.append(addresses)
        windows = remaining
    return candidates


class _IpOptions(ctypes.Structure):
    _fields_ = [("Ttl", ctypes.c_ubyte), ("Tos", ctypes.c_ubyte), ("Flags", ctypes.c_ubyte),
                ("OptionsSize", ctypes.c_ubyte), ("OptionsData", ctypes.c_void_p)]


class _EchoReply(ctypes.Structure):
    _fields_ = [("Address", ctypes.c_uint32), ("Status", ctypes.c_uint32),
                ("RoundTripTime", ctypes.c_uint32), ("DataSize", ctypes.c_ushort),
                ("Reserved", ctypes.c_ushort), ("Data", ctypes.c_void_p), ("Options", _IpOptions)]


def _icmp_alive(address: str, timeout: float) -> bool:
    if sys.platform != "win32":
        try:
            result = subprocess.run(
                ["ping", "-n", "-c", "1", "-W", "1", address],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=timeout, check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
    try:
        api = ctypes.WinDLL("iphlpapi")
        create = api.IcmpCreateFile
        create.argtypes = ()
        create.restype = ctypes.c_void_p
        close = api.IcmpCloseHandle
        close.argtypes = (ctypes.c_void_p,)
        close.restype = ctypes.c_int
        send = api.IcmpSendEcho
        send.argtypes = (ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_ushort,
                         ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32)
        send.restype = ctypes.c_uint32
        handle = create()
        if not handle or handle == ctypes.c_void_p(-1).value:
            return False
        try:
            payload = ctypes.create_string_buffer(b"LanBridge")
            reply = ctypes.create_string_buffer(ctypes.sizeof(_EchoReply) + len(payload) + 16)
            destination = struct.unpack("=I", socket.inet_aton(address))[0]
            count = send(handle, destination, payload, len(payload), None, reply, len(reply), max(1, int(timeout * 1000)))
            return bool(count and _EchoReply.from_buffer(reply).Status == 0)
        finally:
            close(handle)
    except (AttributeError, OSError, ValueError):
        return False


def _probe_host(address: str, port: int, timeout: float) -> str:
    """A refused TCP connection proves liveness but not a receiver service."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(timeout / 2)
            result = probe.connect_ex((address, port))
            if result == 0:
                return "tcp"
            if result in (errno.ECONNREFUSED, 10061):
                return "tcp"
    except OSError:
        pass
    return "ping" if _icmp_alive(address, timeout / 2) else ""


def _netbios_hostname(address: str) -> str:
    """Ask a Windows host directly for its unique workstation name."""
    transaction = secrets.randbelow(65536)
    encoded_name = b"".join(bytes((65 + (value >> 4), 65 + (value & 15))) for value in b"*" + b"\0" * 15)
    query = struct.pack("!6H", transaction, 0, 1, 0, 0, 0) + b"\x20" + encoded_name + b"\0\0\x21\0\x01"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(0.18)
            probe.sendto(query, (address, 137))
            response, peer = probe.recvfrom(2048)
            if peer[0] != address or peer[1] != 137:
                return ""
        return _parse_netbios_reply(response, transaction)
    except (OSError, ValueError, struct.error):
        return ""


def _parse_netbios_reply(data: bytes, transaction: int) -> str:
    if len(data) < 12:
        return ""
    ident, flags, questions, answers, _authority, _additional = struct.unpack_from("!6H", data)
    if ident != transaction or not flags & 0x8000 or flags & 0xF or questions > 4 or answers > 16:
        return ""

    def skip_name(offset: int) -> int:
        for _label in range(128):
            if offset >= len(data):
                raise ValueError("截断的主机名响应")
            length = data[offset]
            if length == 0:
                return offset + 1
            if length & 0xC0 == 0xC0:
                if offset + 1 >= len(data):
                    raise ValueError("截断的主机名指针")
                return offset + 2
            if length > 63 or offset + 1 + length >= len(data):
                raise ValueError("无效的主机名响应")
            offset += length + 1
        raise ValueError("主机名响应过长")

    try:
        offset = 12
        for _question in range(questions):
            offset = skip_name(offset) + 4
        for _answer in range(answers):
            offset = skip_name(offset)
            record_type, record_class, _ttl, size = struct.unpack_from("!HHIH", data, offset)
            offset += 10
            end = offset + size
            if end > len(data):
                return ""
            if record_type == 0x21 and record_class == 1 and size:
                count = data[offset]
                if 1 + count * 18 > size:
                    return ""
                for index in range(count):
                    start = offset + 1 + index * 18
                    suffix = data[start + 15]
                    name_flags = struct.unpack_from("!H", data, start + 16)[0]
                    if suffix == 0 and not name_flags & 0x8000:
                        raw = data[start : start + 15].rstrip(b" \0")
                        name = _display_text(raw.decode("mbcs" if sys.platform == "win32" else "ascii", "replace"))
                        if name and name != "*":
                            return name
            offset = end
    except (ValueError, IndexError, struct.error):
        return ""
    return ""


def _resolve_hostname(address: str) -> str:
    netbios_name = _netbios_hostname(address)
    if netbios_name:
        return netbios_name
    try:
        name = _display_text(socket.gethostbyaddr(address)[0])
        try:
            ipaddress.ip_address(name)
        except ValueError:
            return name or "未知主机"
        return "未知主机"
    except (OSError, UnicodeError):
        return "未知主机"


def discover_lan_hosts(
    *,
    port: int = DEFAULT_TRANSFER_PORT,
    timeout: float = 4.0,
    cancel_event: threading.Event | None = None,
    discovery_port: int = DEFAULT_DISCOVERY_PORT,
) -> list[dict[str, Any]]:
    """Return online IPv4 hosts in at most ``timeout`` seconds of probing.

    Call from a UI worker. Every record has ip, hostname, port, online,
    is_receiver, requires_code, source and status. A receiver advertisement
    retains its announced port (including automatically selected ports).
    Scans probe at most 512 addresses across real local subnets and stop
    scheduling immediately on cancellation. DNS workers are separately bounded.
    """
    port = validate_port(port)
    discovery_port = validate_port(discovery_port)
    duration = _scan_timeout(timeout)
    if cancel_event is not None and cancel_event.is_set():
        return []
    deadline = time.monotonic() + duration
    stopped = threading.Event()
    events: queue.Queue = queue.Queue()
    names: queue.Queue = queue.Queue()
    pending: queue.Queue = queue.Queue()
    found: dict[tuple[str, int], dict[str, Any]] = {}
    resolver_threads: list[threading.Thread] = []

    def cancelled() -> bool:
        return stopped.is_set() or (cancel_event is not None and cancel_event.is_set()) or time.monotonic() >= deadline

    interfaces = get_local_ipv4_interfaces()
    try:
        local_name = _display_text(socket.gethostname()) or "本机"
    except OSError:
        local_name = "本机"

    def host_record(address: str, source: str) -> dict[str, Any]:
        return {"ip": address, "port": port, "hostname": local_name if source == "local" else "未知主机",
                "online": True, "is_receiver": False, "requires_code": False, "mac": "",
                "source": source, "status": "本机，待检测" if source == "local" else "主机在线，待检测"}

    for address, _mask in interfaces:
        found[(address, port)] = host_record(address, "local")
    candidates = _scan_candidates(interfaces, _windows_neighbor_addresses())
    for address in candidates:
        pending.put(address)

    def receive_advertisements() -> None:
        try:
            discover_receivers(
                timeout=min(1.5, duration), discovery_port=discovery_port, cancel_event=stopped,
                on_receiver=lambda receiver: events.put(("receiver", receiver)),
            )
        except OSError:
            pass

    def probe_worker() -> None:
        while not cancelled():
            try:
                address = pending.get_nowait()
            except queue.Empty:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            source = _probe_host(address, port, min(0.3, remaining))
            if source and not cancelled():
                events.put(("host", host_record(address, source)))
                names.put(address)

    def name_worker() -> None:
        try:
            while not cancelled():
                try:
                    address = names.get(timeout=0.05)
                except queue.Empty:
                    continue
                if address is None or cancelled():
                    break
                events.put(("name", (address, _resolve_hostname(address))))
        finally:
            _DNS_SLOTS.release()

    def merge(kind: str, payload: Any) -> None:
        if kind == "name":
            address, name = payload
            for key, record in found.items():
                if key[0] == address and not record["is_receiver"]:
                    record["hostname"] = name
        elif kind == "receiver":
            payload.update(online=True, is_receiver=True, source="discovery", status="接收端在线")
            # Replace the generic host row when an auto-selected receiver port
            # differs from the user's current port. Retain distinct receivers.
            for key in list(found):
                if key[0] == payload["ip"] and not found[key]["is_receiver"]:
                    del found[key]
            found[(payload["ip"], payload["port"])] = payload
        elif not any(key[0] == payload["ip"] and record["is_receiver"] for key, record in found.items()):
            found[(payload["ip"], payload["port"])] = payload

    threading.Thread(target=receive_advertisements, name="lanbridge-udp-scan", daemon=True).start()
    for index in range(min(MAX_PROBE_WORKERS, pending.qsize())):
        threading.Thread(target=probe_worker, name=f"lanbridge-host-scan-{index}", daemon=True).start()
    for index in range(min(8, len(candidates))):
        if _DNS_SLOTS.acquire(blocking=False):
            worker = threading.Thread(target=name_worker, name=f"lanbridge-host-name-{index}", daemon=True)
            resolver_threads.append(worker)
            worker.start()
    try:
        while not cancelled():
            try:
                kind, payload = events.get(timeout=min(0.05, max(0.001, deadline - time.monotonic())))
                merge(kind, payload)
            except queue.Empty:
                pass
    finally:
        stopped.set()
        # Wake idle resolvers immediately, so a quick subsequent scan can use
        # the same bounded slots. Never wait indefinitely for OS reverse DNS.
        for _worker in resolver_threads:
            names.put(None)
        shutdown_deadline = time.monotonic() + 0.025
        for worker in resolver_threads:
            worker.join(timeout=max(0, shutdown_deadline - time.monotonic()))
    while True:
        try:
            merge(*events.get_nowait())
        except queue.Empty:
            break
    return sorted(found.values(), key=lambda item: (not item["is_receiver"], int(ipaddress.IPv4Address(item["ip"])), item["port"]))
