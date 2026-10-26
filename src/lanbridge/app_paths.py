from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .constants import APP_NAME_EN


MAX_SETTINGS_BYTES = 1024 * 1024


@dataclass(frozen=True)
class AppPaths:
    root: Path
    settings: Path
    logs: Path


def get_app_paths() -> AppPaths:
    override = os.environ.get("LANBRIDGE_DATA_DIR")
    if override:
        root = Path(override).expanduser().resolve()
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        root = base / APP_NAME_EN
    settings = root / "settings"
    logs = root / "logs"
    settings.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    return AppPaths(root=root, settings=settings, logs=logs)


def load_settings(role: str, defaults: dict[str, Any]) -> dict[str, Any]:
    path = get_app_paths().settings / f"{role}.json"
    result = dict(defaults)
    try:
        if path.stat().st_size > MAX_SETTINGS_BYTES:
            return result
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            if not defaults:
                result.update(loaded)
            else:
                for key, default in defaults.items():
                    value = loaded.get(key, default)
                    if isinstance(default, bool):
                        compatible = isinstance(value, bool)
                    elif isinstance(default, int):
                        compatible = isinstance(value, int) and not isinstance(value, bool)
                    else:
                        compatible = isinstance(value, type(default))
                    if compatible:
                        result[key] = value
    except (OSError, ValueError, UnicodeError, RecursionError):
        pass
    return result


def save_settings(role: str, values: dict[str, Any]) -> Path:
    path = get_app_paths().settings / f"{role}.json"
    payload = json.dumps(values, ensure_ascii=False, indent=2, sort_keys=True)
    # Separate processes/windows must not share the same temporary filename.
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{role}-", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(5):
            try:
                os.replace(temp_path, path)
                break
            except PermissionError as exc:
                # Windows can briefly deny replacement while another instance
                # replaces the same file. Real permission errors still surface.
                if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 4:
                    raise
                time.sleep(0.01 * 2**attempt)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return path
