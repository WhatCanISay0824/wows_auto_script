"""可复用 Tk/ttk 控件与统一风格（平面背景、细分隔线、状态色）。

设计口径：

- 平面背景、细分隔线、小圆角（Tk 只能在边框/留白上近似）、
  Segoe UI / Microsoft YaHei UI；
- 颜色只用于状态与真实异常（运行中绿、暂停橙、错误红、禁用灰）；
- 不写欢迎语、口号、装饰插画（GUI交互与预览设计 §1/§3）。

本模块只在主线程使用（调用方为 ``src/gui/app.py``）。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterable, Sequence
from tkinter import font as tkfont
from tkinter import ttk

from src.gui.viewmodel import LogRow, StatusLine

# 基础色板（浅色；颜色只表达状态/异常，正文用中性色）
BG = "#ffffff"
PANEL = "#ffffff"
SIDEBAR = "#f7f7f7"
BORDER = "#e5e5e3"
TEXT = "#242424"
SECONDARY = "#737370"
ACCENT = "#30302e"
ACCENT_SOFT = "#e9e9e6"
OK_GREEN = "#15803d"
WARN_ORANGE = "#b45309"
WARN_BG = "#fdf3e3"
ERROR_RED = "#b91c1c"
ERROR_BG = "#fdecec"
WARN_YELLOW = "#a16207"

FONT_FAMILY = "Microsoft YaHei UI"
UI_SCALE = 1.0
FONT_FALLBACKS = ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "TkDefaultFont")

EMPHASIS_COLOR = {
    "normal": TEXT,
    "muted": SECONDARY,
    "ok": OK_GREEN,
    "warning": WARN_ORANGE,
    "error": ERROR_RED,
}

LOG_LEVEL_COLOR = {
    "DEBUG": SECONDARY,
    "INFO": TEXT,
    "WARNING": WARN_ORANGE,
    "ERROR": ERROR_RED,
    "CRITICAL": ERROR_RED,
}


def font(size: int = 10, bold: bool = False) -> tuple:
    # 正数字号使用磅，交给 Tk 按系统 DPI 缩放。
    points = size + 1 if size <= 12 else round(size * 1.1)
    return (FONT_FAMILY, points, "bold") if bold else (FONT_FAMILY, points)


def px(value: float) -> int:
    return round(value * UI_SCALE)


def configure_styles(root: tk.Misc) -> ttk.Style:
    """统一主题/字体/控件样式；失败不阻塞启动（7.1）。"""

    global FONT_FAMILY, UI_SCALE
    UI_SCALE = max(1.0, float(root.winfo_fpixels("1i")) / 96.0)
    try:
        families = set(tkfont.families(root))
        for candidate in FONT_FALLBACKS:
            if candidate in families:
                FONT_FAMILY = candidate
                break
    except tk.TclError:
        pass

    style = ttk.Style(root)
    for theme in ("clam", "vista", "default"):
        if theme in style.theme_names():
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue

    base_font = font(10)
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
        try:
            tkfont.nametofont(name, root=root).configure(family=FONT_FAMILY, size=base_font[1])
        except tk.TclError:
            pass
    style.configure(".", font=base_font, background=BG, foreground=TEXT)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("Sidebar.TFrame", background=SIDEBAR)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
    style.configure("Secondary.TLabel", background=BG, foreground=SECONDARY)
    style.configure("PanelSecondary.TLabel", background=PANEL, foreground=SECONDARY)
    style.configure("Title.TLabel", background=BG, foreground=TEXT, font=font(12, bold=True))
    style.configure("PanelTitle.TLabel", background=PANEL, foreground=TEXT, font=font(11, bold=True))
    style.configure("Section.TLabel", background=PANEL, foreground=TEXT, font=font(10, bold=True))
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                    rowheight=px(32), borderwidth=0, font=font(9))
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, padding=(px(9), px(6)), fieldbackground=PANEL,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                        arrowsize=px(12), relief="flat")
        style.map(name, fieldbackground=[("disabled", SIDEBAR), ("readonly", PANEL)],
                  foreground=[("disabled", SECONDARY), ("!disabled", TEXT)])
    for name in ("TCheckbutton", "TRadiobutton"):
        style.configure(name, background=PANEL, indicatorsize=px(12))
    style.configure("Treeview.Heading", font=font(9), background=SIDEBAR, relief="flat", padding=(6, 5))
    style.configure("TButton", padding=(10, 6), background=PANEL, bordercolor=BORDER,
                    lightcolor=BORDER, darkcolor=BORDER, borderwidth=1, relief="solid")
    style.configure("Link.TButton", padding=(0, 0), borderwidth=0, background=PANEL, foreground=SECONDARY)
    style.configure("Accent.TButton", font=font(10, bold=True), background=ACCENT, foreground="#ffffff")
    style.map("Accent.TButton", background=[("disabled", BORDER), ("active", "#454542")],
              foreground=[("disabled", SECONDARY), ("!disabled", "#ffffff")])
    for name, background in (("Nav.TButton", SIDEBAR), ("NavActive.TButton", ACCENT_SOFT)):
        style.configure(name, padding=(14, 10), anchor="center", background=background,
                        borderwidth=0, relief="flat", font=font(10))
        style.map(name, background=[("active", ACCENT_SOFT), ("!active", background)])
    style.configure("TNotebook", background=PANEL, borderwidth=0)
    style.configure("Records.Treeview", borderwidth=0, rowheight=px(34))
    style.layout("Records.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Records.Treeview.Heading", background=PANEL, relief="flat", font=font(9), padding=(0, 9))
    style.configure("TNotebook.Tab", padding=(16, 9), background=SIDEBAR, borderwidth=0)
    style.map("TNotebook.Tab", background=[("selected", PANEL)])
    style.configure("TSeparator", background=BORDER)
    style.map("TButton", foreground=[("disabled", SECONDARY)])
    style.map("Nav.TButton", foreground=[("disabled", SECONDARY)])
    return style


# ---------------------------------------------------------------------------
# 基础容器
# ---------------------------------------------------------------------------


class SectionFrame(tk.Frame):
    """带标题与细分隔线的区域容器（不使用阴影/多层卡片）。"""

    def __init__(
        self,
        master: tk.Misc,
        title: str,
        *,
        bg: str = PANEL,
        padding: int = 12,
    ) -> None:
        super().__init__(master, bg=bg, bd=0)
        self.title = title
        self._bg = bg
        header = tk.Frame(self, bg=bg)
        header.pack(fill="x", padx=padding, pady=(padding, 0))
        tk.Label(header, text=title, bg=bg, fg=TEXT, font=font(10), anchor="w").pack(side="left")
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x", padx=padding, pady=(4, 0))
        self.content = tk.Frame(self, bg=bg)
        self.content.pack(fill="both", expand=True, padx=padding, pady=(8, padding))


class ScrollableFrame(tk.Frame):
    """可纵向滚动的页面容器（最小窗口 1024×720 下避免控件被挤掉）。"""

    def __init__(self, master: tk.Misc, *, bg: str = BG, fill_height: bool = False) -> None:
        super().__init__(master, bg=bg)
        self._fill_height = fill_height
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.body = tk.Frame(self.canvas, bg=bg)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._toplevel = self.winfo_toplevel()
        self._wheel_binding = self._toplevel.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")

    def _on_body_configure(self, _event: tk.Event) -> None:
        if self._fill_height:
            self.canvas.itemconfigure(self._window, height=max(self.body.winfo_reqheight(), self.canvas.winfo_height()))
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)
        if self._fill_height:
            self.canvas.itemconfigure(self._window, height=max(self.body.winfo_reqheight(), event.height))

    def _on_mousewheel(self, event: tk.Event) -> None:
        widget = event.widget
        if isinstance(widget, (tk.Text, ttk.Treeview, ttk.Combobox)):
            return
        while widget is not None and widget is not self:
            widget = getattr(widget, "master", None)
        if widget is None or self.body.winfo_height() <= self.canvas.winfo_height():
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is self and self._wheel_binding:
            self._toplevel.unbind("<MouseWheel>", self._wheel_binding)
            self._wheel_binding = None

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0.0)


class KeyValueGrid(tk.Frame):
    """只读键值表：``set_rows()`` 在键集合不变时只更新文本（5 Hz 刷新不重建控件）。"""

    def __init__(
        self,
        master: tk.Misc,
        *,
        columns: int = 1,
        bg: str = PANEL,
        key_width: int = 16,
        value_wraplength: int = 360,
    ) -> None:
        super().__init__(master, bg=bg)
        self._bg = bg
        self.columns = max(1, int(columns))
        self.key_width = key_width
        self.value_wraplength = value_wraplength
        for column in range(self.columns):
            self.grid_columnconfigure(column * 2 + 1, weight=1, uniform="values")
        self._keys: tuple[str, ...] = ()
        self._value_labels: dict[str, tk.Label] = {}
        self._key_labels: dict[str, tk.Label] = {}

    def set_rows(self, rows: Sequence[StatusLine]) -> None:
        keys = tuple(row.key for row in rows)
        if keys != self._keys:
            self._rebuild(rows)
            return
        for row in rows:
            self._apply(row)

    def _rebuild(self, rows: Sequence[StatusLine]) -> None:
        for child in self.winfo_children():
            child.destroy()
        self._value_labels.clear()
        self._key_labels.clear()
        self._keys = tuple(row.key for row in rows)
        for index, row in enumerate(rows):
            column, line = index % self.columns, index // self.columns
            key_label = tk.Label(
                self,
                text=row.label,
                bg=self._bg,
                fg=SECONDARY,
                font=font(9),
                anchor="w",
                wraplength=self.key_width * 10,
                justify="left",
            )
            key_label.grid(row=line, column=column * 2, sticky="nw", padx=(0, 6), pady=1)
            value_label = tk.Label(
                self,
                text=row.value,
                bg=self._bg,
                fg=TEXT,
                font=font(10),
                anchor="w",
                justify="left",
                wraplength=self.value_wraplength,
            )
            value_label.grid(row=line, column=column * 2 + 1, sticky="nw", padx=(0, 18), pady=1)
            self._key_labels[row.key] = key_label
            self._value_labels[row.key] = value_label
            self._apply(row)

    def _apply(self, row: StatusLine) -> None:
        label = self._value_labels.get(row.key)
        if label is None:
            return
        if label.cget("text") != row.value:
            label.configure(text=row.value)
        label.configure(fg=EMPHASIS_COLOR.get(row.emphasis, TEXT))

    def value_text(self, key: str) -> str | None:
        """测试/诊断用：返回某行的当前文本（不存在返回 ``None``）。"""

        label = self._value_labels.get(key)
        if label is None:
            return None
        return str(label.cget("text"))

    def values(self) -> dict[str, str]:
        return {key: str(label.cget("text")) for key, label in self._value_labels.items()}


class Banner(tk.Frame):
    """单行状态横幅（错误/警告）；不自动弹窗、不抢焦点（12.7）。"""

    def __init__(
        self,
        master: tk.Misc,
        *,
        buttons: Iterable[tuple[str, Callable[[], None]]] = (),
    ) -> None:
        super().__init__(master, bg=WARN_BG, highlightbackground=BORDER, highlightthickness=1)
        self._label = tk.Label(
            self,
            text="",
            bg=WARN_BG,
            fg=WARN_ORANGE,
            font=font(10),
            anchor="w",
            justify="left",
            wraplength=980,
        )
        self._label.pack(side="left", fill="x", expand=True, padx=10, pady=6)
        self._buttons: dict[str, ttk.Button] = {}
        for text, command in buttons:
            button = ttk.Button(self, text=text, command=command)
            button.pack(side="right", padx=(0, 6), pady=4)
            self._buttons[text] = button
        self._visible = False

    def show(self, text: str, level: str = "warning") -> None:
        background = ERROR_BG if level == "error" else WARN_BG
        foreground = ERROR_RED if level == "error" else WARN_ORANGE
        self.configure(bg=background)
        self._label.configure(text=text, bg=background, fg=foreground)
        for button in self._buttons.values():
            button.configure(state="normal")
        if not self._visible:
            self.pack(fill="x", padx=12, pady=(0, 6))
            self._visible = True

    def hide(self) -> None:
        if self._visible:
            self.pack_forget()
            self._visible = False

    @property
    def visible(self) -> bool:
        return self._visible

    @property
    def text(self) -> str:
        return str(self._label.cget("text"))

    def button(self, text: str) -> ttk.Button:
        return self._buttons[text]

    @property
    def button_labels(self) -> tuple[str, ...]:
        return tuple(self._buttons)


def make_button(
    master: tk.Misc,
    text: str,
    command: Callable[[], None],
    *,
    style: str = "TButton",
) -> ttk.Button:
    return ttk.Button(master, text=text, command=command, style=style)


def make_label(
    master: tk.Misc,
    text: str,
    *,
    style: str = "Panel.TLabel",
    font_size: int = 10,
    bold: bool = False,
    wrap: int | None = None,
) -> tk.Label:
    background = PANEL if style.startswith("Panel") else BG
    kwargs: dict[str, object] = {
        "text": text,
        "bg": background,
        "fg": TEXT,
        "font": font(font_size, bold),
        "anchor": "w",
        "justify": "left",
    }
    if wrap:
        kwargs["wraplength"] = wrap
    return tk.Label(master, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 日志控件
# ---------------------------------------------------------------------------


def configure_log_tags(text: tk.Text) -> None:
    for level, color in LOG_LEVEL_COLOR.items():
        text.tag_configure(level, foreground=color)
    text.tag_configure("recent", foreground=SECONDARY)


def insert_log_row(text: tk.Text, row: LogRow) -> None:
    text.configure(state="normal")
    text.insert("end", f"{row.wall_at:<12} {row.level_name:<7} {row.source:<20} {row.message}\n", row.level_name)
    text.configure(state="disabled")


def render_log_rows(text: tk.Text, rows: Iterable[LogRow]) -> None:
    text.configure(state="normal")
    text.delete("1.0", "end")
    for row in rows:
        text.insert("end", f"{row.wall_at:<12} {row.level_name:<7} {row.source:<20} {row.message}\n", row.level_name)
    text.configure(state="disabled")


# ---------------------------------------------------------------------------
# 组合面板
# ---------------------------------------------------------------------------


class PerformanceSummary(tk.Frame):
    """与预览一致的性能侧栏，数值由服务采样提供。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg="#fafafa", width=px(240))
        self.pack_propagate(False)
        self.rows_grid = KeyValueGrid(self, columns=1)
        self._values: dict[str, tk.Label] = {}
        self._bars: dict[str, tuple[tk.Canvas, float]] = {}
        body = tk.Frame(self, bg="#fafafa")
        body.pack(fill="both", expand=True, padx=18, pady=20)
        header = tk.Frame(body, bg="#fafafa")
        header.pack(fill="x", pady=(0, 18))
        self._label(header, "性能").pack(side="left")
        self._label(header, "1 秒", muted=True, size=9).pack(side="right")
        for key, title in (("cpu", "全机 CPU"), ("memory", "物理内存")):
            self._row(body, key, title)
            bar = tk.Canvas(body, height=4, bg="#e3e3e3", highlightthickness=0, bd=0)
            bar.pack(fill="x", pady=(8, 18))
            self._bars[key] = (bar, 0.0)
            bar.bind("<Configure>", lambda event, name=key: self._paint_bar(name))
        for key, title in (("mem_used", "已用内存"), ("mem_available", "可用内存"),
                           ("script_private", "脚本内存"), ("commit_available", "Commit 余量")):
            self._row(body, key, title, pady=4)
        self._divider(body)
        for key, title in (("capture_ms", "截图耗时"), ("loop", "循环节拍 P95"), ("pending", "输入队列")):
            self._row(body, key, title, pady=4)
        self._divider(body)
        self._label(body, "过载保护").pack(anchor="w", pady=(2, 10))
        for key, title in (("protection", "保护模式"), ("sampled", "采样时间"), ("overload", "当前状态")):
            self._row(body, key, title, pady=4)

    @staticmethod
    def _label(master, text, *, muted=False, size=10):
        return tk.Label(master, text=text, bg="#fafafa", fg=SECONDARY if muted else TEXT,
                        font=font(size), anchor="w", bd=0, padx=0, pady=0)

    def _row(self, master, key, title, pady=0):
        row = tk.Frame(master, bg="#fafafa")
        row.pack(fill="x", pady=pady)
        self._label(row, title, muted=True, size=9).pack(side="left")
        value = self._label(row, "—", size=9)
        value.pack(side="right")
        self._values[key] = value

    @staticmethod
    def _divider(master):
        tk.Frame(master, bg=BORDER, height=1).pack(fill="x", pady=(12, 10))

    def _paint_bar(self, key):
        bar, value = self._bars[key]
        bar.delete("all")
        bar.create_rectangle(0, 0, bar.winfo_width() * value / 100, 4, fill="#686868", outline="")

    def update_from(self, rows: Sequence[StatusLine]) -> None:
        self.rows_grid.set_rows(rows)
        for row in rows:
            if row.key in self._values:
                self._values[row.key].configure(text=row.value)

    def render(self, perf, pending_actions=None, capabilities=None):
        from src.gui.viewmodel import performance_rows

        self.update_from(performance_rows(perf, capabilities))
        if perf is None:
            return
        memory_pct = (100 * (1 - perf.mem_available_mb / perf.mem_total_mb)
                      if perf.mem_total_mb and perf.mem_available_mb is not None else None)
        for key, value in (("cpu", perf.cpu_pct), ("memory", memory_pct)):
            self._values[key].configure(text=f"{value:.0f}%" if value is not None else "—")
            self._bars[key] = (self._bars[key][0], min(100, max(0, value or 0)))
            self._paint_bar(key)
        if perf.mem_total_mb is not None and perf.mem_available_mb is not None:
            self._values["mem_used"].configure(text=f"{(perf.mem_total_mb-perf.mem_available_mb)/1024:.1f} / {perf.mem_total_mb/1024:.0f} GiB")
        for key, value in (("mem_available", perf.mem_available_mb), ("commit_available", perf.commit_available_mb)):
            self._values[key].configure(text=f"{value/1024:.1f} GiB" if value is not None else "—")
        self._values["loop"].configure(text=f"{perf.loop_tick_p95_ms:.0f} ms" if perf.loop_tick_p95_ms is not None else "—")
        self._values["pending"].configure(text=str(pending_actions) if pending_actions is not None else "—")
        self._values["protection"].configure(text={"monitor": "仅监测", "off": "关闭", "pause_stop": "暂停 / 停止", "stop": "停止"}.get(perf.mode, perf.mode))
        if perf.unavailable_reason or not perf.enabled:
            self._values["overload"].configure(text=perf.unavailable_reason or "监测未启用", wraplength=160)
        sampled = perf.sampled_wall or "—"
        self._values["sampled"].configure(text=sampled[11:19] if len(sampled) > 19 else sampled)


class RecentLogPanel(tk.Frame):
    """底部最近日志（最多 8 条；完整日志见日志页）。"""

    def __init__(self, master: tk.Misc, *, max_lines: int = 8, on_show_all=None) -> None:
        super().__init__(master, bg=BG)
        self.max_lines = max_lines
        tk.Frame(self, bg=BORDER, height=1).pack(fill="x")
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=20, pady=(10, 5))
        tk.Label(
            header,
            text="日志",
            bg=BG,
            fg=TEXT,
            font=font(9),
            anchor="w",
        ).pack(side="left")
        if on_show_all is not None:
            ttk.Button(header, text="查看全部", command=on_show_all, style="Link.TButton").pack(side="right")
        body = tk.Frame(self, bg=PANEL)
        body.pack(fill="both", expand=True, padx=20, pady=(0, 10))
        self.text = tk.Text(
            body,
            height=3,
            wrap="none",
            bg=PANEL,
            fg=TEXT,
            font=font(9),
            relief="flat",
            state="disabled",
        )
        scroll = ttk.Scrollbar(body, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        self.text.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=4)
        scroll.pack(side="right", fill="y", pady=4, padx=(0, 2))
        configure_log_tags(self.text)

    def set_rows(self, rows: Iterable[LogRow]) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        for row in rows:
            self.text.insert("end", f"{row.wall_at[:8]:<10} {row.level_name:<7} {row.message}\n", row.level_name)
        self.text.configure(state="disabled")


class StatusBar(tk.Frame):
    """底部状态行（输入状态 / 画面更新与清理进度 / 目标反馈）。"""

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self._labels: list[tk.Label] = []
        for index in range(3):
            label = tk.Label(self, text="", bg=PANEL, fg=SECONDARY, font=font(9), anchor="w")
            label.pack(side="left", padx=(12 if index == 0 else 18, 18), pady=4)
            self._labels.append(label)

    def set_texts(self, texts: Sequence[str]) -> None:
        for label, text in zip(self._labels, texts, strict=False):
            label.configure(text=text)


class Sidebar(tk.Frame):
    """左侧导航（运行/性能/日志/设置）+ 底部客户端与船只摘要。"""

    def __init__(
        self,
        master: tk.Misc,
        pages: Sequence[tuple[str, str]],
        on_select: Callable[[str], None],
    ) -> None:
        super().__init__(master, bg=SIDEBAR, width=px(170))
        self.pack_propagate(False)
        self.buttons: dict[str, ttk.Button] = {}
        tk.Label(self, text="WoWS  托管台", bg=SIDEBAR, fg=TEXT, font=font(11), anchor="w").pack(
            fill="x", padx=20, pady=(24, 30)
        )
        for page_id, title in pages:
            button = ttk.Button(
                self,
                text=title,
                style="Nav.TButton",
                command=lambda page=page_id: on_select(page),
            )
            button.pack(fill="x", padx=8, pady=1)
            self.buttons[page_id] = button
        tk.Frame(self, bg=SIDEBAR).pack(fill="both", expand=True)
        self.footer_label = tk.Label(
            self,
            text="客户端：—\n船只：—",
            bg=SIDEBAR,
            fg=SECONDARY,
            font=font(9),
            anchor="w",
            justify="left",
            wraplength=128,
        )
        self.footer_label.pack(fill="x", padx=14, pady=(6, 14))

    def set_active(self, page_id: str) -> None:
        for key, button in self.buttons.items():
            button.configure(style="NavActive.TButton" if key == page_id else "Nav.TButton")

    def set_footer(self, client: str, ship: str) -> None:
        self.footer_label.configure(text=f"{client}\n{ship}")

    def set_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.buttons.values():
            button.configure(state=state)


def emphasis_color(emphasis: str) -> str:
    return EMPHASIS_COLOR.get(emphasis, TEXT)


__all__ = [
    "ACCENT",
    "ACCENT_SOFT",
    "BG",
    "BORDER",
    "Banner",
    "EMPHASIS_COLOR",
    "ERROR_BG",
    "ERROR_RED",
    "KeyValueGrid",
    "LOG_LEVEL_COLOR",
    "OK_GREEN",
    "PANEL",
    "PerformanceSummary",
    "RecentLogPanel",
    "SIDEBAR",
    "SECONDARY",
    "SectionFrame",
    "ScrollableFrame",
    "Sidebar",
    "StatusBar",
    "TEXT",
    "WARN_BG",
    "WARN_ORANGE",
    "WARN_YELLOW",
    "configure_log_tags",
    "configure_styles",
    "emphasis_color",
    "font",
    "insert_log_row",
    "make_button",
    "make_label",
    "render_log_rows",
]
