"""日志页（GUI 界面设计文档 7.3 / 12；GUI交互与预览设计 §7）。

界面最多保留 2000 行；完整日志由运行服务写入本地文件，GUI 只展示。
筛选（级别 / 关键词 / 当前场次 / 自动滚动）只影响视图，不改变日志采集。
事件序号缺口与服务侧丢弃计数必须显示，不得静默丢失（4.8 / 12.3）。
"""

from __future__ import annotations

import os
import tkinter as tk
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from tkinter import ttk

from src.gui import widgets
from src.gui.viewmodel import (
    DEFAULT_LOG_MAX_LINES,
    LEVEL_FILTER_OPTIONS,
    LogModel,
    LogRow,
    SeqGapTracker,
    filter_log_rows,
    format_log_line,
    level_at_least,
    log_row_from_event,
    seq_gap_text,
)
from src.runtime.messages import RuntimeEvent

_LEVEL_BY_LABEL = {label: minimum for label, minimum in LEVEL_FILTER_OPTIONS}
_DEFAULT_LEVEL_LABEL = "INFO 及以上"
_KEYWORD_DEBOUNCE_MS = 300


class LogPage(tk.Frame):
    """完整日志页：筛选 + 有界日志正文 + 诊断摘要。"""

    def __init__(self, master: tk.Misc, *, max_lines: int = DEFAULT_LOG_MAX_LINES) -> None:
        super().__init__(master, bg=widgets.BG)
        self.model = LogModel(max_lines)
        self.seq_tracker = SeqGapTracker()
        self._battle_id: int | None = None
        self._log_path: Path | None = None
        self._keyword_after_id: str | None = None
        self._service_dropped = 0

        self._build_filter_bar()
        self._build_body()
        self._build_summary()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

    # -- 构建 ---------------------------------------------------------
    def _build_filter_bar(self) -> None:
        bar = widgets.SectionFrame(self, "脚本日志")
        bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 2))
        inner = bar.content

        tk.Label(inner, text="级别", bg=widgets.PANEL, fg=widgets.SECONDARY, font=widgets.font(9)).pack(
            side="left"
        )
        self.level_var = tk.StringVar(value=_DEFAULT_LEVEL_LABEL)
        self.level_combo = ttk.Combobox(
            inner,
            textvariable=self.level_var,
            values=[label for label, _ in LEVEL_FILTER_OPTIONS],
            state="readonly",
            width=14,
        )
        self.level_combo.pack(side="left", padx=(4, 12))
        self.level_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_filter())

        tk.Label(inner, text="关键词", bg=widgets.PANEL, fg=widgets.SECONDARY, font=widgets.font(9)).pack(
            side="left"
        )
        self.keyword_var = tk.StringVar(value="")
        self.keyword_entry = ttk.Entry(inner, textvariable=self.keyword_var, width=22)
        self.keyword_entry.pack(side="left", padx=(4, 12))
        self.keyword_var.trace_add("write", lambda *_args: self._schedule_keyword_filter())

        self.only_battle_var = tk.BooleanVar(value=False)
        self.only_battle_check = ttk.Checkbutton(
            inner,
            text="仅显示本场",
            variable=self.only_battle_var,
            command=self.refresh_filter,
        )
        self.only_battle_check.pack(side="left", padx=(0, 12))

        self.autoscroll_var = tk.BooleanVar(value=True)
        self.autoscroll_check = ttk.Checkbutton(
            inner,
            text="自动滚动",
            variable=self.autoscroll_var,
        )
        self.autoscroll_check.pack(side="left", padx=(0, 12))

        self.clear_button = widgets.make_button(inner, "清空视图", self.clear_view)
        self.clear_button.pack(side="left", padx=(0, 6))
        self.open_dir_button = widgets.make_button(inner, "打开日志目录", self.open_log_dir)
        self.open_dir_button.pack(side="left", padx=(0, 6))

        self.battle_hint = tk.Label(
            inner,
            text="场次未关联（ISS-010 未修复）",
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
        )

    def _build_body(self) -> None:
        body = tk.Frame(self, bg=widgets.PANEL)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=4)
        text_frame = tk.Frame(body, bg=widgets.PANEL)
        text_frame.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.text = tk.Text(
            text_frame,
            wrap="none",
            bg=widgets.PANEL,
            fg=widgets.TEXT,
            font=widgets.font(9),
            relief="flat",
            state="disabled",
        )
        scroll_y = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        scroll_x = ttk.Scrollbar(text_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        text_frame.grid_rowconfigure(0, weight=1)
        text_frame.grid_columnconfigure(0, weight=1)
        widgets.configure_log_tags(self.text)
        # 用户上翻时自动关闭自动滚动，避免打断阅读（7.3）。
        self.text.bind("<MouseWheel>", self._on_mousewheel)
        self.text.bind("<Button-4>", lambda _event: self.autoscroll_var.set(False))
        self.text.bind("<Prior>", lambda _event: self.autoscroll_var.set(False))

    def _build_summary(self) -> None:
        summary = tk.Frame(self, bg=widgets.BG)
        summary.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        self.path_label = tk.Label(
            summary, text="日志：—", bg=widgets.BG, fg=widgets.SECONDARY, font=widgets.font(9), anchor="w"
        )
        self.path_label.pack(anchor="w")
        self.seq_label = tk.Label(
            summary,
            text=seq_gap_text(0, 0),
            bg=widgets.BG,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
        )
        self.seq_label.pack(anchor="w")
        self.notice_label = tk.Label(
            summary,
            text="",
            bg=widgets.BG,
            fg=widgets.WARN_ORANGE,
            font=widgets.font(9),
            anchor="w",
            justify="left",
            wraplength=820,
        )
        self.notice_label.pack(anchor="w")

    # -- 数据入口 -----------------------------------------------------
    def append_events(self, events: Iterable[RuntimeEvent]) -> None:
        event_list = list(events)
        if not event_list:
            return
        self.seq_tracker.observe_many(event_list)
        rows = [log_row_from_event(event) for event in event_list]
        self.model.append_rows(rows)
        passed = self._passing(rows)
        if passed:
            self.text.configure(state="normal")
            for row in passed:
                self.text.insert("end", format_log_line(row) + "\n", row.level_name)
            self._trim_text_view()
            self.text.configure(state="disabled")
            if self.autoscroll_var.get():
                self.text.see("end")
        self._refresh_summary_labels()

    def refresh_filter(self) -> None:
        rows = self.model.filtered(
            minimum_level=self._minimum_level(),
            keyword=self.keyword_var.get(),
            only_battle=self.only_battle_var.get(),
            battle_id=self._battle_id,
        )
        widgets.render_log_rows(self.text, rows)
        if self.autoscroll_var.get():
            self.text.see("end")
        self._refresh_battle_hint()

    def clear_view(self) -> None:
        self.model.clear()
        widgets.render_log_rows(self.text, ())
        self._refresh_summary_labels()

    def set_battle_context(self, battle_id: int | None) -> None:
        if battle_id == self._battle_id:
            return
        self._battle_id = battle_id
        self._refresh_battle_hint()
        if self.only_battle_var.get():
            self.refresh_filter()

    def set_log_path(self, path: Path | None) -> None:
        new_path = Path(path) if path else None
        if new_path == self._log_path:
            return
        self._log_path = new_path
        if self._log_path is None:
            self.path_label.configure(text="日志：—（服务未提供日志文件）")
        else:
            self.path_label.configure(text=f"日志：{self._log_path}")

    def append_note(self, message: str, *, level: str = "WARNING") -> None:
        """GUI 自身提示（如“继续等待清理”“退出应用警告”）写入日志视图。"""

        row = LogRow(
            seq=-1,
            wall_at=datetime.now().strftime("%H:%M:%S.%f")[:12],
            level=level.upper(),
            source="gui",
            message=message,
        )
        self.model.append_rows([row])
        if level_at_least(row.level, self._minimum_level()):
            self.text.configure(state="normal")
            self.text.insert("end", format_log_line(row) + "\n", row.level_name)
            self._trim_text_view()
            self.text.configure(state="disabled")
            if self.autoscroll_var.get():
                self.text.see("end")
        self._refresh_summary_labels()

    def set_service_dropped(self, dropped: int) -> None:
        dropped = max(0, int(dropped))
        if dropped == self._service_dropped:
            return
        self._service_dropped = dropped
        self._refresh_summary_labels()

    # -- 交互 ---------------------------------------------------------
    def _on_mousewheel(self, event: tk.Event) -> None:
        if event.delta > 0:
            self.autoscroll_var.set(False)
        elif self.autoscroll_var.get():
            self.text.see("end")

    def _schedule_keyword_filter(self) -> None:
        if self._keyword_after_id is not None:
            try:
                self.after_cancel(self._keyword_after_id)
            except (tk.TclError, ValueError):
                pass
        self._keyword_after_id = self.after(_KEYWORD_DEBOUNCE_MS, self._apply_keyword_filter)

    def _apply_keyword_filter(self) -> None:
        self._keyword_after_id = None
        self.refresh_filter()

    def open_log_dir(self) -> None:
        """只打开目录；不创建运行产物，不读文件（12.5）。"""

        if self._log_path is None:
            self.notice_label.configure(text="尚未生成日志文件，无法打开日志目录")
            return
        directory = self._log_path.parent
        if not directory.is_dir():
            self.notice_label.configure(text=f"日志目录不存在：{directory}")
            return
        try:
            os.startfile(str(directory))  # type: ignore[attr-defined]
        except OSError as exc:
            self.notice_label.configure(text=f"打开日志目录失败：{exc}")

    # -- 内部 ---------------------------------------------------------
    def _minimum_level(self) -> str:
        return _LEVEL_BY_LABEL.get(self.level_var.get(), "INFO")

    def _passing(self, rows: Sequence[LogRow]) -> tuple[LogRow, ...]:
        return filter_log_rows(
            rows,
            minimum_level=self._minimum_level(),
            keyword=self.keyword_var.get(),
            only_battle=self.only_battle_var.get(),
            battle_id=self._battle_id,
        )

    def _trim_text_view(self) -> None:
        total_lines = int(self.text.index("end-1c").split(".")[0])
        row_lines = max(0, total_lines - 1)
        excess = row_lines - self.model.max_lines
        if excess > 0:
            self.text.delete("1.0", f"{excess + 1}.0")

    def _refresh_battle_hint(self) -> None:
        if self._battle_id is None:
            self.battle_hint.configure(text="场次未关联（ISS-010 未修复）", fg=widgets.WARN_ORANGE)
        else:
            self.battle_hint.configure(text=f"当前战斗 #{self._battle_id}", fg=widgets.SECONDARY)

    def _refresh_summary_labels(self) -> None:
        self.seq_label.configure(text=seq_gap_text(self.seq_tracker.gap_total, self._service_dropped))
        parts = []
        if self.model.trimmed_total:
            parts.append(f"视图已裁剪 {self.model.trimmed_total} 条最旧日志")
        if self.seq_tracker.gap_total:
            parts.append(f"事件序号缺失 {self.seq_tracker.gap_total} 条")
        if self._service_dropped:
            parts.append(f"服务事件队列丢弃 {self._service_dropped} 条")
        self.notice_label.configure(text="；".join(parts) + "（完整记录见日志文件）" if parts else "")


__all__ = ["LogPage"]
