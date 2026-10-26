from __future__ import annotations

import argparse
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .app_paths import get_app_paths, load_settings, save_settings
from .constants import APP_NAME, APP_VERSION, DEFAULT_TRANSFER_PORT, ROLE_RECEIVER
from .logging_store import ActivityLogger, TransferLogger
from .protocol import access_key_from_hex, access_key_to_hex, derive_access_key
from .receiver_service import ReceiverServer
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


MAX_SESSION_ROWS = 2000


class ReceiverApp:
    def __init__(self, root: tk.Tk, *, smoke_test: bool = False) -> None:
        self.root = root
        self.smoke_test = smoke_test
        default_folder = Path.home() / "Downloads" / "邻桥接收"
        self.settings = load_settings(
            ROLE_RECEIVER,
            {
                "download_directory": str(default_folder),
                "listen_port": DEFAULT_TRANSFER_PORT,
                "auto_port": True,
                "auto_start": True,
                "access_key_hash": "",
            },
        )
        self.access_key_error = False
        try:
            self.stored_access_key = access_key_from_hex(str(self.settings.get("access_key_hash") or ""))
        except ValueError:
            self.stored_access_key = b""
            self.access_key_error = True
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.server: ReceiverServer | None = None
        self.closed = False
        self.closing = False
        self.stopping = False
        self.rows: dict[str, str] = {}

        self.activity = ActivityLogger(
            ROLE_RECEIVER,
            callback=lambda level, message: self.events.put({"kind": "log", "level": level, "message": message}),
        )
        self.transfer_logger = TransferLogger(ROLE_RECEIVER)
        self._build_window()
        self._poll_after_id = self.root.after(80, self._poll_events)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.activity.info(f"{APP_NAME}接收端 {APP_VERSION} 已启动")

        if smoke_test:
            self.root.after(900, self._close_now)
        elif self.auto_start_var.get() and not self.access_key_error:
            self.root.after(350, self._start_server)

    def _build_window(self) -> None:
        self.root.title(f"{APP_NAME} · 接收端 · {APP_VERSION}")
        self.root.minsize(960, 650)
        center_window(self.root, 1140, 790)
        set_window_icon(self.root, "receiver.ico")
        apply_theme(self.root)
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(2, weight=1)

        header = ttk.Frame(self.root, style="Header.TFrame", padding=(24, 17))
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        title_row = ttk.Frame(header, style="Header.TFrame")
        title_row.grid(row=0, column=0, sticky="w")
        ttk.Label(title_row, text=APP_NAME, style="HeaderTitle.TLabel").pack(side="left")
        role_badge = ttk.Label(title_row, text="接收端 / RECEIVE", style="Badge.TLabel")
        role_badge.pack(side="left", padx=(10, 0), pady=(4, 0))
        ttk.Label(header, text="01 选择保存位置   →   02 启动接收   →   03 在发送端选择这台电脑", style="HeaderSubtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        identity = local_identity()
        self.header_info_var = tk.StringVar(value=f"{identity['hostname']}  ·  v{APP_VERSION}")
        ttk.Label(header, textvariable=self.header_info_var, style="HeaderInfo.TLabel", wraplength=350, justify="right").grid(row=0, column=1, sticky="e")
        ttk.Label(header, text="局域网直连  /  文件完整性校验", style="HeaderSubtitle.TLabel").grid(row=1, column=1, sticky="e", pady=(6, 0))
        tk.Frame(header, background=COLORS["accent"], height=2).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        settings_panel = make_surface(self.root, padding=(18, 13))
        settings_panel.grid(row=1, column=0, sticky="ew", padx=18, pady=(16, 8))
        settings_panel.grid_columnconfigure(1, weight=1)
        ttk.Label(settings_panel, text="01 / 接收配置", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(settings_panel, text="先启动接收，再从另一台电脑发送", style="Hint.TLabel").grid(row=0, column=2, sticky="e", pady=(0, 10))

        ttk.Label(settings_panel, text="保存目录", style="Surface.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 7))
        self.folder_var = tk.StringVar(value=str(self.settings.get("download_directory") or ""))
        self.folder_entry = ttk.Entry(settings_panel, textvariable=self.folder_var)
        self.folder_entry.grid(row=1, column=1, sticky="ew", padx=(0, 8))
        self.browse_button = ttk.Button(settings_panel, text="选择目录", style="Secondary.TButton", command=self._browse_folder)
        self.browse_button.grid(row=1, column=2, sticky="e")
        ttk.Label(settings_panel, text="收到的文件会保存在这里；同名文件自动重命名。监听期间需先停止接收，才能修改配置。", style="Hint.TLabel").grid(row=2, column=1, columnspan=2, sticky="w", pady=(4, 10))

        options = ttk.Frame(settings_panel, style="Surface.TFrame")
        options.grid(row=3, column=0, columnspan=3, sticky="ew")
        options.grid_columnconfigure(0, weight=1, uniform="options")
        options.grid_columnconfigure(2, weight=1, uniform="options")
        network = ttk.Frame(options, style="Surface.TFrame")
        network.grid(row=0, column=0, sticky="new", padx=(0, 18))
        network.grid_columnconfigure(1, weight=1)
        ttk.Separator(options, orient="vertical").grid(row=0, column=1, sticky="ns", padx=(0, 18))
        access = ttk.Frame(options, style="Surface.TFrame")
        access.grid(row=0, column=2, sticky="new")
        access.grid_columnconfigure(1, weight=1)

        ttk.Label(network, text="通信端口", style="Surface.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.port_var = tk.StringVar(value=str(self.settings.get("listen_port", DEFAULT_TRANSFER_PORT)))
        self.port_entry = ttk.Spinbox(network, textvariable=self.port_var, from_=1, to=65535, width=8)
        self.port_entry.grid(row=0, column=1, sticky="w")
        ttk.Label(network, text="通常保留默认值", style="Hint.TLabel").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.auto_port_var = tk.BooleanVar(value=bool(self.settings.get("auto_port", True)))
        self.auto_port_check = ttk.Checkbutton(network, text="占用时自动换端口", variable=self.auto_port_var, command=self._save_settings)
        self.auto_port_check.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(network, text="端口冲突时继续接收", style="Hint.TLabel").grid(row=1, column=2, sticky="w", padx=(12, 0), pady=(10, 0))
        self.auto_start_var = tk.BooleanVar(value=bool(self.settings.get("auto_start", True)))
        self.auto_start_check = ttk.Checkbutton(network, text="下次启动时自动监听", variable=self.auto_start_var, command=self._save_settings)
        self.auto_start_check.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Label(network, text="下次打开即可接收", style="Hint.TLabel").grid(row=2, column=2, sticky="w", padx=(12, 0), pady=(8, 0))

        ttk.Label(access, text="访问码", style="Surface.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.code_var = tk.StringVar()
        self.code_entry = ttk.Entry(access, textvariable=self.code_var, show="*", width=16)
        self.code_entry.grid(row=0, column=1, sticky="ew")
        self.clear_code_button = ttk.Button(access, text="清除", style="Link.TButton", command=self._clear_access_code)
        self.clear_code_button.grid(row=0, column=2, sticky="e", padx=(8, 0))
        self.code_state_var = tk.StringVar()
        self._update_code_state()
        self.code_state_label = ttk.Label(access, textvariable=self.code_state_var, style="Hint.TLabel", wraplength=400)
        self.code_state_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(9, 0))
        ttk.Label(access, text="可选：两端填写相同访问码，启动接收后生效。", style="Hint.TLabel").grid(row=2, column=0, columnspan=3, sticky="w", pady=(8, 0))

        controls = ttk.Frame(settings_panel, style="Surface.TFrame")
        controls.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        controls.grid_columnconfigure(0, weight=1)
        self.server_status = ttk.Label(controls, text="● 服务未启动 · 点击“启动接收”等待发送端", style="Warning.TLabel", wraplength=600, justify="left")
        self.server_status.grid(row=0, column=0, sticky="w")
        self.start_button = ttk.Button(controls, text="▶  启动接收", style="Accent.TButton", command=self._start_server)
        self.start_button.grid(row=0, column=1, padx=(12, 8))
        self.stop_button = ttk.Button(controls, text="■  停止接收", style="Danger.TButton", command=self._stop_server, state="disabled")
        self.stop_button.grid(row=0, column=2)
        controls.bind("<Configure>", lambda event: self.server_status.configure(wraplength=max(180, event.width - self.start_button.winfo_reqwidth() - self.stop_button.winfo_reqwidth() - 32)))

        self._config_controls = [
            self.folder_entry,
            self.browse_button,
            self.port_entry,
            self.auto_port_check,
            self.code_entry,
            self.clear_code_button,
        ]

        self.notebook = ttk.Notebook(self.root)
        self.notebook.grid(row=2, column=0, sticky="nsew", padx=18, pady=(0, 8))
        self.records_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=(14, 12))
        self.log_tab = ttk.Frame(self.notebook, style="Surface.TFrame", padding=(12, 12))
        self.notebook.add(self.records_tab, text="02 / 接收记录")
        self.notebook.add(self.log_tab, text="运行日志 · 排查问题")
        self._build_records_tab()
        self._build_log_tab()

        footer = ttk.Frame(self.root, padding=(18, 0, 18, 14))
        footer.grid(row=3, column=0, sticky="ew")
        footer.grid_columnconfigure(0, weight=1)
        self.address_var = tk.StringVar(value="启动后，这里会显示发送端需要填写的 IP 和端口。")
        self.address_label = ttk.Label(footer, textvariable=self.address_var, style="WindowMuted.TLabel", wraplength=650, justify="left")
        self.address_label.grid(row=0, column=0, sticky="w")
        self.open_folder_button = ttk.Button(footer, text="打开接收目录", style="Secondary.TButton", command=self._open_receive_folder)
        self.open_folder_button.grid(row=0, column=1, padx=(8, 8))
        self.open_logs_button = ttk.Button(footer, text="打开日志目录", style="Secondary.TButton", command=lambda: open_directory(get_app_paths().logs))
        self.open_logs_button.grid(row=0, column=2)
        footer.bind("<Configure>", lambda event: self.address_label.configure(wraplength=max(250, event.width - 320)))
        for widget, hint in (
            (self.folder_entry, "文件接收位置。可直接输入完整路径，或点击右侧“选择目录”。"),
            (self.browse_button, "选择一个有写入权限的文件夹；后续收到的文件都会保存在这里。"),
            (self.port_entry, f"接收端监听端口，默认 {DEFAULT_TRANSFER_PORT}。发送端需要填写相同端口；自动发现会一起填写。"),
            (self.auto_port_check, "打勾：端口被其他程序占用时自动选择可用端口。实际端口会显示在下方。"),
            (self.auto_start_check, "打勾：下次打开接收端时自动启动监听。此选项不会设置 Windows 开机自启动。"),
            (self.code_entry, "可选访问码。设置后，发送端必须填写相同访问码。留空会继续使用已保存的访问码。"),
            (self.clear_code_button, "清除当前及已保存的访问码；下次启动监听后，同一局域网的设备可以直接连接。"),
            (self.start_button, "启动后保持此窗口打开，在另一台电脑的发送端选择本机并发送文件。"),
            (self.stop_button, "停止监听并中断进行中的接收。已完整接收的文件仍保留；停止后可以修改配置。"),
            (self.open_folder_button, "在文件资源管理器中查看已接收的文件。"),
            (self.open_logs_button, "查看本机保存的运行与传输日志，方便定位连接或传输问题。"),
        ):
            ToolTip(widget, hint)

    def _build_records_tab(self) -> None:
        self.records_tab.grid_columnconfigure(0, weight=1)
        self.records_tab.grid_rowconfigure(1, weight=1)
        header = ttk.Frame(self.records_tab, style="Surface.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="本次运行收到的文件", style="Section.TLabel").pack(side="left")
        self.session_summary = tk.StringVar(value="尚未接收文件")
        ttk.Label(header, textvariable=self.session_summary, style="Muted.TLabel").pack(side="right")

        tree_frame = ttk.Frame(self.records_tab, style="Surface.TFrame")
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.grid_columnconfigure(0, weight=1)
        tree_frame.grid_rowconfigure(0, weight=1)
        columns = ("time", "name", "size", "sender", "status", "progress", "speed", "path")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings", selectmode="browse")
        headings = {
            "time": "开始时间",
            "name": "文件名",
            "size": "大小",
            "sender": "发送端",
            "status": "状态",
            "progress": "进度",
            "speed": "速度",
            "path": "保存路径",
        }
        for column, text in headings.items():
            self.tree.heading(column, text=text)
        self.tree.column("time", width=105, minwidth=90, stretch=False)
        self.tree.column("name", width=180, minwidth=120)
        self.tree.column("size", width=90, minwidth=75, anchor="e", stretch=False)
        self.tree.column("sender", width=125, minwidth=110, stretch=False)
        self.tree.column("status", width=80, minwidth=70, anchor="center", stretch=False)
        self.tree.column("progress", width=75, minwidth=65, anchor="center", stretch=False)
        self.tree.column("speed", width=105, minwidth=90, anchor="e", stretch=False)
        self.tree.column("path", width=260, minwidth=160)
        scrollbar = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        ToolTip(self.tree, "接收进度会自动更新；显示“成功”即已完成完整性校验。底部滚动条可查看完整保存路径。")

    def _build_log_tab(self) -> None:
        self.log_tab.grid_columnconfigure(0, weight=1)
        self.log_tab.grid_rowconfigure(1, weight=1)
        toolbar = ttk.Frame(self.log_tab, style="Surface.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="日志自动保存到本机，不会上传到任何服务器。", style="Muted.TLabel").pack(side="left")
        logs_button = ttk.Button(toolbar, text="打开日志目录", style="Link.TButton", command=lambda: open_directory(get_app_paths().logs))
        logs_button.pack(side="right")
        ToolTip(logs_button, "打开本机日志文件夹，按时间查看完整的连接、传输与错误记录。")
        self.log_text = make_log_text(self.log_tab)
        self.log_text.grid(row=1, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self.log_tab, orient="vertical", command=self.log_text.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def _update_code_state(self) -> None:
        if self.access_key_error:
            self.code_state_var.set("已保存访问码损坏；请重新设置或清除")
            return
        self.code_state_var.set("已保存访问码；留空会继续使用" if self.stored_access_key else "未设置，局域网内设备可直接连接")

    def _browse_folder(self) -> None:
        selected = filedialog.askdirectory(title="选择文件接收目录", initialdir=self.folder_var.get() or str(Path.home()))
        if selected:
            self.folder_var.set(selected)
            self._save_settings()

    def _clear_access_code(self) -> None:
        if self.server or self.stopping or self.closing:
            messagebox.showinfo("请先停止接收", "停止接收服务后才能更改访问码。", parent=self.root)
            return
        self.stored_access_key = b""
        self.access_key_error = False
        self.code_var.set("")
        self._update_code_state()
        self._save_settings()
        self.activity.info("已清除接收访问码")

    def _save_settings(self) -> None:
        try:
            port = validate_port(self.port_var.get())
        except ValueError:
            port = DEFAULT_TRANSFER_PORT
        self.settings.update(
            {
                "download_directory": self.folder_var.get().strip(),
                "listen_port": port,
                "auto_port": bool(self.auto_port_var.get()),
                "auto_start": bool(self.auto_start_var.get()),
                "access_key_hash": self.settings.get("access_key_hash", "") if self.access_key_error else access_key_to_hex(self.stored_access_key),
            }
        )
        try:
            save_settings(ROLE_RECEIVER, self.settings)
        except OSError as exc:
            self.activity.warning(f"设置未能保存，下次启动可能恢复原设置：{exc}")

    def _start_server(self) -> None:
        if self.server or self.stopping or self.closing or self.closed:
            return
        server = None
        try:
            port = validate_port(self.port_var.get())
            folder_text = self.folder_var.get().strip()
            if not folder_text:
                raise ValueError("请选择文件接收目录")
            folder = Path(folder_text).expanduser()
            code = self.code_var.get()
            if self.access_key_error and not code:
                raise ValueError("已保存访问码损坏，请重新输入访问码或点击清除访问码")
            access_key = derive_access_key(code) if code else self.stored_access_key
            server = ReceiverServer(
                folder,
                access_key=access_key,
                event_callback=lambda payload: self.events.put(payload),
                activity_logger=self.activity,
                transfer_logger=self.transfer_logger,
            )
            actual_port = server.start(port, auto_port=bool(self.auto_port_var.get()))
            self.server = server
            self.stored_access_key = access_key
            self.access_key_error = False
            self.port_var.set(str(actual_port))
            self.code_var.set("")
            self._update_code_state()
            self._save_settings()
            set_widget_state(self._config_controls, "disabled")
            self.start_button.configure(state="disabled")
            self.stop_button.configure(state="normal")
            self.server_status.configure(text=f"正在监听 · TCP {actual_port}", style="Success.TLabel")
            addresses = get_local_ipv4_addresses()
            if addresses:
                self.address_var.set("发送端可连接：" + "  ·  ".join(f"{address}:{actual_port}" for address in addresses))
            else:
                self.address_var.set(f"正在监听 0.0.0.0:{actual_port}；未获取到局域网 IP")
        except Exception as exc:
            if server:
                try:
                    server.stop()
                except Exception as stop_error:
                    self.activity.error(f"清理启动失败的接收服务时发生错误：{stop_error}")
            self.server = None
            set_widget_state(self._config_controls, "normal")
            self.start_button.configure(state="normal")
            self.stop_button.configure(state="disabled")
            self.server_status.configure(text=f"启动失败 · {exc}", style="Danger.TLabel")
            self.activity.error(f"接收服务启动失败：{exc}")
            if not self.smoke_test:
                messagebox.showerror("无法启动接收服务", str(exc), parent=self.root)

    def _stop_server(self) -> None:
        if not self.server or self.stopping:
            return
        server = self.server
        self.stopping = True
        set_widget_state(self._config_controls, "disabled")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.server_status.configure(text="正在停止接收服务…", style="Warning.TLabel")

        def worker() -> None:
            try:
                server.stop()
                self.events.put({"kind": "server_stop_finished", "success": True})
            except Exception as exc:
                self.events.put(
                    {
                        "kind": "server_stop_finished",
                        "success": False,
                        "message": str(exc) or type(exc).__name__,
                    }
                )

        threading.Thread(target=worker, name="lanbridge-stop-receiver", daemon=True).start()

    def _server_stopped(self) -> None:
        self.stopping = False
        self.server = None
        set_widget_state(self._config_controls, "normal")
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.server_status.configure(text="● 服务未启动 · 点击“启动接收”等待发送端", style="Warning.TLabel")
        self.address_var.set("启动后，这里会显示发送端需要填写的 IP 和端口。")

    def _server_stop_failed(self, message: str) -> None:
        self.stopping = False
        self.activity.error(f"停止接收服务时发生错误：{message}")
        if self.closing:
            self._finish_close()
            return
        set_widget_state(self._config_controls, "disabled")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.server_status.configure(text=f"停止失败 · {message}", style="Danger.TLabel")
        self.address_var.set("服务状态不确定；请重试停止")

    def _open_receive_folder(self) -> None:
        folder = Path(self.folder_var.get().strip() or Path.home()).expanduser()
        open_directory(folder)

    def _ensure_row(self, transfer_id: str, event: dict[str, Any], *, restart: bool = False) -> None:
        exists = self.tree.exists(transfer_id)
        if exists and not restart:
            return
        values = (
            datetime.now().strftime("%H:%M:%S"),
            event.get("file_name") or "未知文件",
            format_bytes(int(event.get("size") or 0)),
            event.get("sender_ip") or "-",
            "接收中",
            "0%",
            "--",
            event.get("relative_path") or "",
        )
        if exists:
            self.tree.item(transfer_id, values=values)
        else:
            self.tree.insert("", "end", iid=transfer_id, values=values)
        self.rows[transfer_id] = "active"
        self._prune_rows()
        self._update_session_summary()

    def _prune_rows(self) -> None:
        excess = len(self.rows) - MAX_SESSION_ROWS
        if excess <= 0:
            return
        for transfer_id in self.tree.get_children():
            if excess <= 0:
                break
            if self.rows.get(transfer_id) == "active":
                continue
            self.tree.delete(transfer_id)
            self.rows.pop(transfer_id, None)
            excess -= 1

    def _update_row(self, transfer_id: str, **changes: Any) -> None:
        if not self.tree.exists(transfer_id):
            return
        values = list(self.tree.item(transfer_id, "values"))
        index_map = {"time": 0, "name": 1, "size": 2, "sender": 3, "status": 4, "progress": 5, "speed": 6, "path": 7}
        for key, value in changes.items():
            if key in index_map:
                values[index_map[key]] = value
        self.tree.item(transfer_id, values=values)
        self.tree.see(transfer_id)

    def _update_session_summary(self) -> None:
        total = len(self.rows)
        success = sum(1 for state in self.rows.values() if state == "success")
        failed = sum(1 for state in self.rows.values() if state == "failed")
        active = sum(1 for state in self.rows.values() if state == "active")
        if total:
            self.session_summary.set(f"共 {total} · 完成 {success} · 失败 {failed} · 进行中 {active}")
        else:
            self.session_summary.set("尚未接收文件")

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
            elif kind == "transfer_started":
                transfer_id = str(event.get("transfer_id"))
                self._ensure_row(transfer_id, event, restart=True)
            elif kind == "transfer_progress":
                transfer_id = str(event.get("transfer_id"))
                total = int(event.get("total") or 0)
                current = int(event.get("bytes") or 0)
                percent = current * 100.0 / total if total else 100.0
                self._update_row(transfer_id, progress=f"{percent:.1f}%", speed=format_rate(float(event.get("rate") or 0.0)))
            elif kind == "transfer_complete":
                transfer_id = str(event.get("transfer_id"))
                self.rows[transfer_id] = "success"
                self._update_row(
                    transfer_id,
                    status="成功",
                    progress="100%",
                    speed=format_rate(float(event.get("receive_rate_bytes_per_second") or 0.0)),
                    path=event.get("saved_relative_path") or "",
                )
                self._prune_rows()
                self._update_session_summary()
            elif kind == "transfer_error":
                transfer_id = str(event.get("transfer_id"))
                if not self.tree.exists(transfer_id):
                    self._ensure_row(
                        transfer_id,
                        {
                            "file_name": event.get("file_name") or "未知文件",
                            "size": 0,
                            "sender_ip": "-",
                            "relative_path": "",
                        },
                    )
                self.rows[transfer_id] = "failed"
                self._update_row(transfer_id, status="失败", speed="--")
                self._prune_rows()
                self._update_session_summary()
            elif kind == "server_started":
                pass
            elif kind == "server_stopped":
                pass
            elif kind == "server_stop_finished":
                if not event.get("success"):
                    self._server_stop_failed(str(event.get("message") or "未知错误"))
                    if self.closed:
                        return
                else:
                    self._server_stopped()
                    if self.closing:
                        self._finish_close()
                        return
        self._poll_after_id = self.root.after(80, self._poll_events)

    def _on_close(self) -> None:
        if self.closing or self.closed:
            return
        active = any(state == "active" for state in self.rows.values())
        if active:
            ask_confirm_close(self.root, "当前仍有文件正在接收。", self._close_now)
        else:
            self._close_now()

    def _close_now(self) -> None:
        if self.closing or self.closed:
            return
        self.closing = True
        self._save_settings()
        if self.server:
            self._stop_server()
        else:
            self._finish_close()

    def _finish_close(self) -> None:
        self.closed = True
        self.root.after_cancel(self._poll_after_id)
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--smoke-test", action="store_true")
    args, _unknown = parser.parse_known_args()
    configure_windows_dpi()
    root = tk.Tk()
    ReceiverApp(root, smoke_test=args.smoke_test)
    root.mainloop()


if __name__ == "__main__":
    main()
