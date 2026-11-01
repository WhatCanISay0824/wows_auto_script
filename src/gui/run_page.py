"""运行页与性能页（GUI 界面设计文档 §7.2/§7.5/§7.6/§8/§10.2/§11.1）。

运行页（M2）：

- 本次运行配置表单：客户端 profile、船名（本地档案匹配，不猜选）、账号别名、会话间轮换、
  本次场次上限（空=默认）、启动时先暂停、日志级别；“校验配置”调用服务 ``plan_preview``
  并在界面常驻展示 errors/warnings（不弹窗轰炸）；
- 非 ``STOPPED`` 时表单全部只读/禁用（11.8），不写任何配置文件；
- 运行状态严格四组 A 生命周期 / B 游戏流程 / C 暂停原因 / D 运行反馈；
- ``WAIT_ACCOUNT`` 顶部黄色等待面板：当前账号 → 下一账号 + 确认/结束按钮（10.2，无凭据输入）；
- 启动前展示 ``EffectivePlan`` 三类上限与生效值，未知值一律“—”。

性能页与右侧摘要共用 :func:`src.gui.viewmodel.performance_rows`。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import ttk

from src.gui import widgets
from src.gui.viewmodel import (
    DEFAULT_LOG_LEVEL_LABEL,
    F12_NOTICE,
    LOG_LEVEL_OPTIONS,
    NO_ACCOUNT_LABEL,
    ROTATION_NO_CREDENTIALS,
    ROTATION_WAIT_NOTE,
    ROTATION_WAIT_STEPS,
    ROTATION_WAIT_TITLE,
    SHIP_NAME_HINT,
    SHIP_SELECT_HINT,
    STOP_NOTICE,
    VALIDATION_DIRTY_TEXT,
    AccountOption,
    ProfileOption,
    ShipCandidate,
    StatusLine,
    effective_plan_rows,
    match_ship_candidates,
    overload_state_text,
    performance_detail_rows,
    performance_rows,
    performance_threshold_rows,
    plan_result_summary,
    rotation_wait_texts,
    run_status_groups,
    threshold_status_text,
    validation_issue_lines,
)
from src.runtime.messages import (
    CapabilityFlags,
    EffectivePlan,
    PerformanceView,
    PlanResult,
    RunLifecycle,
    RunSnapshot,
    StartOptions,
)

_GROUP_TITLES = (
    ("A", "A. 脚本生命周期"),
    ("B", "B. 游戏流程状态"),
    ("C", "C. 暂停原因"),
    ("D", "D. 运行反馈"),
)
_MATCH_DEBOUNCE_MS = 300


def _noop(*_args: object) -> None:
    return None


class RunPage(tk.Frame):
    """运行页：配置表单 + 等待面板 + 状态四组 + 生效值 + 运行控制说明。"""

    WARNING_BG = "#fdf3e3"

    def __init__(
        self,
        master: tk.Misc,
        *,
        on_validate: Callable[[], None] | None = None,
        on_import_ship: Callable[[], None] | None = None,
        on_profile_change: Callable[[str], None] | None = None,
        on_confirm_account_switch: Callable[[], None] | None = None,
        on_abort_rotation: Callable[[], None] | None = None,
        embedded_scroll: widgets.ScrollableFrame | None = None,
    ) -> None:
        super().__init__(master, bg=widgets.BG)
        self._on_validate = on_validate or _noop
        self._on_import_ship = on_import_ship or _noop
        self._on_profile_change = on_profile_change or _noop
        self._on_confirm_account_switch = on_confirm_account_switch or _noop
        self._on_abort_rotation = on_abort_rotation or _noop

        self._profile_labels: dict[str, str] = {}
        self._account_labels: dict[str, str | None] = {NO_ACCOUNT_LABEL: None}
        self._ships: tuple[ShipCandidate, ...] = ()
        self._selected_ship_id: str | None = None
        self._editable = True
        self._tool_running = False
        self._validate_enabled = True
        self._import_enabled = True
        self._rotation_available = False
        self._validation_result: PlanResult | None = None
        self._dirty = False
        self._match_after: str | None = None
        self._rotation_status = "预检状态：等待确认"

        scroll = embedded_scroll or widgets.ScrollableFrame(self)
        if embedded_scroll is None:
            scroll.pack(fill="both", expand=True)
        self.scroll = scroll
        body = self if embedded_scroll is not None else scroll.body
        self._build_wait_panel(body)
        self._build_config_section(body)
        self._build_plan_section(body)
        self._build_control_section(body)
        self._build_status_section(body)
        if embedded_scroll is not None:
            self.plan_section.pack_forget()
            self.control_section.pack_forget()
            self.status_section.pack_forget()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_wait_panel(self, body: tk.Misc) -> None:
        self.wait_panel = tk.Frame(
            body,
            bg=self.WARNING_BG,
            highlightbackground=widgets.BORDER,
            highlightthickness=1,
        )
        inner = tk.Frame(self.wait_panel, bg=self.WARNING_BG)
        inner.pack(fill="x", padx=12, pady=8)
        tk.Label(
            inner,
            text=ROTATION_WAIT_TITLE,
            bg=self.WARNING_BG,
            fg=widgets.WARN_ORANGE,
            font=widgets.font(11, bold=True),
            anchor="w",
        ).pack(fill="x")
        self.rotation_headline = tk.Label(
            inner,
            text="—",
            bg=self.WARNING_BG,
            fg=widgets.TEXT,
            font=widgets.font(10),
            anchor="w",
            justify="left",
            wraplength=980,
        )
        self.rotation_headline.pack(fill="x", pady=(2, 2))
        for text in (ROTATION_WAIT_STEPS, ROTATION_WAIT_NOTE, ROTATION_NO_CREDENTIALS):
            tk.Label(
                inner,
                text=text,
                bg=self.WARNING_BG,
                fg=widgets.SECONDARY,
                font=widgets.font(9),
                anchor="w",
                justify="left",
                wraplength=980,
            ).pack(fill="x")
        footer = tk.Frame(inner, bg=self.WARNING_BG)
        footer.pack(fill="x", pady=(6, 0))
        self.rotation_status_label = tk.Label(
            footer,
            text=self._rotation_status,
            bg=self.WARNING_BG,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
        )
        self.rotation_status_label.pack(side="left")
        self.rotation_abort_button = ttk.Button(footer, text="结束轮换", command=self._on_abort_rotation)
        self.rotation_abort_button.pack(side="right", padx=(6, 0))
        self.rotation_confirm_button = ttk.Button(
            footer, text="确认切号并继续", command=self._on_confirm_account_switch, style="Accent.TButton"
        )
        self.rotation_confirm_button.pack(side="right")
        self.wait_panel.pack_forget()

    def _build_config_section(self, body: tk.Misc) -> None:
        self.config_section = widgets.SectionFrame(body, "运行设置")
        self.config_section.pack(fill="x", padx=10, pady=(8, 4))
        content = self.config_section.content
        form = tk.Frame(content, bg=widgets.PANEL)
        form.pack(fill="x")
        for column in (0, 1):
            form.grid_columnconfigure(column, weight=1, uniform="field")

        def field(title, row, column):
            cell = tk.Frame(form, bg=widgets.PANEL)
            cell.grid(row=row, column=column, sticky="ew", padx=(0, 24) if column == 0 else 0,
                      pady=(0, 16))
            tk.Label(cell, text=title, bg=widgets.PANEL, fg=widgets.SECONDARY,
                     font=widgets.font(9), anchor="w").pack(fill="x", pady=(0, 7))
            return cell

        self.profile_var = tk.StringVar(value="")
        self.profile_combo = ttk.Combobox(field("客户端", 0, 0), textvariable=self.profile_var, state="readonly")
        self.profile_combo.pack(fill="x")
        self.profile_text = tk.Label(self.profile_combo.master, textvariable=self.profile_var,
                                    bg=widgets.PANEL, fg=widgets.TEXT, font=widgets.font(10), anchor="w")
        self.account_var = tk.StringVar(value="")
        self.account_combo = ttk.Combobox(field("账号", 0, 1), textvariable=self.account_var, state="readonly")
        self.account_combo.pack(fill="x")
        self.account_text = tk.Label(self.account_combo.master, textvariable=self.account_var,
                                    bg=widgets.PANEL, fg=widgets.TEXT, font=widgets.font(10), anchor="w")
        self.account_choices = tk.Frame(self.account_combo.master, bg=widgets.PANEL)
        self.max_battles_var = tk.StringVar(value="")
        self.max_battles_spin = ttk.Spinbox(field("本次场次上限", 1, 0), from_=1, to=999,
                                           textvariable=self.max_battles_var)
        self.max_battles_spin.pack(fill="x")
        self.log_level_var = tk.StringVar(value=DEFAULT_LOG_LEVEL_LABEL)
        self.log_level_combo = ttk.Combobox(field("日志级别", 1, 1), textvariable=self.log_level_var,
            values=(DEFAULT_LOG_LEVEL_LABEL, *LOG_LEVEL_OPTIONS), state="readonly")
        self.log_level_combo.pack(fill="x")
        options = tk.Frame(content, bg=widgets.PANEL)
        options.pack(fill="x", pady=(0, 22))
        self.start_paused_var = tk.BooleanVar(value=False)
        self.start_paused_check = ttk.Checkbutton(options, text="启动时暂停", variable=self.start_paused_var)
        self.start_paused_check.pack(side="left", padx=(0, 20))
        self.rotate_var = tk.BooleanVar(value=False)
        self.rotate_check = ttk.Checkbutton(options, text="会话间轮换", variable=self.rotate_var)
        self.rotate_check.pack(side="left")
        self.rotation_note = tk.Label(content, text="", bg=widgets.PANEL, font=widgets.font(9))
        self.safety_cap_label = tk.Label(options, text="会话上限：—", bg=widgets.PANEL,
                                         fg=widgets.SECONDARY, font=widgets.font(9))
        self.safety_cap_label.pack(side="right")

        tk.Label(content, text="船只", bg=widgets.PANEL, fg=widgets.TEXT,
                 font=widgets.font(10), anchor="w").pack(fill="x", pady=(0, 14))
        ship_form = tk.Frame(content, bg=widgets.PANEL)
        ship_form.pack(fill="x")
        ship_form.grid_columnconfigure(1, weight=1)
        left = tk.Frame(ship_form, bg=widgets.PANEL, width=widgets.px(320))
        left.grid(row=0, column=0, sticky="ew")
        for cell, title in ((left, "船名"),):
            tk.Label(cell, text=title, bg=widgets.PANEL, fg=widgets.SECONDARY,
                     font=widgets.font(9), anchor="w").pack(fill="x", pady=(0, 7))
        self.ship_name_var = tk.StringVar(value="")
        self.ship_name_entry = ttk.Combobox(left, textvariable=self.ship_name_var, width=26)
        self.ship_name_entry.pack(fill="x")
        self.ship_name_entry.bind("<<ComboboxSelected>>", self._on_name_selected)
        # 保留候选接口供匹配和歧义选择使用，不再单独展示第二个输入框。
        self.ship_candidate_combo = ttk.Combobox(ship_form, state="readonly")
        self.ship_candidate_combo.bind("<<ComboboxSelected>>", self._on_candidate_selected)
        self.ship_match_label = tk.Label(content, text="", bg=widgets.PANEL, fg=widgets.SECONDARY,
            font=widgets.font(9), anchor="w", wraplength=720, justify="left")
        self.ship_match_label.pack(fill="x", pady=(8, 0))
        self.ship_hint_label = tk.Label(content, text=f"{SHIP_NAME_HINT} {SHIP_SELECT_HINT}", bg=widgets.PANEL,
                                         fg=widgets.SECONDARY, font=widgets.font(9))
        validation_row = tk.Frame(content, bg=widgets.PANEL)
        validation_row.pack(fill="x", pady=(12, 0))
        self.validation_summary_label = tk.Label(validation_row, text=plan_result_summary(None),
            bg=widgets.PANEL, fg=widgets.SECONDARY, font=widgets.font(9), anchor="w")
        self.validation_summary_label.pack(side="left")
        self.validate_button = widgets.make_button(validation_row, "校验配置", self.request_validate)
        self.validate_button.pack(side="right")
        self.import_button = widgets.make_button(validation_row, "导入船卡", self.request_import)
        self.import_button.pack(side="right", padx=(0, 8))
        self.validation_text = tk.Text(content, height=3, wrap="word", bg=widgets.PANEL,
            fg=widgets.TEXT, font=widgets.font(9), relief="flat", state="disabled")
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        for var in (self.rotate_var, self.start_paused_var, self.max_battles_var,
                    self.log_level_var, self.ship_name_var):
            var.trace_add("write", lambda *_args: self.mark_dirty())
        self.account_combo.bind("<<ComboboxSelected>>", lambda _event: self.mark_dirty())
        self.ship_name_var.trace_add("write", lambda *_args: self._schedule_match())

    def _build_plan_section(self, body: tk.Misc) -> None:
        self.plan_section = widgets.SectionFrame(body, "生效值（启动前；来自校验/运行服务快照）")
        self.plan_section.pack(fill="x", padx=10, pady=4)
        self.plan_grid = widgets.KeyValueGrid(
            self.plan_section.content,
            columns=2,
            key_width=12,
            value_wraplength=250,
        )
        self.plan_grid.pack(fill="both", expand=True)

    def _build_control_section(self, body: tk.Misc) -> None:
        self.control_section = widgets.SectionFrame(body, "运行控制")
        self.control_section.pack(fill="x", padx=10, pady=4)
        self.f12_label = tk.Label(
            self.control_section.content,
            text=F12_NOTICE,
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
            justify="left",
            wraplength=980,
        )
        self.f12_label.pack(fill="x")
        self.stop_label = tk.Label(
            self.control_section.content,
            text=STOP_NOTICE,
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
            justify="left",
            wraplength=980,
        )
        self.stop_label.pack(fill="x", pady=(2, 0))

    def _build_status_section(self, body: tk.Misc) -> None:
        self.groups: dict[str, widgets.KeyValueGrid] = {}
        status_section = widgets.SectionFrame(body, "运行状态")
        self.status_section = status_section
        status_section.pack(fill="x", padx=10, pady=(4, 10))
        group_container = status_section.content
        for index, (group_id, title) in enumerate(_GROUP_TITLES):
            group_frame = tk.Frame(group_container, bg=widgets.PANEL)
            group_frame.grid(
                row=index // 2,
                column=index % 2,
                sticky="nsew",
                padx=(0, 14) if index % 2 == 0 else (0, 0),
                pady=(0, 8),
            )
            tk.Label(
                group_frame,
                text=title,
                bg=widgets.PANEL,
                fg=widgets.TEXT,
                font=widgets.font(10, bold=True),
                anchor="w",
            ).pack(fill="x")
            grid = widgets.KeyValueGrid(
                group_frame,
                columns=1,
                key_width=11,
                value_wraplength=250,
            )
            grid.pack(fill="x")
            self.groups[group_id] = grid
        group_container.grid_columnconfigure(0, weight=1)
        group_container.grid_columnconfigure(1, weight=1)

    def _small_label(self, parent: tk.Misc, text: str) -> tk.Label:
        label = tk.Label(parent, text=text, bg=widgets.PANEL, fg=widgets.SECONDARY, font=widgets.font(9))
        label.pack(side="left")
        return label

    # ------------------------------------------------------------------
    # 候选/表单
    # ------------------------------------------------------------------
    def set_catalogs(
        self,
        profiles: Sequence[ProfileOption],
        accounts: Sequence[AccountOption],
        *,
        rotation_available: bool,
        ships: Sequence[ShipCandidate],
    ) -> None:
        current_profile = self.selected_profile_id()
        self._profile_labels = {item.label: item.profile_id for item in profiles}
        self.profile_combo.configure(values=list(self._profile_labels))
        if current_profile and current_profile in self._profile_labels.values():
            label = next(key for key, value in self._profile_labels.items() if value == current_profile)
        else:
            label = next(iter(self._profile_labels), "")
        self.profile_combo.set(label)
        self._show_choice(self.profile_combo, self.profile_text, len(profiles) > 1)

        current_account = self.account_alias()
        self._account_labels = {NO_ACCOUNT_LABEL: None}
        for account in accounts:
            self._account_labels[account.label] = account.alias
        self.account_combo.configure(values=list(self._account_labels))
        account_label = next(
            (key for key, value in self._account_labels.items() if value == current_account),
            NO_ACCOUNT_LABEL,
        )
        self.account_combo.set(account_label)
        self._show_choice(self.account_combo, self.account_text, len(self._account_labels) > 1)
        for child in self.account_choices.winfo_children():
            child.destroy()
        self.account_choices.pack_forget()
        if 1 < len(self._account_labels) <= 4:
            self.account_combo.pack_forget()
            self.account_choices.pack(fill="x")
            for label in self._account_labels:
                button = ttk.Radiobutton(self.account_choices,
                    text="默认账号" if label == NO_ACCOUNT_LABEL else label,
                    variable=self.account_var, value=label, command=self.mark_dirty)
                button.pack(anchor="w", pady=(0, 2))
                button.configure(state="normal" if self._editable else "disabled")
        if len(self._account_labels) == 1:
            self.account_text.configure(textvariable="", text="默认账号")
        else:
            self.account_text.configure(textvariable=self.account_var)
        self._rotation_available = bool(rotation_available)
        self.rotate_check.configure(state="normal" if rotation_available else "disabled")
        if rotation_available:
            self.rotation_note.configure(text="")
        else:
            self.rotation_note.configure(
                text="accounts.yaml 未启用轮换或没有启用账号：GUI 不修改该文件",
            )
        self.set_ships(ships)

    def set_ships(self, ships: Sequence[ShipCandidate]) -> None:
        self._ships = tuple(ships)
        self.ship_name_entry.configure(values=[item.label for item in self._ships])
        self.ship_candidate_combo.configure(values=[item.label for item in self._ships])
        self._apply_ship_match()

    @staticmethod
    def _show_choice(combo, label, selectable):
        combo.pack_forget()
        label.pack_forget()
        (combo if selectable else label).pack(fill="x", pady=(0, 4))

    def _on_name_selected(self, _event=None):
        candidate = next((item for item in self._ships if item.label == self.ship_name_var.get()), None)
        if candidate is None:
            return
        self.ship_name_var.set(candidate.display_name)
        self._selected_ship_id = candidate.ship_id
        self._apply_ship_match()

    def set_ship_name(self, name: str) -> None:
        self.ship_name_var.set(name)

    def selected_profile_id(self) -> str | None:
        return self._profile_labels.get(self.profile_combo.get())

    def account_alias(self) -> str | None:
        return self._account_labels.get(self.account_combo.get())

    def rotate_enabled(self) -> bool:
        return bool(self.rotate_var.get()) and getattr(self, "_rotation_available", False)

    def selected_ship_id(self) -> str | None:
        return self._selected_ship_id

    def ship_name(self) -> str:
        return self.ship_name_var.get().strip()

    def start_paused(self) -> bool:
        return bool(self.start_paused_var.get())

    def log_level(self) -> str | None:
        value = self.log_level_var.get()
        return None if value == DEFAULT_LOG_LEVEL_LABEL else value

    def max_battles(self) -> int | None:
        text = self.max_battles_var.get().strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError as exc:
            raise ValueError(f"本次场次上限必须是正整数（或留空使用默认）：{text!r}") from exc
        if value <= 0:
            raise ValueError(f"本次场次上限必须 > 0：{value}")
        return value

    def build_start_options(self, project_root: Path) -> StartOptions:
        return StartOptions(
            root=Path(project_root),
            profile_id=self.selected_profile_id(),
            ship_name=self.ship_name() or None,
            ship_id=self._selected_ship_id,
            account_alias=self.account_alias(),
            rotate=self.rotate_enabled(),
            max_battles=self.max_battles(),
            log_level=self.log_level(),
            start_paused=self.start_paused(),
        )

    def _on_profile_selected(self, _event: object = None) -> None:
        profile_id = self.selected_profile_id()
        if profile_id:
            self._selected_ship_id = None
            self._on_profile_change(profile_id)

    def _on_candidate_selected(self, _event: object = None) -> None:
        label = self.ship_candidate_combo.get()
        candidate = next((item for item in self._ships if item.label == label), None)
        if candidate is None:
            return
        self.ship_name_var.set(candidate.display_name)
        self._selected_ship_id = candidate.ship_id
        self.ship_match_label.configure(text=f"已选择候选：{candidate.label}")

    def _schedule_match(self) -> None:
        if self._match_after is not None:
            try:
                self.after_cancel(self._match_after)
            except (tk.TclError, ValueError):
                pass
        self._match_after = self.after(_MATCH_DEBOUNCE_MS, self._apply_ship_match)

    def _apply_ship_match(self) -> None:
        self._match_after = None
        name = self.ship_name()
        match = match_ship_candidates(self._ships, name)
        selected = next((item for item in match.matches if item.ship_id == self._selected_ship_id), None)
        if match.status == "partial_multiple" and selected is not None:
            self.ship_match_label.configure(text=selected.label, fg=widgets.SECONDARY)
            return
        emphasis = {"exact": widgets.OK_GREEN, "none": widgets.WARN_ORANGE}.get(match.status, widgets.SECONDARY)
        self.ship_match_label.configure(text="" if match.status == "empty" else match.message, fg=emphasis)
        if match.status == "empty":
            self._selected_ship_id = None
            self.ship_candidate_combo.configure(values=[item.label for item in self._ships])
            self.ship_candidate_combo.set("")
            return
        if match.status in ("exact", "partial_unique"):
            self.ship_candidate_combo.configure(values=[match.matches[0].label])
            self.ship_candidate_combo.set(match.matches[0].label)
            self._selected_ship_id = match.matches[0].ship_id
            return
        if match.status == "partial_multiple":
            self.ship_candidate_combo.configure(values=[item.label for item in match.matches])
            self.ship_candidate_combo.set("")
            self._selected_ship_id = None
            return
        # 未登记：仍列出全部已登记船，用户可改选；不自动选任何一项。
        self.ship_candidate_combo.configure(values=[item.label for item in self._ships])
        self.ship_candidate_combo.set("")
        self._selected_ship_id = None

    # ------------------------------------------------------------------
    # 校验与只读状态
    # ------------------------------------------------------------------
    def set_validation(self, result: PlanResult | None, *, dirty: bool = False) -> None:
        self._validation_result = result
        self._dirty = dirty
        self._render_validation()

    def mark_dirty(self) -> None:
        self._dirty = True
        self._render_validation()

    def _render_validation(self) -> None:
        if self._dirty:
            text = VALIDATION_DIRTY_TEXT
            color = widgets.WARN_ORANGE
        else:
            text = plan_result_summary(self._validation_result)
            if self._validation_result is None:
                color = widgets.SECONDARY
            elif self._validation_result.ok:
                color = widgets.OK_GREEN
            else:
                color = widgets.ERROR_RED
        self.validation_summary_label.configure(text=text, fg=color)
        lines = validation_issue_lines(self._validation_result)
        self.validation_text.configure(state="normal")
        self.validation_text.delete("1.0", "end")
        if lines:
            self.validation_text.insert("end", "\n".join(lines))
        self.validation_text.configure(state="disabled")
        if lines:
            self.validation_text.pack(fill="x", pady=(4, 0))
        else:
            self.validation_text.pack_forget()
        if self._validation_result is not None and self._validation_result.plan is not None:
            self.safety_cap_label.configure(
                text=f"会话安全上限：{self._validation_result.plan.safety_battle_cap} 场"
            )

    def set_config_editable(self, editable: bool) -> None:
        self._editable = bool(editable)
        self._apply_editable()

    def set_import_enabled(self, enabled: bool) -> None:
        self._import_enabled = bool(enabled)
        self._apply_editable()

    def set_tool_running(self, running: bool) -> None:
        self._tool_running = bool(running)
        self._apply_editable()

    def set_validate_enabled(self, enabled: bool) -> None:
        self._validate_enabled = bool(enabled)
        self.validate_button.configure(state="normal" if self._validate_enabled else "disabled")

    def _apply_editable(self) -> None:
        widget_state = "normal" if self._editable else "disabled"
        combo_state = "readonly" if self._editable else "disabled"
        self.profile_combo.configure(state=combo_state)
        self.account_combo.configure(state=combo_state)
        for button in self.account_choices.winfo_children():
            button.configure(state=widget_state)
        self.rotate_check.configure(
            state=("normal" if self._editable and getattr(self, "_rotation_available", False) else "disabled")
        )
        self.ship_name_entry.configure(state=widget_state)
        self.ship_candidate_combo.configure(state=combo_state)
        self.max_battles_spin.configure(state=widget_state)
        self.start_paused_check.configure(state=widget_state)
        self.log_level_combo.configure(state=combo_state)
        import_enabled = self._editable and not self._tool_running and self._import_enabled
        self.import_button.configure(state="normal" if import_enabled else "disabled")
        self.ship_hint_label.configure(
            text=(f"{SHIP_NAME_HINT} {SHIP_SELECT_HINT}" if not self._tool_running else "标定工具运行中，暂不能导入船卡。")
        )

    def request_validate(self) -> None:
        if not self._validate_enabled:
            return
        try:
            self._on_validate()
        except Exception:  # noqa: BLE001 - 回调异常由 App 上报
            pass

    def request_import(self) -> None:
        if self._editable and not self._tool_running:
            self._on_import_ship()

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render(
        self,
        snapshot: RunSnapshot | None,
        capabilities: CapabilityFlags | None = None,
        *,
        plan: EffectivePlan | None = None,
    ) -> None:
        groups = run_status_groups(snapshot)
        for group_id, grid in self.groups.items():
            grid.set_rows(groups.get(group_id, ()))
        self.plan_grid.set_rows(effective_plan_rows(plan))
        self._render_control_notice(capabilities)
        self.render_rotation(snapshot)

    def set_plan(self, plan: EffectivePlan | None) -> None:
        self.plan_grid.set_rows(effective_plan_rows(plan))

    def _render_control_notice(self, capabilities: CapabilityFlags | None) -> None:
        if capabilities is not None and capabilities.input_gate:
            extra = "F12 由安全监控线程直接关闭输入门禁（目标：主循环阻塞时 ≤1 s 释放）"
        else:
            extra = "F12 处理依赖主循环同步（ISS-005 未修复），主循环阻塞时可能延迟"
        self.f12_label.configure(text=f"{F12_NOTICE}；{extra}")

    # ------------------------------------------------------------------
    # M3 等待面板（10.2）
    # ------------------------------------------------------------------
    def render_rotation(self, snapshot: RunSnapshot | None, *, status: str | None = None) -> None:
        lifecycle = snapshot.lifecycle.lifecycle if snapshot is not None else None
        if lifecycle is not RunLifecycle.WAIT_ACCOUNT:
            if self.wait_panel.winfo_manager():
                self.wait_panel.pack_forget()
                self._rotation_status = "预检状态：等待确认"
            return
        headline, steps, note = rotation_wait_texts(snapshot)
        self.rotation_headline.configure(text=headline)
        if status is not None:
            self._rotation_status = status
        self.rotation_status_label.configure(text=self._rotation_status)
        if not self.wait_panel.winfo_manager():
            self.wait_panel.pack(fill="x", padx=10, pady=(8, 4), before=self.config_section)
            self.scroll.scroll_to_top()

    def set_rotation_status(self, status: str) -> None:
        self._rotation_status = status
        self.rotation_status_label.configure(text=status)

    @property
    def rotation_status(self) -> str:
        return self._rotation_status


class PerformancePage(tk.Frame):
    """性能详情：系统摘要、真实趋势、脚本开销与保护规则。"""

    def __init__(self, master: tk.Misc) -> None:
        from src.gui.performance_chart import CpuHistoryChart

        super().__init__(master, bg=widgets.BG)
        self.scroll = widgets.ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.body
        self.metrics_section = widgets.SectionFrame(body, "电脑性能")
        self.metrics_section.pack(fill="x", padx=12, pady=(8, 0))
        self.metrics_grid = widgets.KeyValueGrid(body, columns=2)
        summary = self.metrics_section.content
        self.summary_values = []
        for index, title in enumerate(("全机 CPU", "物理内存", "可用内存")):
            summary.grid_columnconfigure(index, weight=1, uniform="summary")
            cell = tk.Frame(summary, bg=widgets.PANEL)
            cell.grid(row=0, column=index, sticky="ew", pady=(4, 8))
            tk.Label(cell, text=title, bg=widgets.PANEL, fg=widgets.SECONDARY,
                     font=widgets.font(9), anchor="w").pack(fill="x")
            label = tk.Label(cell, text="—", bg=widgets.PANEL, fg=widgets.TEXT,
                             font=widgets.font(22), anchor="w")
            label.pack(fill="x", pady=(10, 0))
            self.summary_values.append(label)
        chart_section = widgets.SectionFrame(body, "最近 60 秒")
        chart_section.pack(fill="x", padx=12, pady=(4, 0))
        self.cpu_chart = CpuHistoryChart(chart_section.content)
        self.cpu_chart.pack(fill="x")
        usage = widgets.SectionFrame(body, "脚本开销")
        usage.pack(fill="x", padx=12, pady=(4, 0))
        self.usage_grid = widgets.KeyValueGrid(usage.content, columns=1, key_width=18, value_wraplength=480)
        self.usage_grid.pack(fill="x")
        self.threshold_section = widgets.SectionFrame(body, "过载保护")
        self.threshold_section.pack(fill="x", padx=12, pady=(4, 16))
        self.threshold_label = tk.Label(self.threshold_section.content, text="—", bg=widgets.PANEL,
            fg=widgets.SECONDARY, font=widgets.font(9), anchor="w")
        self.threshold_label.pack(fill="x")
        self.overload_label = tk.Label(self.threshold_section.content, text="—", bg=widgets.PANEL,
            fg=widgets.SECONDARY, font=widgets.font(9), anchor="w", wraplength=700, justify="left")
        self.overload_label.pack(fill="x", pady=(4, 8))
        self.threshold_grid = widgets.KeyValueGrid(self.threshold_section.content, columns=2,
                                                   key_width=15, value_wraplength=240)
        self.threshold_grid.pack(fill="x")

    def render(self, perf: PerformanceView | None, capabilities: CapabilityFlags | None = None,
               pending_actions: int | None = None) -> None:
        rows = performance_rows(perf, capabilities) + performance_detail_rows(perf, capabilities)
        self.metrics_grid.set_rows(rows)
        self.usage_grid.set_rows(tuple(row for row in rows if row.key in
            ("script_private", "capture_ms", "loop_tick", "script_cpu")) +
            (StatusLine("pending", "输入队列", str(pending_actions) if pending_actions is not None else "—"),))
        self.threshold_label.configure(text=threshold_status_text(perf, capabilities))
        self.threshold_grid.set_rows(performance_threshold_rows(perf.thresholds if perf else None))
        if perf is None:
            return
        self.cpu_chart.update_sample(perf.sampled_at, perf.cpu_pct)
        memory_pct = (100 * (1 - perf.mem_available_mb / perf.mem_total_mb)
                      if perf.mem_total_mb and perf.mem_available_mb is not None else None)
        for label, value in zip(self.summary_values[:2], (perf.cpu_pct, memory_pct), strict=True):
            label.configure(text=f"{value:.0f}%" if value is not None else "—")
        self.summary_values[2].configure(text=f"{perf.mem_available_mb/1024:.1f} GiB" if perf.mem_available_mb is not None else "—")
        overload = (f"监测不可用：{perf.unavailable_reason}" if perf.unavailable_reason else
                    overload_state_text(perf.overload_state))
        if perf.overload_reason or perf.overload_detail:
            overload += f"：{perf.overload_reason or perf.overload_detail}"
        self.overload_label.configure(text=f"当前状态：{overload}")


__all__ = ["PerformancePage", "RunPage"]
