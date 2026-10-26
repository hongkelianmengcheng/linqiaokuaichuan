from __future__ import annotations

import ctypes
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Callable

from .utils import resource_path

COLORS = {
    "window": "#07111F",
    "surface": "#102033",
    "header": "#091827",
    "header_muted": "#94B7CC",
    "text": "#E7F2FA",
    "muted": "#9CB3C6",
    "border": "#29465C",
    "accent": "#38CFE5",
    "accent_hover": "#7CE6F4",
    "success": "#6AE0BB",
    "success_bg": "#123E3B",
    "warning": "#F4C874",
    "warning_bg": "#42351F",
    "danger": "#FF9CAF",
    "danger_bg": "#3B2132",
    "selection": "#214D66",
    "input": "#0A1727",
    "disabled": "#72889B",
}

FONT_FAMILY = "Microsoft YaHei UI"


def configure_windows_dpi() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def apply_theme(root: tk.Tk) -> ttk.Style:
    root.configure(background=COLORS["window"])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=COLORS["surface"], foreground=COLORS["text"], bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"])
    style.configure("TFrame", background=COLORS["window"])
    style.configure("Surface.TFrame", background=COLORS["surface"])
    style.configure("Panel.TFrame", background=COLORS["surface"], bordercolor=COLORS["border"], borderwidth=1, relief="solid")
    style.configure("Header.TFrame", background=COLORS["header"])
    style.configure("TLabel", background=COLORS["window"], foreground=COLORS["text"], font=(FONT_FAMILY, 10))
    style.configure("Surface.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=(FONT_FAMILY, 10))
    style.configure("HeaderTitle.TLabel", background=COLORS["header"], foreground="#FFFFFF", font=(FONT_FAMILY, 21, "bold"))
    style.configure("HeaderSubtitle.TLabel", background=COLORS["header"], foreground=COLORS["header_muted"], font=(FONT_FAMILY, 9))
    style.configure("HeaderInfo.TLabel", background=COLORS["header"], foreground="#FFFFFF", font=(FONT_FAMILY, 9))
    style.configure("Section.TLabel", background=COLORS["surface"], foreground=COLORS["text"], font=(FONT_FAMILY, 11, "bold"))
    style.configure("Muted.TLabel", background=COLORS["surface"], foreground=COLORS["muted"], font=(FONT_FAMILY, 9))
    style.configure("Hint.TLabel", background=COLORS["surface"], foreground=COLORS["muted"], font=(FONT_FAMILY, 8))
    style.configure("Badge.TLabel", background=COLORS["selection"], foreground=COLORS["accent"], font=(FONT_FAMILY, 9, "bold"), padding=(9, 4))
    style.configure("WindowMuted.TLabel", background=COLORS["window"], foreground=COLORS["muted"], font=(FONT_FAMILY, 9))
    style.configure("Success.TLabel", background=COLORS["surface"], foreground=COLORS["success"], font=(FONT_FAMILY, 9, "bold"))
    style.configure("Warning.TLabel", background=COLORS["surface"], foreground=COLORS["warning"], font=(FONT_FAMILY, 9, "bold"))
    style.configure("Danger.TLabel", background=COLORS["surface"], foreground=COLORS["danger"], font=(FONT_FAMILY, 9, "bold"))

    style.configure(
        "TEntry",
        fieldbackground=COLORS["input"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
        relief="flat",
        padding=(9, 7),
        font=(FONT_FAMILY, 10),
    )
    style.map("TEntry", bordercolor=[("focus", COLORS["accent"])], fieldbackground=[("disabled", COLORS["header"])], foreground=[("disabled", COLORS["disabled"])])
    style.configure(
        "TSpinbox",
        fieldbackground=COLORS["input"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        padding=(7, 6),
        font=(FONT_FAMILY, 10),
        background=COLORS["selection"],
        arrowcolor=COLORS["accent"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
    )
    style.map("TSpinbox", fieldbackground=[("disabled", COLORS["header"])], foreground=[("disabled", COLORS["disabled"])], bordercolor=[("focus", COLORS["accent"])])
    style.configure("TCombobox", fieldbackground=COLORS["input"], background=COLORS["selection"], foreground=COLORS["text"], arrowcolor=COLORS["accent"], bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"], padding=(9, 7), font=(FONT_FAMILY, 10))
    style.map("TCombobox", fieldbackground=[("disabled", COLORS["header"]), ("readonly", COLORS["input"])], foreground=[("disabled", COLORS["disabled"]), ("readonly", COLORS["text"])], bordercolor=[("focus", COLORS["accent"])], selectbackground=[("!disabled", COLORS["selection"])], selectforeground=[("!disabled", COLORS["text"])])
    root.option_add("*TCombobox*Listbox.background", COLORS["input"])
    root.option_add("*TCombobox*Listbox.foreground", COLORS["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", COLORS["selection"])
    root.option_add("*TCombobox*Listbox.selectForeground", COLORS["text"])
    root.option_add("*TCombobox*Listbox.font", (FONT_FAMILY, 10))
    style.configure("TCheckbutton", background=COLORS["surface"], foreground=COLORS["text"], font=(FONT_FAMILY, 9))
    style.map("TCheckbutton", background=[("active", COLORS["surface"])], foreground=[("disabled", COLORS["disabled"])])
    _configure_checkmark(root, style)

    base_button = {
        "font": (FONT_FAMILY, 9, "bold"),
        "padding": (12, 8),
        "borderwidth": 1,
        "relief": "flat",
    }
    style.configure("Accent.TButton", **base_button, background=COLORS["accent"], foreground=COLORS["window"], bordercolor=COLORS["accent"], focuscolor=COLORS["window"])
    style.map(
        "Accent.TButton",
        background=[("disabled", "#1C394B"), ("pressed", "#21A8BD"), ("active", COLORS["accent_hover"])],
        foreground=[("disabled", COLORS["disabled"])],
        bordercolor=[("disabled", COLORS["border"]), ("active", "#B0F4FC")],
    )
    style.configure("Secondary.TButton", **base_button, background="#193247", foreground=COLORS["text"], bordercolor=COLORS["border"], focuscolor=COLORS["accent"])
    style.map("Secondary.TButton", background=[("disabled", COLORS["header"]), ("pressed", "#102B3F"), ("active", "#23465D")], foreground=[("disabled", COLORS["disabled"])], bordercolor=[("active", COLORS["accent"])])
    style.configure("Danger.TButton", **base_button, background=COLORS["danger_bg"], foreground=COLORS["danger"], bordercolor="#67364A", focuscolor=COLORS["danger"])
    style.map("Danger.TButton", background=[("disabled", COLORS["header"]), ("active", "#562A40")], foreground=[("disabled", COLORS["disabled"])], bordercolor=[("disabled", COLORS["border"]), ("active", COLORS["danger"])])
    style.configure("Success.TButton", **base_button, background=COLORS["accent"], foreground=COLORS["window"], bordercolor=COLORS["accent"], focuscolor=COLORS["window"])
    style.map("Success.TButton", background=[("disabled", "#1C394B"), ("pressed", "#21A8BD"), ("active", COLORS["accent_hover"])], foreground=[("disabled", COLORS["disabled"])], bordercolor=[("disabled", COLORS["border"]), ("active", "#B0F4FC")])
    link_button = {**base_button, "padding": (8, 5)}
    style.configure("Link.TButton", **link_button, background=COLORS["surface"], foreground=COLORS["accent"], bordercolor=COLORS["surface"], focuscolor=COLORS["accent"])
    style.map("Link.TButton", background=[("active", COLORS["selection"])], foreground=[("disabled", COLORS["disabled"])])

    style.configure(
        "Treeview",
        background=COLORS["input"],
        fieldbackground=COLORS["input"],
        foreground=COLORS["text"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
        relief="flat",
        rowheight=31,
        font=(FONT_FAMILY, 9),
    )
    style.configure(
        "Treeview.Heading",
        background="#173047",
        foreground=COLORS["muted"],
        bordercolor=COLORS["border"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
        relief="flat",
        font=(FONT_FAMILY, 9, "bold"),
        padding=(6, 8),
    )
    style.map("Treeview", background=[("selected", COLORS["selection"])], foreground=[("selected", COLORS["text"])])
    style.map("Treeview.Heading", background=[("active", COLORS["selection"])])
    style.configure("TScrollbar", background="#25475C", troughcolor=COLORS["input"], arrowcolor=COLORS["muted"], bordercolor=COLORS["input"], lightcolor=COLORS["input"], darkcolor=COLORS["input"])
    style.map("TScrollbar", background=[("active", "#376680")])
    style.configure("TSeparator", background=COLORS["border"])

    style.configure("TNotebook", background=COLORS["window"], borderwidth=0, bordercolor=COLORS["border"], lightcolor=COLORS["border"], darkcolor=COLORS["border"])
    style.configure(
        "TNotebook.Tab",
        background=COLORS["window"],
        foreground=COLORS["muted"],
        padding=(18, 9),
        font=(FONT_FAMILY, 9, "bold"),
        borderwidth=0,
        bordercolor=COLORS["border"],
        lightcolor=COLORS["border"],
        darkcolor=COLORS["border"],
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", COLORS["surface"])],
        foreground=[("selected", COLORS["accent"])],
    )
    style.configure(
        "Horizontal.TProgressbar",
        background=COLORS["accent"],
        troughcolor=COLORS["input"],
        bordercolor=COLORS["input"],
        lightcolor=COLORS["accent"],
        darkcolor=COLORS["accent"],
        thickness=8,
    )
    return style


def _configure_checkmark(root: tk.Misc, style: ttk.Style) -> None:
    """Use a real tick in every Tk theme; clam otherwise draws an X on Windows."""
    owner = root._root()
    element = "LanBridge.Check.indicator"
    if element not in style.element_names():
        scale = max(1.0, float(root.tk.call("tk", "scaling")) / (96.0 / 72.0))
        size = max(18, round(18 * scale))

        def indicator(selected: bool, disabled: bool = False, active: bool = False) -> tk.PhotoImage:
            picture = tk.PhotoImage(master=root, width=size + 8, height=size)
            edge = COLORS["disabled"] if disabled else COLORS["accent"] if selected or active else COLORS["muted"]
            fill = "#294354" if disabled and selected else COLORS["accent"] if selected else COLORS["input"]
            picture.put(edge, to=(1, 1, size - 1, size - 1))
            picture.put(fill, to=(2, 2, size - 2, size - 2))
            if selected:
                # Draw the two strokes geometrically, independent of font glyph availability.
                ink = COLORS["text"] if disabled else COLORS["window"]
                points = ((0.25, 0.50), (0.43, 0.69), (0.76, 0.30))
                width = max(2, round(size * 0.12))
                for start, end in zip(points, points[1:]):
                    x1, y1 = (round(v * size) for v in start)
                    x2, y2 = (round(v * size) for v in end)
                    steps = max(abs(x2 - x1), abs(y2 - y1), 1)
                    for step in range(steps + 1):
                        x = round(x1 + (x2 - x1) * step / steps)
                        y = round(y1 + (y2 - y1) * step / steps)
                        picture.put(ink, to=(x, y, min(size - 2, x + width), min(size - 2, y + width)))
            return picture

        images = [indicator(False), indicator(True), indicator(False, True), indicator(True, True), indicator(False, active=True)]
        style.element_create(element, "image", images[0], ("disabled", "selected", images[3]), ("disabled", images[2]), ("selected", images[1]), ("active", images[4]), sticky="w")
        # Tk only stores image names. Keep Python references for the interpreter lifetime.
        owner._lanbridge_check_images = images
    style.layout("TCheckbutton", [("Checkbutton.padding", {"sticky": "nswe", "children": [(element, {"side": "left", "sticky": "w"}), ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [("Checkbutton.label", {"sticky": "nswe"})]})]})])


def set_window_icon(root: tk.Misc, icon_name: str) -> None:
    icon_path = resource_path(f"assets/{icon_name}")
    if icon_path.exists():
        try:
            root.iconbitmap(default=str(icon_path))
        except tk.TclError:
            pass


def center_window(window: tk.Toplevel | tk.Tk, width: int, height: int) -> None:
    window.update_idletasks()
    screen_width = window.winfo_screenwidth()
    screen_height = window.winfo_screenheight()
    width = min(width, max(320, screen_width - 40))
    height = min(height, max(240, screen_height - 80))
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")


def make_surface(parent: tk.Misc, *, padding: tuple[int, int] = (18, 14)) -> ttk.Frame:
    frame = ttk.Frame(parent, style="Panel.TFrame", padding=padding)
    return frame


def make_log_text(parent: tk.Misc) -> tk.Text:
    text = tk.Text(
        parent,
        wrap="word",
        relief="flat",
        borderwidth=0,
        background=COLORS["input"],
        foreground=COLORS["muted"],
        insertbackground="#FFFFFF",
        font=("Consolas", 9),
        padx=12,
        pady=10,
        state="disabled",
    )
    text.tag_configure("INFO", foreground="#D1D5DB")
    text.tag_configure("WARNING", foreground="#FBBF24")
    text.tag_configure("ERROR", foreground="#FCA5A5")
    text.tag_configure("SUCCESS", foreground="#6EE7B7")
    return text


def append_log(text: tk.Text, timestamp: str, level: str, message: str) -> None:
    text.configure(state="normal")
    text.insert("end", f"{timestamp}  {level:<7}  {message}\n", level if level in {"INFO", "WARNING", "ERROR", "SUCCESS"} else "INFO")
    # Keep the live view bounded; the full history remains in the log files.
    extra_lines = int(text.index("end-1c").split(".")[0]) - 2001
    if extra_lines > 0:
        text.delete("1.0", f"{extra_lines + 1}.0")
    text.see("end")
    text.configure(state="disabled")


def open_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))
        else:
            raise OSError("当前系统不支持打开目录")
    except OSError as exc:
        messagebox.showerror("无法打开目录", str(exc))


class ToolTip:
    def __init__(self, widget: tk.Widget, text: str, delay_ms: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event: tk.Event | None = None) -> None:
        self._hide()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _show(self) -> None:
        self._after_id = None
        if not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        self._window = tk.Toplevel(self.widget)
        self._window.wm_overrideredirect(True)
        self._window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            self._window,
            text=self.text,
            background=COLORS["selection"],
            foreground=COLORS["text"],
            font=(FONT_FAMILY, 8),
            padx=8,
            pady=5,
            relief="solid",
            borderwidth=1,
            wraplength=340,
            justify="left",
        )
        label.pack()
        self._window.update_idletasks()
        x = max(0, min(x, self.widget.winfo_screenwidth() - self._window.winfo_reqwidth() - 8))
        if y + self._window.winfo_reqheight() > self.widget.winfo_screenheight() - 20:
            y = max(0, self.widget.winfo_rooty() - self._window.winfo_reqheight() - 6)
        self._window.wm_geometry(f"+{x}+{y}")

    def _hide(self, _event: tk.Event | None = None) -> None:
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None
        if self._window:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


def set_widget_state(widgets: list[tk.Widget], state: str) -> None:
    for widget in widgets:
        try:
            widget.configure(state=state)
        except tk.TclError:
            pass


def ask_confirm_close(parent: tk.Misc, active_description: str, on_confirm: Callable[[], None]) -> None:
    if messagebox.askyesno("确认退出", f"{active_description}\n退出会中断当前任务，确定继续吗？", parent=parent):
        on_confirm()
