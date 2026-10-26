from __future__ import annotations

import sys
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge import receiver_app, sender_app
from lanbridge.models import SendJob
from lanbridge.ui_common import append_log, make_log_text


class UiRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self._destroy_root)
        for module in (sender_app, receiver_app):
            for name, value in (
                ("ActivityLogger", Mock()),
                ("TransferLogger", Mock()),
                ("get_local_ipv4_addresses", Mock(return_value=["127.0.0.1"])),
                ("local_identity", Mock(return_value={"hostname": "test", "mac": "00:00:00:00:00:00"})),
                ("save_settings", Mock()),
                ("messagebox", Mock()),
                ("load_settings", lambda _role, defaults: {**defaults, "auto_start": False}),
            ):
                patcher = patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)

    def _destroy_root(self) -> None:
        try:
            self.root.update_idletasks()
            for callback in self.root.tk.call("after", "info"):
                self.root.after_cancel(callback)
            self.root.destroy()
        except tk.TclError:
            pass

    def _job(self, name: str = "file.txt") -> SendJob:
        path = Path(self.directory.name) / name
        path.write_bytes(b"a" * 10)
        return SendJob(path, name, 10)

    @staticmethod
    def _poll(app: sender_app.SenderApp | receiver_app.ReceiverApp) -> None:
        app.root.after_cancel(app._poll_after_id)
        app._poll_events()

    def test_folder_scan_blocks_queue_mutation_and_send_until_complete(self) -> None:
        app = sender_app.SenderApp(self.root)
        job = self._job()
        app._insert_job(job)
        with patch.object(sender_app.filedialog, "askdirectory", return_value=self.directory.name), patch.object(sender_app.threading, "Thread"):
            app._add_folder()
            self.assertTrue(app.scanning)
            self.assertTrue(app.send_button.instate(["disabled"]))
            self.assertTrue(all(control.instate(["disabled"]) for control in app._queue_controls))
            app._start_send()
            app._clear_jobs()
            self.assertFalse(app.sending)
            self.assertIn(job.transfer_id, app.jobs)
        app.events.put({"kind": "folder_scanned", "jobs": [self._job("second.txt")], "errors": []})
        self._poll(app)
        self.assertFalse(app.scanning)
        self.assertEqual(len(app.jobs), 2)
        self.assertTrue(app.send_button.instate(["!disabled"]))

    def test_probe_and_discovery_cannot_overlap_a_send(self) -> None:
        app = sender_app.SenderApp(self.root)
        app._insert_job(self._job())
        with patch.object(sender_app.threading, "Thread") as thread:
            app._probe()
            self.assertTrue(app.probing)
            self.assertTrue(app.send_button.instate(["disabled"]))
            self.assertTrue(app.host_entry.instate(["disabled"]))
            app._discover()
            app._start_send()
            self.assertEqual(thread.call_count, 1)
            self.assertFalse(app.sending)
            app.events.put({"kind": "probe_error", "message": "timeout"})
            self._poll(app)
            self.assertFalse(app.probing)
            app._start_send()
            self.assertTrue(app.sending)
            self.assertTrue(app.discover_button.instate(["disabled"]))
            app._probe()
            app._discover()
            self.assertEqual(thread.call_count, 2)

    def test_retry_resets_sender_progress_and_refreshes_actual_file_size(self) -> None:
        app = sender_app.SenderApp(self.root)
        job = self._job()
        app._insert_job(job)
        app._update_tree_row(job.transfer_id, status="失败", bytes_value=8, total=10, rate=100)
        app.events.put({"kind": "job_state", "transfer_id": job.transfer_id, "status": "发送中"})
        self._poll(app)
        self.assertEqual(app.job_bytes[job.transfer_id], 0)
        self.assertEqual(app.tree.item(job.transfer_id, "values")[4], "0.0%")
        job.size = 20
        app.events.put({"kind": "job_state", "transfer_id": job.transfer_id, "status": "成功"})
        self._poll(app)
        values = app.tree.item(job.transfer_id, "values")
        self.assertEqual(values[2], "20 B")
        self.assertEqual(values[4], "100.0%")

    def test_changing_connection_clears_stale_success(self) -> None:
        app = sender_app.SenderApp(self.root)
        app.connection_status.configure(text="通信正常", style="Success.TLabel")
        app.host_var.set("192.0.2.1")
        self.assertNotIn("通信正常", app.connection_status.cget("text"))

    def test_scan_preserves_remembered_address_and_opens_selection_even_for_one_host(self) -> None:
        app = sender_app.SenderApp(self.root)
        app.host_var.set("192.168.1.80")
        device = {"ip": "192.168.1.90", "hostname": "office", "port": 53319, "is_receiver": True}
        with patch.object(sender_app, "DeviceDialog") as dialog:
            app.events.put({"kind": "discovery_result", "devices": [device]})
            self._poll(app)
        self.assertEqual("192.168.1.80", app.host_var.get())
        dialog.assert_called_once()
        self.assertTrue(any("office" in item for item in app.host_entry.cget("values")))

    def test_selecting_device_fills_port_and_automatically_probes(self) -> None:
        app = sender_app.SenderApp(self.root)
        with patch.object(app, "_probe") as probe:
            app._set_device({"ip": "192.168.1.90", "port": 53319, "hostname": "office"})
        self.assertEqual("192.168.1.90", app.host_var.get())
        self.assertEqual("53319", app.port_var.get())
        probe.assert_called_once()

    def test_protected_device_prompts_for_code_without_sending_known_wrong_auth(self) -> None:
        app = sender_app.SenderApp(self.root)
        with patch.object(app, "_probe") as probe:
            app._set_device({"ip": "192.168.1.90", "port": 53319, "requires_code": True})
        probe.assert_not_called()
        self.assertIn("访问码", app.connection_status.cget("text"))

    def test_stale_probe_cannot_validate_changed_address(self) -> None:
        app = sender_app.SenderApp(self.root)
        revision = app._connection_revision
        app.host_var.set("192.168.1.90")
        app.events.put({"kind": "probe_success", "revision": revision, "result": {}})
        self._poll(app)
        self.assertNotIn("通信正常", app.connection_status.cget("text"))

    def test_history_dropdown_restores_port_alongside_ip(self) -> None:
        app = sender_app.SenderApp(self.root)
        sender_app.remember_endpoint(app.settings, "192.168.1.90", 54444)
        item = sender_app.recent_endpoints(app.settings)[0]
        app.host_var.set(app._recent_label(item))
        with patch.object(app, "_probe") as probe:
            app._choose_dropdown()
        self.assertEqual("192.168.1.90", app.host_var.get())
        self.assertEqual("54444", app.port_var.get())
        probe.assert_called_once()

    def test_dropdown_distinguishes_receivers_on_different_ports(self) -> None:
        app = sender_app.SenderApp(self.root)
        first = {"ip": "192.168.1.90", "port": 53317, "hostname": "office"}
        second = {**first, "port": 53318}
        app.devices = [first, second]
        self.assertNotEqual(app._device_label(first), app._device_label(second))
        app.host_var.set(app._device_label(second))
        with patch.object(app, "_probe"):
            app._choose_dropdown()
        self.assertEqual("53318", app.port_var.get())

    def test_duplicate_index_is_updated_when_jobs_are_removed(self) -> None:
        app = sender_app.SenderApp(self.root)
        job = self._job()
        self.assertTrue(app._insert_job(job))
        self.assertFalse(app._insert_job(self._job()))
        app.tree.selection_set(job.transfer_id)
        app._remove_selected()
        self.assertTrue(app._insert_job(self._job()))
        self.assertEqual(len(app.jobs), 1)

    def test_scan_reports_unreadable_directory(self) -> None:
        app = sender_app.SenderApp(self.root)

        def fail_walk(_folder: Path, **kwargs: object) -> list[object]:
            kwargs["onerror"](PermissionError("denied"))
            return []

        with patch.object(sender_app.os, "walk", side_effect=fail_walk):
            app._scan_folder(Path(self.directory.name))
        event = app.events.get_nowait()
        self.assertEqual(event["kind"], "folder_scanned")
        self.assertEqual(event["errors"], ["denied"])

    def test_retry_resets_receiver_state_and_session_summary(self) -> None:
        app = receiver_app.ReceiverApp(self.root)
        started = {"kind": "transfer_started", "transfer_id": "retry", "file_name": "file.txt", "size": 10}
        app.events.put(started)
        app.events.put({"kind": "transfer_progress", "transfer_id": "retry", "bytes": 9, "total": 10})
        app.events.put({"kind": "transfer_error", "transfer_id": "retry"})
        self._poll(app)
        self.assertEqual(app.rows["retry"], "failed")
        app.events.put(started)
        self._poll(app)
        self.assertEqual(app.rows["retry"], "active")
        self.assertEqual(app.tree.item("retry", "values")[4:7], ("接收中", "0%", "--"))
        self.assertIn("进行中 1", app.session_summary.get())

    def test_settings_failure_does_not_orphan_running_receiver(self) -> None:
        app = receiver_app.ReceiverApp(self.root)
        server = Mock()
        server.start.return_value = 54321
        with patch.object(receiver_app, "ReceiverServer", return_value=server), patch.object(receiver_app, "save_settings", side_effect=PermissionError("read-only")):
            app._start_server()
        self.assertIs(app.server, server)
        self.assertTrue(app.start_button.instate(["disabled"]))
        self.assertTrue(app.stop_button.instate(["!disabled"]))
        self.assertIn("正在监听", app.server_status.cget("text"))
        server.stop.assert_not_called()

    def test_stop_runs_off_ui_thread_and_blocks_restart_until_finished(self) -> None:
        app = receiver_app.ReceiverApp(self.root)
        stopping = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        server = Mock()

        def stop() -> None:
            stopping.set()
            release.wait(3)

        server.stop.side_effect = stop
        app.server = server
        app._stop_server()
        self.assertTrue(stopping.wait(1))
        self.assertTrue(app.stopping)
        self.assertTrue(app.start_button.instate(["disabled"]))
        self.assertIs(app.server, server)
        with patch.object(receiver_app, "ReceiverServer") as constructor:
            app._start_server()
            constructor.assert_not_called()
        self.root.update()
        release.set()
        deadline = time.monotonic() + 2
        while app.stopping and time.monotonic() < deadline:
            self._poll(app)
            time.sleep(0.01)
        self.assertFalse(app.stopping)
        self.assertIsNone(app.server)
        self.assertTrue(app.start_button.instate(["!disabled"]))

    def test_stop_failure_keeps_server_reference_and_allows_retry(self) -> None:
        app = receiver_app.ReceiverApp(self.root)
        server = Mock()
        server.stop.side_effect = OSError()
        app.server = server
        app._stop_server()
        deadline = time.monotonic() + 2
        while app.events.empty() and time.monotonic() < deadline:
            time.sleep(0.01)
        self._poll(app)
        self.assertIs(app.server, server)
        self.assertFalse(app.stopping)
        self.assertTrue(app.start_button.instate(["disabled"]))
        self.assertTrue(app.stop_button.instate(["!disabled"]))
        self.assertIn("停止失败", app.server_status.cget("text"))
        self.assertIn("OSError", app.server_status.cget("text"))

    def test_receiver_history_is_bounded_without_removing_active_rows(self) -> None:
        app = receiver_app.ReceiverApp(self.root)
        with patch.object(receiver_app, "MAX_SESSION_ROWS", 2):
            for transfer_id in ("one", "two", "three"):
                app.events.put({"kind": "transfer_started", "transfer_id": transfer_id, "file_name": transfer_id, "size": 1})
                app.events.put({"kind": "transfer_complete", "transfer_id": transfer_id})
            self._poll(app)
        self.assertEqual({"two", "three"}, set(app.rows))
        self.assertFalse(app.tree.exists("one"))

    def test_corrupt_access_key_cannot_silently_enable_unprotected_receiver(self) -> None:
        with patch.object(receiver_app, "load_settings", side_effect=lambda _role, defaults: {**defaults, "access_key_hash": "bad key", "auto_start": True}):
            app = receiver_app.ReceiverApp(self.root)
        self.assertTrue(app.access_key_error)
        with patch.object(receiver_app, "ReceiverServer") as constructor:
            app._start_server()
            constructor.assert_not_called()
        app._save_settings()
        self.assertEqual(app.settings["access_key_hash"], "bad key")
        app._clear_access_code()
        self.assertFalse(app.access_key_error)
        self.assertEqual(app.settings["access_key_hash"], "")

    def test_sender_closes_even_when_settings_cannot_be_saved(self) -> None:
        app = sender_app.SenderApp(self.root)
        self.root.update_idletasks()
        with patch.object(sender_app, "save_settings", side_effect=PermissionError("read-only")):
            app._close_now()
        self.assertTrue(app.closed)
        self.assertTrue(app.cancel_event.is_set())

    def test_log_view_bounds_memory_and_keeps_recent_entries(self) -> None:
        widget = make_log_text(self.root)
        append_log(widget, "12:00:00", "INFO", "old\n" * 2100)
        append_log(widget, "12:00:01", "INFO", "latest")
        lines = widget.get("1.0", "end-1c").splitlines()
        self.assertLessEqual(len(lines), 2000)
        self.assertTrue(lines[-1].endswith("latest"))
        self.assertEqual(widget.cget("state"), "disabled")


if __name__ == "__main__":
    unittest.main()
