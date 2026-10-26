from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge.logging_store import ActivityLogger, TransferLogger


def _write_concurrent_records(index: int, count: int, workers: int) -> None:
    root = Path(os.environ["LANBRIDGE_DATA_DIR"])
    transfers = TransferLogger("concurrent")
    activity = ActivityLogger("concurrent")
    handler = activity._logger.handlers[0]
    handler.maxBytes = 512
    handler.backupCount = 128
    (root / f"ready-{index}").touch()
    deadline = time.monotonic() + 10
    while len(list(root.glob("ready-*"))) != workers:
        if time.monotonic() >= deadline:
            raise RuntimeError("other logging processes did not start")
        time.sleep(0.01)
    try:
        for item in range(count):
            name = f"文件-{index}-{item}"
            transfers.write({"file_name": name, "transfer_id": name})
            activity.info(name)
    finally:
        activity.close()


class LoggingStoreTests(unittest.TestCase):
    def test_csv_cells_cannot_be_interpreted_as_spreadsheet_formulas(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"LANBRIDGE_DATA_DIR": directory}):
            logger = TransferLogger("receiver")
            normalized = logger.write({"file_name": "=2+2", "message": "+cmd|' /C calc'!A0"})
            csv_path = next((Path(directory) / "logs").glob("receiver-transfers-*.csv"))
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                row = next(csv.DictReader(handle))

        self.assertEqual("=2+2", normalized["file_name"])
        self.assertEqual("'=2+2", row["file_name"])
        self.assertEqual("'+cmd|' /C calc'!A0", row["message"])

    def test_multiple_processes_preserve_csv_jsonl_and_rotated_activity_records(self) -> None:
        workers, count = 4, 24
        with tempfile.TemporaryDirectory() as directory:
            env = os.environ.copy()
            env["LANBRIDGE_DATA_DIR"] = directory
            env["PYTHONIOENCODING"] = "utf-8"
            processes = []
            try:
                for index in range(workers):
                    script = (
                        "from tests.test_logging_store import _write_concurrent_records; "
                        f"_write_concurrent_records({index}, {count}, {workers})"
                    )
                    processes.append(subprocess.Popen(
                        [sys.executable, "-c", script],
                        cwd=str(Path(__file__).resolve().parents[1]), env=env,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    ))
                for process in processes:
                    _stdout, stderr = process.communicate(timeout=20)
                    self.assertEqual(process.returncode, 0, stderr.decode("utf-8", errors="replace"))
                    self.assertEqual(stderr, b"")
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.communicate(timeout=5)
            logs = Path(directory) / "logs"
            csv_path = next(logs.glob("concurrent-transfers-*.csv"))
            with csv_path.open(encoding="utf-8-sig", newline="") as handle:
                csv_records = list(csv.DictReader(handle))
            jsonl_path = next(logs.glob("concurrent-transfers-*.jsonl"))
            json_records = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
            expected = {f"文件-{index}-{item}" for index in range(workers) for item in range(count)}
            self.assertEqual(len(csv_records), workers * count)
            self.assertEqual(len(json_records), workers * count)
            self.assertEqual({record["file_name"] for record in csv_records}, expected)
            self.assertEqual({record["file_name"] for record in json_records}, expected)
            activity_lines = []
            for path in logs.glob("concurrent.log*"):
                if path.suffix != ".lock":
                    activity_lines.extend(path.read_text(encoding="utf-8").splitlines())
            self.assertEqual(len(activity_lines), workers * count)
            self.assertEqual({line.split(" | ")[-1] for line in activity_lines}, expected)


if __name__ == "__main__":
    unittest.main()
