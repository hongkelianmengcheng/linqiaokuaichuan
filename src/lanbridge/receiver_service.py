from __future__ import annotations

import hashlib
import os
import shutil
import socket
import tempfile
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from .constants import (
    APP_VERSION,
    CHUNK_SIZE,
    DEFAULT_IO_TIMEOUT,
    MAX_CONCURRENT_RECEIVERS,
    PROTOCOL_VERSION,
    ROLE_RECEIVER,
)
from .discovery import DiscoveryResponder
from .logging_store import ActivityLogger, TransferLogger, write_transfer_record
from .protocol import (
    ProtocolError,
    new_nonce,
    recv_frame,
    send_error,
    send_frame,
    verify_auth_digest,
)
from .utils import (
    collision_candidate,
    configure_tcp_socket,
    filesystem_name,
    local_identity,
    safe_destination,
    utc_now_iso,
    validate_port,
)

EventCallback = Callable


class ReceiverServer:
    def __init__(
        self,
        download_directory: Path,
        *,
        access_key: bytes = b"",
        event_callback: EventCallback | None = None,
        activity_logger: ActivityLogger | None = None,
        transfer_logger: TransferLogger | None = None,
        max_connections: int = MAX_CONCURRENT_RECEIVERS,
    ) -> None:
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or max_connections < 1
        ):
            raise ValueError("最大并发接收数必须是正整数")
        self.download_directory = Path(download_directory).expanduser()
        self.access_key = access_key
        self.event_callback = event_callback
        self.activity = activity_logger or ActivityLogger(ROLE_RECEIVER)
        self.transfers = transfer_logger or TransferLogger(ROLE_RECEIVER)
        self.max_connections = max_connections
        self.port = 0

        self._stop_event = threading.Event()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._discovery: DiscoveryResponder | None = None
        self._semaphore = threading.BoundedSemaphore(self.max_connections)
        self._connections: set[socket.socket] = set()
        self._workers: set[threading.Thread] = set()
        self._connection_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._commit_lock = threading.RLock()
        self._reservation_lock = threading.Lock()
        self._reserved_paths: set[str] = set()
        self._auth_lock = threading.Lock()
        self._auth_failures: dict[str, list[float]] = defaultdict(list)

    @property
    def is_running(self) -> bool:
        return bool(self._accept_thread and self._accept_thread.is_alive() and self._listener)

    def _identity(self) -> dict[str, Any]:
        return {**local_identity(), "app_version": APP_VERSION, "protocol_version": PROTOCOL_VERSION}

    def _emit(self, payload: dict[str, Any]) -> None:
        if self.event_callback:
            try:
                self.event_callback(payload)
            except Exception as exc:
                self._log_activity("error", f"界面事件回调失败：{exc}")

    def _log_activity(self, level: str, message: str) -> None:
        # Logging and UI callbacks must not change the outcome of a transfer.
        try:
            getattr(self.activity, level)(message)
        except Exception:
            pass

    def start(self, port: int, *, auto_port: bool = True) -> int:
        with self._lifecycle_lock:
            return self._start_locked(port, auto_port=auto_port)

    def _start_locked(self, port: int, *, auto_port: bool) -> int:
        port = validate_port(port, allow_zero=True)
        if self.is_running:
            return self.port
        if self._listener is not None or self._accept_thread is not None:
            self._stop_locked()
        self.download_directory.mkdir(parents=True, exist_ok=True)
        attempts = [0] if port == 0 else list(range(port, min(65535, port + (20 if auto_port else 0)) + 1))
        listener: socket.socket | None = None
        last_error: OSError | None = None
        for candidate in attempts:
            trial = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                    trial.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                else:
                    trial.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                trial.bind(("0.0.0.0", candidate))
                trial.listen(self.max_connections * 2)
                trial.settimeout(0.5)
                listener = trial
                break
            except OSError as exc:
                last_error = exc
                trial.close()
        if listener is None:
            raise OSError(f"无法监听端口 {port}：{last_error}")

        self._listener = listener
        self.port = int(listener.getsockname()[1])
        # A restarted server must not revive workers from the previous run.
        self._stop_event = threading.Event()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            args=(listener, self._stop_event),
            name="lanbridge-accept",
            daemon=True,
        )
        try:
            self._accept_thread.start()
        except Exception:
            listener.close()
            self._listener = None
            self._accept_thread = None
            self.port = 0
            raise
        self._discovery = DiscoveryResponder(
            lambda: {
                **self._identity(),
                "port": self.port,
                "requires_code": bool(self.access_key),
            }
        )
        try:
            self._discovery.start()
        except Exception as exc:
            self._log_activity("warning", f"局域网发现服务未启动：{exc}；仍可手动输入 IP 连接")
            self._discovery = None
        self._log_activity("info", f"接收服务已启动，监听端口 {self.port}")
        self._emit({"kind": "server_started", "port": self.port})
        return self.port

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        with self._commit_lock:
            self._stop_event.set()
        if self._discovery:
            try:
                self._discovery.stop()
            except Exception as exc:
                self._log_activity("warning", f"局域网发现服务停止失败：{exc}")
            self._discovery = None
        listener, self._listener = self._listener, None
        if listener:
            try:
                listener.close()
            except OSError:
                pass
        with self._connection_lock:
            connections = list(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if self._accept_thread and self._accept_thread is not threading.current_thread():
            self._accept_thread.join(timeout=2.0)
        with self._connection_lock:
            workers = list(self._workers)
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=2.0)
        self._accept_thread = None
        self._log_activity("info", "接收服务已停止")
        self._emit({"kind": "server_stopped"})

    def _accept_loop(self, listener: socket.socket, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            try:
                connection, address = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._semaphore.acquire(blocking=False):
                try:
                    connection.settimeout(0.5)
                    send_error(connection, "BUSY", "接收端当前任务过多，请稍后重试")
                finally:
                    connection.close()
                continue
            worker = threading.Thread(
                target=self._worker_entry,
                args=(connection, address, stop_event),
                name=f"lanbridge-receive-{address[0]}",
                daemon=True,
            )
            start_error: Exception | None = None
            with self._connection_lock:
                if stop_event.is_set():
                    connection.close()
                    self._semaphore.release()
                    break
                self._connections.add(connection)
                self._workers.add(worker)
                # stop() must never observe a registered but unstarted thread.
                try:
                    worker.start()
                except Exception as exc:
                    self._connections.discard(connection)
                    self._workers.discard(worker)
                    start_error = exc
            if start_error is not None:
                try:
                    connection.close()
                except OSError:
                    pass
                self._semaphore.release()
                self._log_activity("error", f"无法启动接收任务：{start_error}")

    def _worker_entry(
        self, connection: socket.socket, address: tuple[str, int], stop_event: threading.Event
    ) -> None:
        try:
            self._handle_connection(connection, address, stop_event)
        finally:
            with self._connection_lock:
                self._connections.discard(connection)
                self._workers.discard(threading.current_thread())
            try:
                connection.close()
            except OSError:
                pass
            self._semaphore.release()

    def _auth_limited(self, ip: str) -> bool:
        now = time.monotonic()
        with self._auth_lock:
            recent = [value for value in self._auth_failures.get(ip, ()) if now - value < 60.0]
            if recent:
                self._auth_failures[ip] = recent
            else:
                self._auth_failures.pop(ip, None)
            return len(recent) >= 5

    def _record_auth_failure(self, ip: str) -> None:
        with self._auth_lock:
            if len(self._auth_failures) >= 1024:
                cutoff = time.monotonic() - 60.0
                stale = [
                    key
                    for key, values in self._auth_failures.items()
                    if not values or values[-1] < cutoff
                ]
                for key in stale:
                    self._auth_failures.pop(key, None)
            self._auth_failures[ip].append(time.monotonic())

    def _reserve_destination(self, requested: Path) -> Path:
        with self._reservation_lock:
            index = 0
            while True:
                candidate = collision_candidate(requested, index)
                key = os.path.normcase(str(candidate))
                if not os.path.lexists(candidate) and key not in self._reserved_paths:
                    self._reserved_paths.add(key)
                    return candidate
                index += 1

    def _release_destination(self, path: Path | None) -> None:
        if path is None:
            return
        with self._reservation_lock:
            self._reserved_paths.discard(os.path.normcase(str(path)))

    @staticmethod
    def _limited_text(value: Any, maximum: int = 256) -> str:
        text = str(value or "")
        return text[:maximum]

    def _commit_file(self, part_path: Path, destination: Path) -> None:
        # Publish without replacing a file created after the name was reserved.
        if os.name == "nt":
            os.rename(part_path, destination)
        else:
            os.link(part_path, destination)
            try:
                part_path.unlink()
            except OSError as exc:
                self._log_activity("warning", f"文件已保存，但临时文件未能清理：{exc}")

    def _handle_connection(
        self,
        connection: socket.socket,
        address: tuple[str, int],
        stop_event: threading.Event | None = None,
    ) -> None:
        stop_event = stop_event if stop_event is not None else self._stop_event
        remote_ip, remote_port = address[0], int(address[1])
        local_ip, local_port = "", 0
        destination: Path | None = None
        part_path: Path | None = None
        offer: dict[str, Any] = {}
        sender: dict[str, Any] = {}
        transfer_id = ""
        bytes_received = 0
        receive_started_at = ""
        receive_completed_at = ""
        receive_started_perf = 0.0
        action = ""
        try:
            configure_tcp_socket(connection)
            connection.settimeout(DEFAULT_IO_TIMEOUT)
            local_ip, local_port = connection.getsockname()[:2]
            hello = recv_frame(connection)
            if hello.get("type") != "hello":
                raise ProtocolError("首条消息不是握手请求", "BAD_SEQUENCE")
            action = self._limited_text(hello.get("action"), 32)
            if action not in {"probe", "transfer"}:
                raise ProtocolError("不支持的操作类型", "UNSUPPORTED_ACTION")
            session_id = hello.get("session_id")
            if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
                raise ProtocolError("会话编号无效", "INVALID_SESSION")
            transfer_id = session_id
            sender = hello.get("sender") if isinstance(hello.get("sender"), dict) else {}
            offer = hello.get("offer") if isinstance(hello.get("offer"), dict) else {}

            if self._auth_limited(remote_ip):
                send_error(connection, "RATE_LIMITED", "访问码错误次数过多，请稍后再试")
                return
            nonce = new_nonce()
            receiver_info = self._identity()
            send_frame(
                connection,
                {
                    "type": "challenge",
                    "nonce": nonce.hex(),
                    "requires_code": bool(self.access_key),
                    "receiver": receiver_info,
                },
            )
            auth = recv_frame(connection)
            if auth.get("type") != "auth":
                raise ProtocolError("身份验证消息顺序错误", "BAD_SEQUENCE")
            if not verify_auth_digest(self.access_key, nonce, hello, str(auth.get("digest") or "")):
                self._record_auth_failure(remote_ip)
                send_error(connection, "AUTH_FAILED", "访问码不正确")
                self._log_activity("warning", f"拒绝来自 {remote_ip} 的错误访问码")
                return

            if action == "probe":
                send_frame(
                    connection,
                    {
                        "type": "pong",
                        "receiver": receiver_info,
                        "server_time_utc": utc_now_iso(),
                    },
                )
                return

            relative_path = offer.get("relative_path")
            if not isinstance(relative_path, str) or not relative_path or len(relative_path) > 2048:
                raise ProtocolError("文件相对路径无效", "INVALID_PATH")
            file_size = offer.get("size")
            if type(file_size) is not int:
                raise ProtocolError("文件大小必须是整数", "INVALID_SIZE")
            if not 0 <= file_size <= (2**64 - 1):
                raise ProtocolError("文件大小超出协议范围", "INVALID_SIZE")
            try:
                requested = safe_destination(self.download_directory, relative_path)
                if requested == self.download_directory.resolve():
                    raise ValueError("相对路径必须包含文件名")
            except ValueError as exc:
                raise ProtocolError(str(exc), "INVALID_PATH") from exc
            destination = self._reserve_destination(requested)
            destination.parent.mkdir(parents=True, exist_ok=True)

            if filesystem_name(destination.parent) in {"FAT", "FAT32"} and file_size > 4 * 1024**3 - 1:
                raise ProtocolError("接收盘为 FAT32，无法保存超过 4 GB 的单个文件", "FILESYSTEM_LIMIT")
            free_space = shutil.disk_usage(destination.parent).free
            if file_size > free_space:
                raise ProtocolError("接收磁盘剩余空间不足", "NO_SPACE")

            send_frame(
                connection,
                {
                    "type": "ready",
                    "receiver": receiver_info,
                    "saved_relative_path": str(destination.relative_to(self.download_directory.resolve())),
                },
            )
            receive_started_at = utc_now_iso()
            receive_started_perf = time.perf_counter()
            last_progress = receive_started_perf
            hasher = hashlib.sha256()
            self._log_activity("info", f"开始接收：{relative_path}，来自 {remote_ip}:{remote_port}")
            self._emit(
                {
                    "kind": "transfer_started",
                    "transfer_id": transfer_id,
                    "file_name": Path(relative_path).name,
                    "relative_path": relative_path,
                    "size": file_size,
                    "sender_ip": remote_ip,
                }
            )

            with tempfile.NamedTemporaryFile(
                mode="wb", buffering=0, prefix=".lanbridge-", suffix=".part",
                dir=destination.parent, delete=False,
            ) as handle:
                part_path = Path(handle.name)
                remaining = file_size
                while remaining:
                    if stop_event.is_set():
                        raise ProtocolError("接收服务已停止", "SERVER_STOPPED")
                    chunk = connection.recv(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        raise ProtocolError("发送端在文件传完前断开连接", "CONNECTION_CLOSED")
                    # Unbuffered file writes may accept only part of a chunk.
                    view = memoryview(chunk)
                    while view:
                        written = handle.write(view)
                        if not written:
                            raise OSError("接收文件写入失败")
                        view = view[written:]
                    hasher.update(chunk)
                    bytes_received += len(chunk)
                    remaining -= len(chunk)
                    now = time.perf_counter()
                    if now - last_progress >= 0.2 or remaining == 0:
                        elapsed = max(now - receive_started_perf, 0.000001)
                        self._emit(
                            {
                                "kind": "transfer_progress",
                                "transfer_id": transfer_id,
                                "bytes": bytes_received,
                                "total": file_size,
                                "rate": bytes_received / elapsed,
                            }
                        )
                        last_progress = now
                handle.flush()
                os.fsync(handle.fileno())

            finish = recv_frame(connection)
            if finish.get("type") != "finish":
                raise ProtocolError("发送端未发送完成校验信息", "BAD_SEQUENCE")
            if finish.get("source_changed"):
                raise ProtocolError("源文件在发送过程中发生变化，已拒绝保存", "SOURCE_CHANGED")
            digest = hasher.hexdigest()
            if str(finish.get("sha256") or "").lower() != digest:
                raise ProtocolError("SHA-256 校验失败，文件已丢弃", "CHECKSUM_MISMATCH")
            if type(finish.get("bytes_sent")) is not int or finish["bytes_sent"] != bytes_received:
                raise ProtocolError("发送和接收字节数不一致", "SIZE_MISMATCH")

            with self._commit_lock:
                if stop_event.is_set():
                    raise ProtocolError("接收服务已停止", "SERVER_STOPPED")
                self._commit_file(part_path, destination)
            part_path = None
            receive_completed_at = utc_now_iso()
            receive_duration = max(time.perf_counter() - receive_started_perf, 0.000001)
            saved_relative_path = str(destination.relative_to(self.download_directory.resolve()))
            result = {
                "type": "complete",
                "transfer_id": transfer_id,
                "sha256": digest,
                "bytes_received": bytes_received,
                "saved_relative_path": saved_relative_path,
                "receive_started_at_utc": receive_started_at,
                "receive_completed_at_utc": receive_completed_at,
                "receive_duration_seconds": round(receive_duration, 6),
                "receive_rate_bytes_per_second": round(bytes_received / receive_duration, 3),
            }
            try:
                send_frame(connection, result)
            except OSError as exc:
                self._log_activity("warning", f"文件已保存，但完成确认未送达：{exc}")
            record = {
                "status": "success",
                "message": "接收并校验成功",
                "transfer_id": transfer_id,
                "file_name": Path(relative_path).name,
                "relative_path": relative_path,
                "file_size_bytes": file_size,
                "bytes_transferred": bytes_received,
                "sha256": digest,
                "sender_hostname": self._limited_text(sender.get("hostname")),
                "sender_mac": self._limited_text(sender.get("mac")),
                "sender_ip": remote_ip,
                "sender_port": remote_port,
                "receiver_hostname": receiver_info["hostname"],
                "receiver_mac": receiver_info["mac"],
                "receiver_ip": local_ip,
                "receiver_port": local_port,
                "send_started_at_utc": finish.get("send_started_at_utc", ""),
                "send_completed_at_utc": finish.get("send_completed_at_utc", ""),
                "send_duration_seconds": finish.get("send_duration_seconds", ""),
                "send_rate_bytes_per_second": finish.get("send_rate_bytes_per_second", ""),
                "receive_started_at_utc": receive_started_at,
                "receive_completed_at_utc": receive_completed_at,
                "receive_duration_seconds": round(receive_duration, 6),
                "receive_rate_bytes_per_second": round(bytes_received / receive_duration, 3),
                "saved_path": str(destination),
            }
            write_transfer_record(self.transfers, self.activity, record)
            self._log_activity("info", f"接收完成：{saved_relative_path}")
            self._emit({"kind": "transfer_complete", **result, "file_name": Path(relative_path).name})
        except Exception as exc:
            if part_path and part_path.exists():
                try:
                    part_path.unlink()
                except OSError:
                    pass
            code = getattr(exc, "code", type(exc).__name__)
            if isinstance(exc, socket.timeout):
                code = "TIMEOUT"
                message = "连接超时"
            else:
                message = str(exc) or type(exc).__name__
            send_error(connection, str(code), message)
            if action == "transfer":
                duration = max(time.perf_counter() - receive_started_perf, 0.0) if receive_started_perf else 0.0
                file_size_value = offer.get("size", "")
                write_transfer_record(
                    self.transfers,
                    self.activity,
                    {
                        "status": "failed",
                        "error_code": code,
                        "message": message,
                        "transfer_id": transfer_id,
                        "file_name": Path(str(offer.get("relative_path") or "未知文件")).name,
                        "relative_path": offer.get("relative_path", ""),
                        "file_size_bytes": file_size_value,
                        "bytes_transferred": bytes_received,
                        "sender_hostname": self._limited_text(sender.get("hostname")),
                        "sender_mac": self._limited_text(sender.get("mac")),
                        "sender_ip": remote_ip,
                        "sender_port": remote_port,
                        "receiver_hostname": self._identity()["hostname"],
                        "receiver_mac": self._identity()["mac"],
                        "receiver_ip": local_ip,
                        "receiver_port": local_port,
                        "receive_started_at_utc": receive_started_at,
                        "receive_completed_at_utc": receive_completed_at or (utc_now_iso() if receive_started_at else ""),
                        "receive_duration_seconds": round(duration, 6) if duration else "",
                        "receive_rate_bytes_per_second": round(bytes_received / duration, 3) if duration else "",
                    }
                )
                self._log_activity("error", f"接收失败：{offer.get('relative_path', '未知文件')}；{message}")
                self._emit(
                    {
                        "kind": "transfer_error",
                        "transfer_id": transfer_id,
                        "file_name": Path(str(offer.get("relative_path") or "未知文件")).name,
                        "error_code": code,
                        "message": message,
                    }
                )
        finally:
            self._release_destination(destination)
