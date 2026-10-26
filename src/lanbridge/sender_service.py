from __future__ import annotations

import hashlib
import os
import socket
import stat
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .constants import (
    APP_VERSION,
    CHUNK_SIZE,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_IO_TIMEOUT,
    PROTOCOL_VERSION,
    ROLE_SENDER,
)
from .logging_store import ActivityLogger, TransferLogger, write_transfer_record
from .models import SendJob
from .protocol import ProtocolError, client_handshake, raise_for_error, recv_frame, send_frame
from .utils import configure_tcp_socket, local_identity, utc_now_iso, validate_port


class TransferCancelled(Exception):
    pass


# Keep this runtime alias unsubscripted: Python 3.8 cannot subscript
# ``collections.abc.Callable`` at module import time.
ProgressCallback = Callable


@contextmanager
def _cancel_socket_on_request(sock: socket.socket, cancel: threading.Event) -> Iterator[None]:
    """Interrupt blocking socket I/O without closing a socket owned by another thread."""
    stopped = threading.Event()

    def watch() -> None:
        while not stopped.wait(0.05):
            if cancel.is_set():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    watcher = threading.Thread(target=watch, name="lanbridge-send-cancel", daemon=True)
    watcher.start()
    try:
        if cancel.is_set():
            raise TransferCancelled("发送已取消")
        yield
    except (OSError, ProtocolError) as exc:
        if cancel.is_set():
            raise TransferCancelled("用户取消了发送") from exc
        raise
    finally:
        stopped.set()
        watcher.join()


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class SenderService:
    def __init__(
        self,
        *,
        activity_logger: ActivityLogger | None = None,
        transfer_logger: TransferLogger | None = None,
    ) -> None:
        self.activity = activity_logger or ActivityLogger(ROLE_SENDER)
        self.transfers = transfer_logger or TransferLogger(ROLE_SENDER)

    def _identity(self) -> dict[str, Any]:
        return {**local_identity(), "app_version": APP_VERSION, "protocol_version": PROTOCOL_VERSION}

    def _log_activity(self, level: str, message: str) -> None:
        try:
            getattr(self.activity, level)(message)
        except Exception:
            pass

    def _emit_progress(self, callback: ProgressCallback | None, payload: dict[str, Any]) -> None:
        if callback:
            try:
                callback(payload)
            except Exception as exc:
                self._log_activity("error", f"发送进度回调失败：{exc}")

    def probe(
        self,
        host: str,
        port: int,
        access_code: str = "",
        *,
        timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> dict[str, Any]:
        port = validate_port(port)
        session_id = f"probe-{os.urandom(8).hex()}"
        self._log_activity("info", f"正在检测 {host}:{port}")
        with socket.create_connection((host, port), timeout=timeout) as sock:
            configure_tcp_socket(sock)
            sock.settimeout(timeout)
            response = client_handshake(
                sock,
                action="probe",
                session_id=session_id,
                sender_identity=self._identity(),
                access_code=access_code,
            )
            if response.get("type") != "pong":
                raise ProtocolError("目标端未返回通信检测结果", "BAD_SEQUENCE")
            local_ip, local_port = sock.getsockname()[:2]
            remote_ip, remote_port = sock.getpeername()[:2]
            result = dict(response)
            result.update(
                {
                    "sender_ip": local_ip,
                    "sender_port": local_port,
                    "receiver_ip": remote_ip,
                    "receiver_port": remote_port,
                }
            )
        self._log_activity("info", f"通信检测成功：{host}:{port}")
        return result

    def send_file(
        self,
        job: SendJob,
        host: str,
        port: int,
        access_code: str = "",
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        cancel = cancel_event or threading.Event()
        source = Path(job.source_path)
        sender_info = self._identity()
        base_record: dict[str, Any] = {
            "transfer_id": job.transfer_id,
            "file_name": source.name,
            "relative_path": job.relative_path,
            "file_size_bytes": job.size,
            "bytes_transferred": 0,
            "sender_hostname": sender_info["hostname"],
            "sender_mac": sender_info["mac"],
            "receiver_ip": host,
            "receiver_port": port,
        }
        bytes_sent = 0
        started_at = ""
        completed_at = ""
        started_perf = 0.0
        sender_ip = ""
        sender_port: int | str = ""
        receiver_info: dict[str, Any] = {}
        try:
            port = validate_port(port)
            base_record["receiver_port"] = port
            if cancel.is_set():
                raise TransferCancelled("发送已取消")
            initial_stat = source.stat()
            if not stat.S_ISREG(initial_stat.st_mode):
                raise FileNotFoundError(f"不是可发送的文件：{source}")
            job.size = initial_stat.st_size
            base_record["file_size_bytes"] = initial_stat.st_size
            offer = {
                "file_name": source.name,
                "relative_path": job.relative_path,
                "size": initial_stat.st_size,
                "modified_ns": initial_stat.st_mtime_ns,
            }
            self._log_activity("info", f"开始发送：{job.relative_path}（{initial_stat.st_size} 字节）")
            self._emit_progress(
                progress_callback,
                {"transfer_id": job.transfer_id, "bytes": 0, "total": initial_stat.st_size, "rate": 0.0},
            )
            if cancel.is_set():
                raise TransferCancelled("用户取消了发送")
            # ExitStack keeps multiple context managers compatible with Python 3.8.
            with ExitStack() as stack:
                sock = stack.enter_context(
                    socket.create_connection((host, port), timeout=DEFAULT_CONNECT_TIMEOUT)
                )
                stack.enter_context(_cancel_socket_on_request(sock, cancel))
                configure_tcp_socket(sock)
                sock.settimeout(DEFAULT_IO_TIMEOUT)
                sender_ip, sender_port = sock.getsockname()[:2]
                remote_ip, remote_port = sock.getpeername()[:2]
                base_record.update(
                    {
                        "sender_ip": sender_ip,
                        "sender_port": sender_port,
                        "receiver_ip": remote_ip,
                        "receiver_port": remote_port,
                    }
                )
                ready = client_handshake(
                    sock,
                    action="transfer",
                    session_id=job.transfer_id,
                    sender_identity=sender_info,
                    access_code=access_code,
                    offer=offer,
                )
                if ready.get("type") != "ready":
                    raise ProtocolError("接收端未准备好接收文件", "BAD_SEQUENCE")
                receiver_info = ready.get("receiver") if isinstance(ready.get("receiver"), dict) else {}
                base_record.update(
                    {
                        "receiver_hostname": receiver_info.get("hostname", ""),
                        "receiver_mac": receiver_info.get("mac", ""),
                    }
                )

                started_at = utc_now_iso()
                started_perf = time.perf_counter()
                last_progress = started_perf
                hasher = hashlib.sha256()
                with source.open("rb", buffering=0) as handle:
                    if _file_signature(os.fstat(handle.fileno())) != _file_signature(initial_stat):
                        raise ProtocolError("源文件在发送前发生变化", "SOURCE_CHANGED")
                    remaining = initial_stat.st_size
                    while remaining:
                        if cancel.is_set():
                            raise TransferCancelled("用户取消了发送")
                        # The receiver expects exactly the offered byte count before the finish frame.
                        chunk = handle.read(min(CHUNK_SIZE, remaining))
                        if not chunk:
                            raise ProtocolError("源文件在发送过程中被截断", "SOURCE_CHANGED")
                        sock.sendall(chunk)
                        hasher.update(chunk)
                        bytes_sent += len(chunk)
                        remaining -= len(chunk)
                        now = time.perf_counter()
                        if progress_callback and (now - last_progress >= 0.2 or bytes_sent == initial_stat.st_size):
                            elapsed = max(now - started_perf, 0.000001)
                            self._emit_progress(
                                progress_callback,
                                {
                                    "transfer_id": job.transfer_id,
                                    "bytes": bytes_sent,
                                    "total": initial_stat.st_size,
                                    "rate": bytes_sent / elapsed,
                                }
                            )
                            last_progress = now
                    source_changed = _file_signature(os.fstat(handle.fileno())) != _file_signature(initial_stat)
                    try:
                        source_changed |= _file_signature(source.stat()) != _file_signature(initial_stat)
                    except OSError:
                        source_changed = True

                if cancel.is_set():
                    raise TransferCancelled("用户取消了发送")
                completed_at = utc_now_iso()
                send_duration = max(time.perf_counter() - started_perf, 0.000001)
                digest = hasher.hexdigest()
                send_frame(
                    sock,
                    {
                        "type": "finish",
                        "sha256": digest,
                        "bytes_sent": bytes_sent,
                        "source_changed": source_changed,
                        "send_started_at_utc": started_at,
                        "send_completed_at_utc": completed_at,
                        "send_duration_seconds": round(send_duration, 6),
                        "send_rate_bytes_per_second": round(bytes_sent / send_duration, 3),
                    },
                )
                if source_changed:
                    raise ProtocolError("源文件在发送过程中发生变化，已取消保存", "SOURCE_CHANGED")
                result = recv_frame(sock)
                raise_for_error(result)
                if result.get("type") != "complete":
                    raise ProtocolError("接收端未返回完成确认", "BAD_SEQUENCE")
                if result.get("transfer_id") != job.transfer_id:
                    raise ProtocolError("接收端确认的会话编号不匹配", "INVALID_SESSION")
                if result.get("sha256") != digest:
                    raise ProtocolError("接收端确认的 SHA-256 不匹配", "CHECKSUM_MISMATCH")
                received = result.get("bytes_received")
                if type(received) is not int or received != bytes_sent:
                    raise ProtocolError("接收端确认的字节数不匹配", "SIZE_MISMATCH")

            record = {
                **base_record,
                "status": "success",
                "message": "发送并校验成功",
                "bytes_transferred": bytes_sent,
                "sha256": result.get("sha256", digest),
                "send_started_at_utc": started_at,
                "send_completed_at_utc": completed_at,
                "send_duration_seconds": round(send_duration, 6),
                "send_rate_bytes_per_second": round(bytes_sent / send_duration, 3),
                "receive_started_at_utc": result.get("receive_started_at_utc", ""),
                "receive_completed_at_utc": result.get("receive_completed_at_utc", ""),
                "receive_duration_seconds": result.get("receive_duration_seconds", ""),
                "receive_rate_bytes_per_second": result.get("receive_rate_bytes_per_second", ""),
                "saved_path": result.get("saved_relative_path", ""),
            }
            write_transfer_record(self.transfers, self.activity, record)
            self._log_activity("info", f"发送完成：{job.relative_path}")
            self._emit_progress(
                progress_callback,
                {
                    "transfer_id": job.transfer_id,
                    "bytes": bytes_sent,
                    "total": initial_stat.st_size,
                    "rate": bytes_sent / send_duration,
                    "complete": True,
                },
            )
            return {**result, "log_record": record}
        except Exception as exc:
            error = TransferCancelled("用户取消了发送") if cancel.is_set() and isinstance(exc, OSError) else exc
            if not completed_at and started_at:
                completed_at = utc_now_iso()
            duration = max(time.perf_counter() - started_perf, 0.0) if started_perf else 0.0
            error_code = getattr(error, "code", "CANCELLED" if isinstance(error, TransferCancelled) else type(error).__name__)
            record = {
                **base_record,
                "status": "cancelled" if isinstance(error, TransferCancelled) else "failed",
                "error_code": error_code,
                "message": str(error),
                "bytes_transferred": bytes_sent,
                "sender_ip": sender_ip,
                "sender_port": sender_port,
                "receiver_hostname": receiver_info.get("hostname", ""),
                "receiver_mac": receiver_info.get("mac", ""),
                "send_started_at_utc": started_at,
                "send_completed_at_utc": completed_at,
                "send_duration_seconds": round(duration, 6) if duration else "",
                "send_rate_bytes_per_second": round(bytes_sent / duration, 3) if duration else "",
            }
            write_transfer_record(self.transfers, self.activity, record)
            self._log_activity("error", f"发送失败：{job.relative_path}；{error}")
            if error is not exc:
                raise error from exc
            raise
