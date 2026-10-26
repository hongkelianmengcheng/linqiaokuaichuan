from __future__ import annotations

import sys
import tkinter as tk
import unittest
from pathlib import Path
from tkinter import ttk
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lanbridge import receiver_app, sender_app
from lanbridge.ui_common import apply_theme


class WindowLayoutTests(unittest.TestCase):
    """Check actual Tk geometry so critical controls cannot be clipped on resize."""

    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display is unavailable: {exc}")
        self.root.attributes("-alpha", 0)
        self.addCleanup(self._destroy_root)
        for module in (sender_app, receiver_app):
            for name, value in (
                ("ActivityLogger", Mock()),
                ("TransferLogger", Mock()),
                ("get_local_ipv4_addresses", Mock(return_value=["192.0.2.10"])),
                ("local_identity", Mock(return_value={"hostname": "测试电脑", "mac": "00:00:00:00:00:00"})),
                ("save_settings", Mock()),
                ("load_settings", lambda _role, defaults: {**defaults, "auto_start": False}),
            ):
                patcher = patch.object(module, name, value)
                patcher.start()
                self.addCleanup(patcher.stop)

    def _destroy_root(self) -> None:
        try:
            for callback in self.root.tk.call("after", "info"):
                self.root.after_cancel(callback)
            self.root.destroy()
        except tk.TclError:
            pass

    def _layout(self, window: tk.Misc, width: int, height: int) -> None:
        # Prevent scans or automatic shutdowns; these tests exercise geometry only.
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        window.geometry(f"{width}x{height}+0+0")
        window.update_idletasks()

    def _assert_visible(self, widget: tk.Misc, window: tk.Misc) -> None:
        self.assertGreater(widget.winfo_width(), 1, str(widget))
        self.assertGreater(widget.winfo_height(), 1, str(widget))
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        parent = widget.master
        while parent is not None:
            x = widget.winfo_rootx() - parent.winfo_rootx()
            y = widget.winfo_rooty() - parent.winfo_rooty()
            self.assertGreaterEqual(x, 0, str(widget))
            self.assertGreaterEqual(y, 0, str(widget))
            self.assertLessEqual(x + widget.winfo_width(), parent.winfo_width(), str(widget))
            self.assertLessEqual(y + widget.winfo_height(), parent.winfo_height(), str(widget))
            if parent is window:
                break
            parent = parent.master

    def test_sender_actions_remain_visible_at_minimum_size(self) -> None:
        app = sender_app.SenderApp(self.root)
        self._layout(self.root, 1000, 680)
        for name in (
            "host_entry", "port_entry", "code_entry", "discover_button", "probe_button",
            "add_files_button", "add_folder_button", "remove_button", "clear_button",
            "send_button", "cancel_button", "connection_status",
        ):
            with self.subTest(control=name):
                self._assert_visible(getattr(app, name), self.root)

    def test_receiver_actions_remain_visible_at_minimum_size(self) -> None:
        app = receiver_app.ReceiverApp(self.root)
        self._layout(self.root, 960, 650)
        for name in (
            "folder_entry", "browse_button", "port_entry", "auto_port_check", "auto_start_check",
            "code_entry", "clear_code_button", "start_button", "stop_button",
            "open_folder_button", "open_logs_button",
        ):
            with self.subTest(control=name):
                self._assert_visible(getattr(app, name), self.root)

    def test_device_dialog_selection_buttons_survive_small_window(self) -> None:
        self._check_dialog([{"hostname": "接收电脑", "ip": "192.0.2.20", "port": 53317}])

    def test_empty_device_dialog_keeps_cancel_visible(self) -> None:
        self._check_dialog([])

    def _check_dialog(self, devices: list[dict[str, object]]) -> None:
        apply_theme(self.root)
        self.root.update_idletasks()
        dialog = sender_app.DeviceDialog(self.root, devices, Mock())
        dialog.attributes("-alpha", 0)
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        self._layout(dialog, 760, 360)
        descendants = list(dialog.winfo_children())
        buttons = []
        while descendants:
            child = descendants.pop()
            descendants.extend(child.winfo_children())
            if isinstance(child, ttk.Button):
                buttons.append(child)
        self.assertEqual(len(buttons), 2)
        for button in buttons:
            self._assert_visible(button, dialog)
        self.assertGreater(dialog.tree.winfo_height(), 60)


if __name__ == "__main__":
    unittest.main()
