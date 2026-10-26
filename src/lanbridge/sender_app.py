from __future__ import annotations

import argparse
import os
import queue
import socket
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .app_paths import get_app_paths, load_settings, save_settings
from .constants import APP_NAME, APP_VERSION, DEFAULT_TRANSFER_PORT, ROLE_SENDER
from .connection_memory import initial_endpoint, recent_endpoints, remember_endpoint
from .discovery import discover_lan_hosts
from .logging_store import ActivityLogger, TransferLogger
from .models import SendJob
from .protocol import AuthenticationError, ProtocolError
from .sender_service import SenderService, TransferCancelled
from .ui_common import (
    COLORS,
    ToolTip,
    append_log,
    apply_theme,
    ask_confirm_close,
    center_window,
    configure_windows_dpi,
    make_log_text,
    make_surface,
    open_directory,
    set_widget_state,
    set_window_icon,
)
from .utils import format_bytes, format_rate, get_local_ipv4_addresses, local_identity, validate_port


class DeviceDialog(tk.Toplevel):
    def __init__(self, parent: tk.Misc, devices: list[dict[str, Any]], on_select: Any) -> None:
        super().__init__(parent)
        self.title("邻桥快传 · 选择局域网主机")
        self.configure(background=COLORS["window"])
        self.resizable(True, True)
        self.minsize(760, 360)
        set_window_icon(self, "sender.ico")
        center_window(self, 860, 440)
        self.transient(parent)
        self.grab_set()
        self.on_select = on_select
        self.devices = devices

        surface = make_surface(self, padding=(18, 16))
        surface.pack(fill="both", expand=True, padx=16, pady=16)
        ttk.Label(surface, text="发现在线主机 · 双击即可连接", style="Section.TLabel").pack(anchor="w")
        ttk.Label(surface, text="双击一行自动填入 IP / 端口并检测通信；接收文件的电脑需先启动接收端。", style="Muted.TLabel").pack(anchor="w", pady=(3, 4))
        ttk.Label(surface, text="在线不代表能传文件；未响应扫描的设备仍可手动填 IP。关闭本窗口会保留当前地址。", style="Hint.TLabel").pack(anchor="w", pady=(0, 12))

        columns = ("device", "address", "status", "code")
        table = ttk.Frame(surface, style="Surface.TFrame")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("device", text="设备")
        self.tree.heading("address", text="地址")
        self.tree.heading("code", text="访问码")
        self.tree.heading("status", text="检测状态")
        self.tree.column("device", width=190, minwidth=120)
        self.tree.column("address", width=170, minwidth=140)
        self.tree.column("code", width=90, minwidth=80, anchor="center")
        self.tree.column("status", width=190, minwidth=150)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scrollbar.pack(side="right", fill="y")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        for index, device in enumerate(devices):
            self.tree.insert(
                "",
                "end",
                iid=str(index),
                values=(
                    device.get("hostname") or "未知设备",
                    f"{device.get('ip')}:{device.get('port')}",
                    device.get("status") or "主机在线，待检测",
                    ("需要" if device.get("requires_code") else "不需要") if device.get("is_receiver") else "待确认",
                ),
            )
        if devices:
            self.tree.selection_set("0")
            self.tree.focus("0")
        self.tree.bind("<Double-1>", self._double_click)
        self.tree.bind("<Return>", lambda _event: self._choose())
        self.bind("<Escape>", lambda _event: self.destroy())
        if not devices:
            ttk.Label(surface, text="暂未发现响应的主机。请确认同一局域网，并打开对方接收端后重试。", style="Warning.TLabel").pack(anchor="w", pady=(10, 0))

        controls = ttk.Frame(surface, style="Surface.TFrame")
        controls.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Button(controls, text="取消", style="Secondary.TButton", command=self.destroy).pack(side="right")
        ttk.Button(controls, text="使用所选设备", style="Accent.TButton", command=self._choose).pack(side="right", padx=(0, 8))
        table.pack(fill="both", expand=True)

    def _double_click(self, event: tk.Event) -> None:
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self._choose()

    def _choose(self) -> None:
        selected = self.tree.selection()
        if not selected:
            return
        device = self.devices[int(selected[0])]
        self.on_select(device)
        self.destroy()


class SenderApp:
    def __init__(self, root: tk.Tk, *, smoke_test: bool = False) -> None:
        self.root = root
        self.smoke_test = smoke_test
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.settings = load_settings(
            ROLE_SENDER,
            {
                "receiver_host": "",
                "receiver_port": DEFAULT_TRANSFER_PORT,
                "last_directory": str(Path.home()),
                "memory_initialized": False,
                "recent_endpoints": [],
            },
        )
        self.jobs: dict[str, SendJob] = {}
        self.job_status: dict[str, str] = {}
        self.job_bytes: dict[str, int] = {}
        self._job_ids_by_path: dict[tuple[str, str], str] = {}
        self.cancel_event = threading.Event()
        self.discovery_cancel = threading.Event()
        self.devices: list[dict[str, Any]] = []
        self.device_dialog: DeviceDialog | None = None
        self._connection_revision = 0
        self._startup_after_id = None
        self.sending = False
        self.scanning = False
        self.discovering = False
        self.probing = False
        self.closed = False

        self.activity = ActivityLogger(
            ROLE_SENDER,
            callback=lambda level, message: self.events.put({"kind": "log", "level": level, "message": message}),
        )
        self.service = SenderService(activity_logger=self.activity, transfer_logger=TransferLogger(ROLE_SENDER))

        self._build_window()
        self._poll_after_id = self.root.after(80, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.activity.info(f"{APP_NAME}发送端 {APP_VERSION} 已启动")
        if smoke_test:
            self.root.after(900, self._close_now)
        else:
            self._startup_after_id = self.root.after(600, self._startup_discovery)

    def _build_window(self) -> None:
        self.root.title(f"{APP_NAME} · 发送端 · {APP_VERSION}")
        self.root.minsize(1000, 680)
        center_window(self.root, 1160, 800)
        set_window_icon(self.root, "sender.ico")
        apply_theme(self.root)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(24, 17))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        title_row = ttk.Frame(header, style="Header.TFrame")
        title_row.grid(row=0, column=0, sticky="w")
        ttk.Label(title_row, text=APP_NAME, style="HeaderTitle.TLabel").pack(side="left")
        role_badge = ttk.Label(title_row, text="发送端 / SEND", style="Badge.TLabel")
        role_badge.pack(side="left", padx=(10, 0), pady=(4, 0))
        ttk.Label(header, text=f"LANBRIDGE / {APP_VERSION}     01 选择电脑   →   02 添加文件   →   03 开始发送", style="HeaderSubtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 0))
        identity = local_identity()
        addresses = get_local_ipv4_addresses()
        local_text = f"本机 / {identity['hostname']}\n{addresses[0] if addresses else '未获取 IP'}"
        ttk.Label(header, text=local_text, style="HeaderInfo.TLabel").grid(row=0, column=1, rowspan=2, sticky="e")
        tk.Frame(header, background=COLORS["accent"], height=2).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        connection = make_surface(self.root, padding=(18, 13))
        connection.grid(row=1, column=0, sticky="ew", padx=18, pady=(16, 8))
        connection.grid_columnconfigure(1, weight=1)
        ttk.Label(connection, text="01 / 连接接收电脑", style="Section.TLabel").grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 9))
        ttk.Label(connection, text="IP 地址", style="Surface.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 7))
        host, port, remembered = initial_endpoint(self.settings, addresses)
        self.host_var = tk.StringVar(value=host)
        self.host_entry = ttk.Combobox(connection, textvariable=self.host_var, width=25, height=10)
        self.host_entry.configure(values=tuple(dict.fromkeys([host] + [self._recent_label(item) for item in recent_endpoints(self.settings)])))
        self.host_entry.bind("<<ComboboxSelected>>", self._choose_dropdown)
        ToolTip(self.host_entry, "可手动输入 IP，也可从下拉列表选择在线电脑。最近使用的地址保留 7 天，启动扫描不会覆盖它。")
        self.host_entry.grid(row=1, column=1, sticky="ew", padx=(0, 12))
        ttk.Label(connection, text="端口", style="Surface.TLabel").grid(row=1, column=2, sticky="w", padx=(0, 7))
        self.port_var = tk.StringVar(value=str(port))
        self.port_entry = ttk.Spinbox(connection, textvariable=self.port_var, from_=1, to=65535, width=8)
        self.port_entry.grid(row=1, column=3, sticky="w", padx=(0, 12))
        ttk.Label(connection, text="访问码", style="Surface.TLabel").grid(row=1, column=4, sticky="w", padx=(0, 7))
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(connection, textvariable=self.code_var, show="*", width=13)
        self.code_entry.grid(row=1, column=5, sticky="w", padx=(0, 12))
        self.discover_button = ttk.Button(connection, text="扫描主机", style="Secondary.TButton", command=self._discover)
        self.discover_button.grid(row=1, column=6, padx=(0, 8))
        ToolTip(self.discover_button, "约 4 秒扫描在线主机和接收端，双击自动填写并检测。大网段仅探测附近 /24 与缓存邻居，最多 512 个地址；未发现时可手填 IP。")
        self.probe_button = ttk.Button(connection, text="✓ 检测通信", style="Accent.TButton", command=self._probe)
        self.probe_button.grid(row=1, column=7)
        ttk.Label(connection, text="下拉选择 / 手动输入；地址记忆 7 天", style="Hint.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(connection, text="与接收端一致", style="Hint.TLabel").grid(row=2, column=2, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(connection, text="对方未设置可留空", style="Hint.TLabel").grid(row=2, column=4, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(connection, text="扫描后双击选择 → 自动检测", style="Hint.TLabel").grid(row=2, column=6, columnspan=2, sticky="w", pady=(6, 0))
        initial_status = "已恢复最近使用的 IP · 尚未检测通信" if remembered else "已填入本机 IP 供自测 · 传给其他电脑请从扫描列表选择"
        self.connection_status = ttk.Label(connection, text=initial_status, style="Muted.TLabel", wraplength=980)
        self.connection_status.grid(row=3, column=0, columnspan=8, sticky="w", pady=(12, 0))
        ToolTip(self.port_entry, "通常不用修改；如果接收端自动换过端口，请以接收端显示的端口为准。")
        ToolTip(self.code_entry, "输入对方接收端设置的访问码；程序不会保存发送端访问码。")
        ToolTip(self.probe_button, "验证邻桥协议和访问码。左下方显示通信正常后即可发送。")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 8))
        self.task_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=(14, 12))
        self.log_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=(12, 12))
        self.notebook.add(self.task_tab, text="传输任务")
        self.notebook.add(self.log_tab, text="运行日志")
        self._build_task_tab()
        self._build_log_tab()

        footer = ttk.Frame(self.root, padding=(18, 0, 18, 14))
        footer.grid(row=3, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.summary_var = tk.StringVar(value="队列为空")
        ttk.Label(footer, textvariable=self.summary_var, style="WindowMuted.TLabel").grid(row=0, column=0, sticky="w")
        self.cancel_button = ttk.Button(footer, text="■ 取消", style="Danger.TButton", command=self._cancel_send, state="disabled")
        self.cancel_button.grid(row=0, column=1, padx=(8, 8))
        self.send_button = ttk.Button(footer, text="▶ 开始发送", style="Success.TButton", command=self._start_send)
        self.send_button.grid(row=0, column=2)
        ToolTip(self.send_button, "依次发送队列中尚未成功的文件；每个文件完成 SHA-256 校验后才显示成功。")
        ToolTip(self.cancel_button, "停止当前发送，已完成文件保留；未完成文件会在接收端清理。")

        self._mutable_controls = [
            self.host_entry,
            self.port_entry,
            self.code_entry,
            self.discover_button,
            self.probe_button,
        ]
        for variable in (self.host_var, self.port_var, self.code_var):
            variable.trace_add("write", self._connection_changed)

    def _connection_changed(self, *_args: str) -> None:
        self._connection_revision += 1
        self.connection_status.configure(text="连接信息已更改，请检测通信", style="Muted.TLabel")

    def _refresh_controls(self) -> None:
        connection_busy = self.sending or self.discovering or self.probing or self.closed
        queue_busy = self.sending or self.scanning or self.closed
        set_widget_state(self._mutable_controls, "disabled" if connection_busy else "normal")
        set_widget_state(self._queue_controls, "disabled" if queue_busy else "normal")
        self.send_button.configure(state="disabled" if connection_busy or queue_busy else "normal")
        self.cancel_button.configure(state="normal" if self.sending and not self.cancel_event.is_set() else "disabled")

    def _build_task_tab(self) -> None:
        self.task_tab.grid_columnconfigure(0, weight=1)
        self.task_tab.grid_rowconfigure(2, weight=1)
        toolbar = ttk.Frame(self.task_tab, style="Surface.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.add_files_button = ttk.Button(toolbar, text="＋ 添加文件", style="Accent.TButton", command=self._add_files)
        self.add_files_button.pack(side="left")
        self.add_folder_button = ttk.Button(toolbar, text="▣ 添加文件夹", style="Secondary.TButton", command=self._add_folder)
        self.add_folder_button.pack(side="left", padx=(8, 0))
        self.remove_button = ttk.Button(toolbar, text="✕ 移除所选", style="Secondary.TButton", command=self._remove_selected)
        self.remove_button.pack(side="left", padx=(8, 0))
        self.clear_button = ttk.Button(toolbar, text="清空队列", style="Link.TButton", command=self._clear_jobs)
        self.clear_button.pack(side="right")
        self._queue_controls = [self.add_files_button, self.add_folder_button, self.remove_button, self.clear_button]
        for widget, hint in (
            (self.add_files_button, "选择一个或多个文件，添加到下方待发送队列。"),
            (self.add_folder_button, "添加文件夹内的全部文件，保留子目录；空文件夹不传输。"),
            (self.remove_button, "先选中下方任务，再从队列移除；不会删除电脑上的原文件。"),
            (self.clear_button, "清空本次队列，不会删除源文件或已接收文件。"),
        ):
            ToolTip(widget, hint)
        ttk.Label(self.task_tab, text="02 / 添加文件或整个文件夹    ·    选中任务可移除    ·    03 / 右下角开始发送，失败的文件可再次发送", style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 10))

        columns = ("name", "relative", "size", "status", "progress", "speed")
        tree_frame = ttk.Frame(self.task_tab, style="Surface.TFrame")
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "name": "文件名",
            "relative": "发送路径",
            "size": "大小",
            "status": "状态",
            "progress": "进度",
            "speed": "速度",
        }
        for column, label in headings.items():
            self.tree.heading(column, text=label)
        self.tree.column("name", width=190, minwidth=130)
        self.tree.column("relative", width=280, minwidth=180)
        self.tree.column("size", width=95, minwidth=80, anchor="e", stretch=False)
        self.tree.column("status", width=88, minwidth=75, anchor="center", stretch=False)
        self.tree.column("progress", width=82, minwidth=70, anchor="center", stretch=False)
        self.tree.column("speed", width=105, minwidth=90, anchor="e", stretch=False)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(xscrollcommand=horizontal.set)
        horizontal.grid(row=1, column=0, sticky="ew")

    def _build_log_tab(self) -> None:
        self.log_tab.grid_columnconfigure(0, weight=1)
        self.log_tab.grid_rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self.log_tab, style="Surface.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="运行日志会自动保存，同时生成可用 Excel 打开的 CSV 传输记录。", style="Muted.TLabel").pack(side="left")
        ttk.Button(toolbar, text="打开日志目录", style="Link.TButton", command=lambda: open_directory(get_app_paths().logs)).pack(side="right")
        self.log_text = make_log_text(self.log_tab)
        self.log_text.grid(row=1, column=0, sticky="nsew")

    def _save_connection_settings(self) -> None:
        try:
            port = validate_port(self.port_var.get())
        except ValueError:
            return
        remember_endpoint(self.settings, self.host_var.get(), port)
        try:
            save_settings(ROLE_SENDER, self.settings)
        except OSError as exc:
            self.activity.warning(f"设置未能保存，下次启动可能恢复原设置：{exc}")

    def _endpoint(self) -> tuple[str, int, str]:
        host = self.host_var.get().strip()
        if not host:
            raise ValueError("请输入接收端 IP 地址")
        return host, validate_port(self.port_var.get()), self.code_var.get()

    def _add_files(self) -> None:
        if self.sending or self.scanning or self.closed:
            return
        paths = filedialog.askopenfilenames(title="选择要发送的文件", initialdir=self.settings.get("last_directory") or str(Path.home()))
        if not paths:
            return
        self.settings["last_directory"] = str(Path(paths[0]).parent)
        for raw_path in paths:
            path = Path(raw_path)
            try:
                self._insert_job(SendJob(path, path.name, path.stat().st_size))
            except OSError as exc:
                self.activity.error(f"无法添加 {path}：{exc}")
        self._save_connection_settings()

    def _add_folder(self) -> None:
        if self.sending or self.scanning or self.closed:
            return
        selected = filedialog.askdirectory(title="选择要发送的文件夹", initialdir=self.settings.get("last_directory") or str(Path.home()))
        if not selected:
            return
        folder = Path(selected)
        self.settings["last_directory"] = str(folder.parent)
        self.scanning = True
        self._refresh_controls()
        self.summary_var.set(f"正在扫描 {folder.name} …")
        threading.Thread(target=self._scan_folder, args=(folder,), name="lanbridge-folder-scan", daemon=True).start()

    def _scan_folder(self, folder: Path) -> None:
        jobs: list[SendJob] = []
        errors: list[str] = []
        try:
            for current, directories, files in os.walk(folder, followlinks=False, onerror=lambda exc: errors.append(str(exc))):
                if self.closed:
                    return
                directories.sort(key=str.casefold)
                files.sort(key=str.casefold)
                current_path = Path(current)
                for file_name in files:
                    path = current_path / file_name
                    try:
                        if path.is_file():
                            relative = str(Path(folder.name) / path.relative_to(folder))
                            jobs.append(SendJob(path, relative, path.stat().st_size))
                    except OSError as exc:
                        errors.append(f"{path}: {exc}")
        except OSError as exc:
            errors.append(str(exc))
        self.events.put({"kind": "folder_scanned", "jobs": jobs, "errors": errors, "folder": str(folder)})

    def _insert_job(self, job: SendJob, *, update_summary: bool = True) -> bool:
        source_key = (os.path.normcase(str(job.source_path.resolve())), job.relative_path)
        if source_key in self._job_ids_by_path:
            return False
        self._job_ids_by_path[source_key] = job.transfer_id
        self.jobs[job.transfer_id] = job
        self.job_status[job.transfer_id] = "待发送"
        self.job_bytes[job.transfer_id] = 0
        self.tree.insert(
            "",
            "end",
            iid=job.transfer_id,
            values=(job.file_name, job.relative_path, format_bytes(job.size), "待发送", "0%", "--"),
        )
        if update_summary:
            self._update_summary()
        return True

    def _remove_selected(self) -> None:
        if self.sending or self.scanning or self.closed:
            return
        for item in self.tree.selection():
            self.tree.delete(item)
            self.jobs.pop(item, None)
            self.job_status.pop(item, None)
            self.job_bytes.pop(item, None)
        self._job_ids_by_path = {key: value for key, value in self._job_ids_by_path.items() if value in self.jobs}
        self._update_summary()

    def _clear_jobs(self) -> None:
        if self.sending or self.scanning or self.closed:
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        self.jobs.clear()
        self.job_status.clear()
        self.job_bytes.clear()
        self._job_ids_by_path.clear()
        self._update_summary()

    def _update_summary(self) -> None:
        count = len(self.jobs)
        total = sum(job.size for job in self.jobs.values())
        completed = sum(1 for status in self.job_status.values() if status == "成功")
        if not count:
            self.summary_var.set("队列为空")
        else:
            self.summary_var.set(f"{count} 个文件 · {format_bytes(total)} · 已完成 {completed}/{count}")

    def _set_device(self, device: dict[str, Any]) -> None:
        if self.sending or self.probing or self.closed:
            return
        self.host_var.set(str(device.get("ip") or ""))
        self.port_var.set(str(device.get("port") or DEFAULT_TRANSFER_PORT))
        self._save_connection_settings()
        if device.get("requires_code") and not self.code_var.get().strip():
            self.connection_status.configure(text="已填写所选主机 · 对方设置了访问码，请输入后点击检测通信", style="Warning.TLabel")
            self.code_entry.focus_set()
        else:
            self._probe()

    def _choose_dropdown(self, _event: Any = None) -> None:
        selected = self.host_var.get()
        for device in self.devices:
            if selected == self._device_label(device):
                self._set_device(device)
                return
        for item in recent_endpoints(self.settings):
            if selected == self._recent_label(item):
                self._set_device({"ip": item["host"], "port": item["port"]})
                return
        self._probe()

    @staticmethod
    def _recent_label(item: dict[str, Any]) -> str:
        return f"{item['host']}:{item['port']}  |  最近使用"

    @staticmethod
    def _device_label(device: dict[str, Any]) -> str:
        return f"{device['ip']}:{device['port']}  |  {device.get('hostname') or '未知主机'}  |  {'接收端' if device.get('is_receiver') else '在线主机'}"

    def _startup_discovery(self) -> None:
        self._startup_after_id = None
        self._discover()

    def _discover(self) -> None:
        if self.sending or self.discovering or self.probing or self.closed:
            return
        if self.device_dialog is not None and self.device_dialog.winfo_exists():
            self.device_dialog.destroy()
        try:
            port = validate_port(self.port_var.get())
        except ValueError:
            port = DEFAULT_TRANSFER_PORT
        self.discovering = True
        self.discovery_cancel.clear()
        self._refresh_controls()
        self.connection_status.configure(text="正在扫描局域网…", style="Warning.TLabel")
        self.activity.info("开始扫描局域网在线主机和接收端")

        def worker() -> None:
            try:
                devices = discover_lan_hosts(port=port, timeout=4.0, cancel_event=self.discovery_cancel)
                self.events.put({"kind": "discovery_result", "devices": devices})
            except Exception as exc:
                self.events.put({"kind": "discovery_error", "message": str(exc)})

        threading.Thread(target=worker, name="lanbridge-discover", daemon=True).start()

    def _probe(self) -> None:
        if self.sending or self.discovering or self.probing or self.closed:
            return
        try:
            host, port, code = self._endpoint()
        except ValueError as exc:
            messagebox.showwarning("连接信息不完整", str(exc), parent=self.root)
            return
        self._save_connection_settings()
        self.probing = True
        revision = self._connection_revision
        self._refresh_controls()
        self.connection_status.configure(text="正在进行应用层通信检测…", style="Warning.TLabel")

        def worker() -> None:
            try:
                result = self.service.probe(host, port, code)
                self.events.put({"kind": "probe_success", "result": result, "revision": revision})
            except Exception as exc:
                self.events.put({"kind": "probe_error", "message": self._friendly_error(exc), "revision": revision})

        threading.Thread(target=worker, name="lanbridge-probe", daemon=True).start()

    def _start_send(self) -> None:
        if self.sending or self.scanning or self.discovering or self.probing or self.closed:
            return
        if not self.jobs:
            messagebox.showinfo("没有任务", "请先添加文件或文件夹。", parent=self.root)
            return
        try:
            host, port, code = self._endpoint()
        except ValueError as exc:
            messagebox.showwarning("连接信息不完整", str(exc), parent=self.root)
            return
        pending = [job for transfer_id, job in self.jobs.items() if self.job_status.get(transfer_id) != "成功"]
        if not pending:
            messagebox.showinfo("任务已完成", "队列中的文件均已发送成功。", parent=self.root)
            return
        self._save_connection_settings()
        self.cancel_event = threading.Event()
        self.sending = True
        self._refresh_controls()

        def worker() -> None:
            succeeded = 0
            failed = 0
            for job in pending:
                if self.cancel_event.is_set():
                    break
                self.events.put({"kind": "job_state", "transfer_id": job.transfer_id, "status": "发送中"})
                try:
                    self.service.send_file(
                        job,
                        host,
                        port,
                        code,
                        cancel_event=self.cancel_event,
                        progress_callback=lambda payload: self.events.put({"kind": "job_progress", **payload}),
                    )
                    succeeded += 1
                    self.events.put({"kind": "job_state", "transfer_id": job.transfer_id, "status": "成功"})
                except TransferCancelled:
                    self.events.put({"kind": "job_state", "transfer_id": job.transfer_id, "status": "已取消"})
                    break
                except Exception as exc:
                    failed += 1
                    self.events.put(
                        {
                            "kind": "job_state",
                            "transfer_id": job.transfer_id,
                            "status": "失败",
                            "message": self._friendly_error(exc),
                        }
                    )
            self.events.put(
                {
                    "kind": "batch_done",
                    "succeeded": succeeded,
                    "failed": failed,
                    "cancelled": self.cancel_event.is_set(),
                }
            )

        threading.Thread(target=worker, name="lanbridge-send-queue", daemon=True).start()

    def _cancel_send(self) -> None:
        if self.sending:
            self.cancel_event.set()
            self.cancel_button.configure(state="disabled")
            self.activity.warning("正在取消当前发送任务…")

    @staticmethod
    def _friendly_error(exc: Exception) -> str:
        if isinstance(exc, AuthenticationError):
            return "访问码错误"
        if isinstance(exc, socket.timeout):
            return "连接或传输超时"
        if isinstance(exc, ConnectionRefusedError):
            return "连接被拒绝；请确认接收端已启动且端口正确"
        if isinstance(exc, socket.gaierror):
            return "IP 地址或主机名无法解析"
        if isinstance(exc, ProtocolError):
            return str(exc)
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10060:
            return "连接超时；请检查防火墙和网络"
        if isinstance(exc, OSError) and getattr(exc, "winerror", None) == 10061:
            return "连接被拒绝；请确认接收端已启动"
        return str(exc) or type(exc).__name__

    def _update_tree_row(self, transfer_id: str, *, status: str | None = None, bytes_value: int | None = None, total: int | None = None, rate: float | None = None) -> None:
        if not self.tree.exists(transfer_id):
            return
        values = list(self.tree.item(transfer_id, "values"))
        if status is not None:
            values[3] = status
            self.job_status[transfer_id] = status
        if bytes_value is not None:
            self.job_bytes[transfer_id] = bytes_value
            if total is not None:
                percent = 100.0 if total == 0 and status == "成功" else (bytes_value * 100.0 / total if total else 0.0)
                values[4] = f"{percent:.1f}%"
        size_changed = total is not None and values[2] != format_bytes(total)
        if total is not None:
            values[2] = format_bytes(total)
        if rate is not None:
            values[5] = format_rate(rate)
        self.tree.item(transfer_id, values=values)
        self.tree.see(transfer_id)
        if status is not None or size_changed:
            self._update_summary()

    def _poll_events(self) -> None:
        if self.closed:
            return
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("kind")
            if kind == "log":
                append_log(self.log_text, datetime.now().strftime("%H:%M:%S"), str(event.get("level")), str(event.get("message")))
            elif kind == "folder_scanned":
                self.scanning = False
                added = 0
                for job in event.get("jobs", []):
                    added += self._insert_job(job, update_summary=False)
                self._refresh_controls()
                self._update_summary()
                errors = event.get("errors", [])
                if errors:
                    self.activity.warning(f"文件夹扫描完成，{len(errors)} 个文件无法读取")
                self.activity.info(f"已从文件夹添加 {added} 个文件")
                self._save_connection_settings()
            elif kind == "discovery_result":
                self.discovering = False
                self._refresh_controls()
                devices = event.get("devices", [])
                self.devices = devices
                recent = [self._recent_label(item) for item in recent_endpoints(self.settings)]
                self.host_entry.configure(values=tuple(dict.fromkeys([self.host_var.get()] + [self._device_label(device) for device in devices] + recent)))
                if not devices:
                    self.connection_status.configure(text="未发现响应的主机 · 可手动输入对方 IP 和端口后检测", style="Warning.TLabel")
                else:
                    receivers = sum(bool(item.get("is_receiver")) for item in devices)
                    self.connection_status.configure(text=f"发现 {len(devices)} 个在线地址 / {receivers} 个接收端 · 双击选择后自动检测；当前 IP 已保留", style="Muted.TLabel")
                self.device_dialog = DeviceDialog(self.root, devices, self._set_device)
                self.activity.info(f"扫描完成：发现 {len(devices)} 个在线地址")
            elif kind == "discovery_error":
                self.discovering = False
                self._refresh_controls()
                self.connection_status.configure(text=f"扫描失败：{event.get('message')}", style="Danger.TLabel")
            elif kind == "probe_success":
                self.probing = False
                self._refresh_controls()
                if event.get("revision", self._connection_revision) != self._connection_revision:
                    continue
                receiver = event.get("result", {}).get("receiver", {})
                if not isinstance(receiver, dict):
                    receiver = {}
                self._save_connection_settings()
                self.connection_status.configure(
                    text=f"通信正常 · {receiver.get('hostname', '接收端')} · {event['result'].get('receiver_ip')}:{event['result'].get('receiver_port')}",
                    style="Success.TLabel",
                )
            elif kind == "probe_error":
                self.probing = False
                self._refresh_controls()
                if event.get("revision", self._connection_revision) != self._connection_revision:
                    continue
                self.connection_status.configure(text=f"通信异常 · {event.get('message')}", style="Danger.TLabel")
            elif kind == "job_state":
                transfer_id = str(event.get("transfer_id"))
                status = str(event.get("status"))
                job = self.jobs.get(transfer_id)
                self._update_tree_row(
                    transfer_id,
                    status=status,
                    bytes_value=job.size if job and status == "成功" else (0 if status == "发送中" else None),
                    total=job.size if job else None,
                    rate=0.0 if status in {"发送中", "失败", "已取消"} else None,
                )
                if event.get("message"):
                    self.activity.error(f"{job.relative_path if job else transfer_id}：{event['message']}")
            elif kind == "job_progress":
                self._update_tree_row(
                    str(event.get("transfer_id")),
                    bytes_value=int(event.get("bytes", 0)),
                    total=int(event.get("total", 0)),
                    rate=float(event.get("rate", 0.0)),
                )
            elif kind == "batch_done":
                self.sending = False
                self._refresh_controls()
                if event.get("succeeded", 0):
                    self._save_connection_settings()
                if event.get("cancelled"):
                    self.activity.warning("发送队列已取消")
                else:
                    self.activity.info(f"发送队列结束：成功 {event.get('succeeded', 0)}，失败 {event.get('failed', 0)}")
                self._update_summary()
        self._poll_after_id = self.root.after(80, self._poll_events)

    def _on_close(self) -> None:
        if self.sending:
            ask_confirm_close(self.root, "当前仍有文件正在发送。", self._close_now)
        else:
            self._close_now()

    def _close_now(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.cancel_event.set()
        self.discovery_cancel.set()
        try:
            self._save_connection_settings()
        finally:
            if self._startup_after_id is not None:
                self.root.after_cancel(self._startup_after_id)
            self.root.after_cancel(self._poll_after_id)
            self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-test", action="store_true")
    args, _unknown = parser.parse_known_args()
    configure_windows_dpi()
    root = tk.Tk()
    SenderApp(root, smoke_test=args.smoke_test)
    root.mainloop()


if __name__ == "__main__":
    main()
