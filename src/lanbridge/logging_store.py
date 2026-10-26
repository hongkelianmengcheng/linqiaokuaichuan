from __future__ import annotations

import csv
import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Iterator

from .app_paths import get_app_paths
from .constants import APP_VERSION, PROTOCOL_VERSION
from .utils import utc_now_iso

TRANSFER_COLUMNS = (
    "event_time_utc",
    "role",
    "status",
    "error_code",
    "message",
    "transfer_id",
    "file_name",
    "relative_path",
    "file_size_bytes",
    "bytes_transferred",
    "sha256",
    "sender_hostname",
    "sender_mac",
    "sender_ip",
    "sender_port",
    "receiver_hostname",
    "receiver_mac",
    "receiver_ip",
    "receiver_port",
    "send_started_at_utc",
    "send_completed_at_utc",
    "send_duration_seconds",
    "send_rate_bytes_per_second",
    "receive_started_at_utc",
    "receive_completed_at_utc",
    "receive_duration_seconds",
    "receive_rate_bytes_per_second",
    "saved_path",
    "app_version",
    "protocol_version",
)


_LOG_LOCKS: dict[str, threading.Lock] = {}
_LOG_LOCKS_GUARD = threading.Lock()


@contextmanager
def _shared_log_lock(path: Path) -> Iterator[None]:
    """Serialize log writes across windows, threads and application processes."""
    key = str(path.resolve())
    with _LOG_LOCKS_GUARD:
        thread_lock = _LOG_LOCKS.setdefault(key, threading.Lock())
    with thread_lock, path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            deadline = time.monotonic() + 5.0
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _SharedRotatingFileHandler(RotatingFileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        # Windows cannot rename a log while another client's handler keeps it
        # open. Open only inside the shared lock and close after each record.
        try:
            with _shared_log_lock(Path(self.baseFilename + ".lock")):
                try:
                    super().emit(record)
                finally:
                    if self.stream is not None:
                        self.stream.close()
                        self.stream = None
        except Exception:
            self.handleError(record)


def _csv_safe(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


class TransferLogger:
    def __init__(self, role: str) -> None:
        self.role = role

    @property
    def log_directory(self) -> Path:
        return get_app_paths().logs

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        normalized = {column: record.get(column, "") for column in TRANSFER_COLUMNS}
        normalized["event_time_utc"] = normalized["event_time_utc"] or utc_now_iso()
        normalized["role"] = normalized["role"] or self.role
        normalized["app_version"] = normalized["app_version"] or APP_VERSION
        normalized["protocol_version"] = normalized["protocol_version"] or PROTOCOL_VERSION
        date_stamp = datetime.now().strftime("%Y-%m-%d")
        directory = self.log_directory
        jsonl_path = directory / f"{self.role}-transfers-{date_stamp}.jsonl"
        csv_path = directory / f"{self.role}-transfers-{date_stamp}.csv"
        with _shared_log_lock(directory / f".{self.role}-transfers.lock"):
            with jsonl_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
            csv_exists = csv_path.exists() and csv_path.stat().st_size > 0
            with csv_path.open("a", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=TRANSFER_COLUMNS, extrasaction="ignore")
                if not csv_exists:
                    writer.writeheader()
                writer.writerow({column: _csv_safe(value) for column, value in normalized.items()})
                handle.flush()
        return normalized


class ActivityLogger:
    def __init__(self, role: str, callback: Callable[[str, str], None] | None = None) -> None:
        self.role = role
        self.callback = callback
        self.path = get_app_paths().logs / f"{role}.log"
        self._logger = logging.getLogger(f"lanbridge.{role}.{id(self)}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        handler = _SharedRotatingFileHandler(
            self.path,
            maxBytes=2 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        self._logger.addHandler(handler)

    def _emit(self, level: str, message: str) -> None:
        getattr(self._logger, level)(message)
        if self.callback:
            try:
                self.callback(level.upper(), message)
            except Exception:
                self._logger.exception("界面日志回调失败")

    def info(self, message: str) -> None:
        self._emit("info", message)

    def warning(self, message: str) -> None:
        self._emit("warning", message)

    def error(self, message: str) -> None:
        self._emit("error", message)

    def close(self) -> None:
        for handler in self._logger.handlers[:]:
            self._logger.removeHandler(handler)
            handler.close()


def write_transfer_record(
    transfers: TransferLogger, activity: ActivityLogger, record: dict[str, Any]
) -> None:
    """Report persistence errors without changing an already completed transfer."""
    try:
        transfers.write(record)
    except Exception as exc:
        try:
            activity.error(f"传输记录写入失败：{exc}")
        except Exception:
            pass
