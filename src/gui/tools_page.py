"""设置页（``tools_page.py``）：显示 + 船只/船卡 + 标定工具 + 只读生效值 + 能力开关。

M2 增加：

- 船只与船卡：已登记船只列表 + “导入船卡…”入口（仅 ``STOPPED`` 且无工具运行时可用）；
- 标定工具：工具表 + 运行按钮；子进程使用**当前解释器** ``sys.executable -m src.tools.<tool>``，
  同一时间只允许一个工具；每 1000ms ``after()`` 轮询退出码；退出后自动跑
  ``src.tools.validate --json`` 并展示 errors/warnings；
- 工具运行中通过 ``on_tool_running`` 回调让 App 禁用“启动托管”（不阻塞 GUI 主线程）；
- 全屏地图标定只显示说明与状态，不声称已支持自动航行（§9.6）。

全屏开关保持 M1 口径：``ttk.Checkbutton`` 勾选立即 ``root.attributes('-fullscreen', True/False)``，
纯内存态，不写 ``config.yaml``。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import ttk

from src.gui import widgets
from src.gui.viewmodel import (
    FULLMAP_CALIBRATION_NOTE,
    FULLSCREEN_HINT,
    TOOL_FAILED_TEXT,
    TOOL_FINISHED_TEXT,
    TOOL_SPECS,
    TOOL_VALIDATE_RUNNING_TEXT,
    ShipCandidate,
    capability_rows,
    effective_plan_rows,
    tool_arguments,
)
from src.runtime.messages import CapabilityFlags, EffectivePlan

TOOL_POLL_MS = 1000


def _noop(*_args: object) -> None:
    return None


class SettingsPage(tk.Frame):
    """设置页：全屏显示 + 船只/船卡 + 标定工具 + 只读生效值 + 能力开关。"""

    POLL_INTERVAL_MS = TOOL_POLL_MS

    def __init__(
        self,
        master: tk.Misc,
        *,
        on_fullscreen: Callable[[bool], None] | None = None,
        on_tool_running: Callable[[bool], None] | None = None,
        on_import_ship: Callable[[], None] | None = None,
        on_validate: Callable[[list[dict[str, str]], list[dict[str, str]], int], None] | None = None,
        project_root: Path | None = None,
        context_provider: Callable[[], tuple[str | None, str | None, str | None]] | None = None,
        popen_factory: Callable[..., object] | None = None,
        auto_poll: bool = True,
    ) -> None:
        super().__init__(master, bg=widgets.BG)
        self._on_fullscreen = on_fullscreen
        self._on_tool_running = on_tool_running or _noop
        self._on_import_ship = on_import_ship or _noop
        self._on_validate = on_validate or _noop
        self.project_root = Path(project_root) if project_root is not None else Path.cwd()
        self._context_provider = context_provider or (lambda: (None, None, None))
        self._popen_factory = popen_factory or subprocess.Popen
        self._auto_poll = bool(auto_poll)

        self._ships: tuple[ShipCandidate, ...] = ()
        self._tool_allowed = True
        self._import_enabled = True
        self._running_tool: str | None = None
        self._tool_proc: object | None = None
        self._validate_proc: object | None = None
        self._poll_after: str | None = None
        self._busy_notified = False
        self.launched_commands: list[tuple[str, ...]] = []
        self._last_validate: tuple[list[dict[str, str]], list[dict[str, str]], int] | None = None

        scroll = widgets.ScrollableFrame(self)
        self.scroll = scroll
        scroll.pack(fill="both", expand=True)
        body = tk.Frame(scroll.body, bg=widgets.BG)
        body.pack(anchor="nw", fill="x")
        scroll.body.bind("<Configure>", lambda event: body.pack_configure(
            padx=(0, max(0, event.width - widgets.px(1000)))), add="+")
        self.config_container = tk.Frame(body, bg=widgets.BG)
        self.config_container.pack(fill="x")
        self._build_tools_section(body)
        self._build_display_section(body)
        self._advanced = tk.Frame(body, bg=widgets.PANEL)
        self._advanced_button = ttk.Button(body, text="诊断与生效值", command=self._toggle_advanced)
        self._advanced_button.pack(anchor="w", padx=22, pady=(8, 16))
        self._build_ships_section(self._advanced)
        self._build_validation_section(self._advanced)
        self._build_plan_section(self._advanced)
        self._build_capability_section(self._advanced)

        self._apply_import_state()
        self._apply_tool_states()

    def _toggle_advanced(self):
        if self._advanced.winfo_manager():
            self._advanced.pack_forget()
        else:
            self._advanced.pack(fill="x")

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_display_section(self, body: tk.Misc) -> None:
        display_section = widgets.SectionFrame(body, "显示")
        display_section.pack(fill="x", padx=10, pady=(8, 4))
        self.fullscreen_var = tk.BooleanVar(value=False)
        self.fullscreen_check = ttk.Checkbutton(
            display_section.content,
            text="全屏显示",
            variable=self.fullscreen_var,
            command=self._on_fullscreen_clicked,
        )
        self.fullscreen_check.pack(anchor="w")
        self.fullscreen_hint = tk.Label(
            display_section.content,
            text=FULLSCREEN_HINT,
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
            justify="left",
            wraplength=1000,
        )

    def _build_ships_section(self, body: tk.Misc) -> None:
        ships_section = widgets.SectionFrame(body, "船只与船卡")
        ships_section.pack(fill="x", padx=10, pady=4)
        content = ships_section.content
        hint = tk.Label(
            content,
            text="只输入名称；新船首次需导入一次船卡（本机图片，不上传；只框船体剪影+船名）。",
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
            justify="left",
            wraplength=1000,
        )
        hint.pack(fill="x")
        row = tk.Frame(content, bg=widgets.PANEL)
        row.pack(fill="both", expand=True, pady=(4, 0))
        self.ships_table = ttk.Treeview(
            row,
            columns=("ship_id", "display_name"),
            show="headings",
            height=3,
        )
        self.ships_table.heading("ship_id", text="船号")
        self.ships_table.heading("display_name", text="船名")
        self.ships_table.column("ship_id", width=180, anchor="w")
        self.ships_table.column("display_name", width=360, anchor="w")
        self.ships_table.pack(side="left", fill="both", expand=True)
        self.ships_table.configure(selectmode="none")
        self.import_ship_button = widgets.make_button(row, "导入船卡…", self.request_import)
        self.import_ship_button.pack(side="right", padx=(8, 0), anchor="n")
        self.import_hint = tk.Label(
            content,
            text="导入仅在“已停止”且没有标定工具运行时可用；保存后请重新校验配置。",
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
            justify="left",
            wraplength=1000,
        )
        self.import_hint.pack(fill="x", pady=(4, 0))

    def _build_tools_section(self, body: tk.Misc) -> None:
        tools_section = widgets.SectionFrame(body, "标定工具")
        tools_section.pack(fill="x", padx=10, pady=4)
        content = tools_section.content
        self.tool_buttons: dict[str, ttk.Button] = {}
        self.tool_status_labels: dict[str, tk.Label] = {}
        for spec in TOOL_SPECS:
            row = tk.Frame(content, bg=widgets.PANEL)
            row.pack(fill="x", pady=2)
            tk.Label(
                row,
                text=spec.title,
                bg=widgets.PANEL,
                fg=widgets.TEXT,
                font=widgets.font(10),
                width=14,
                anchor="w",
            ).pack(side="left")
            tk.Label(
                row,
                text=spec.purpose,
                bg=widgets.PANEL,
                fg=widgets.SECONDARY,
                font=widgets.font(9),
                anchor="w",
                justify="left",
            )
            status = tk.Label(
                row,
                text="未运行",
                bg=widgets.PANEL,
                fg=widgets.SECONDARY,
                font=widgets.font(9),
                anchor="e",
            )
            status.pack(side="right", padx=(12, 12))
            button = widgets.make_button(row, "运行", lambda tool_id=spec.tool_id: self.start_tool(tool_id))
            button.pack(side="right")
            self.tool_buttons[spec.tool_id] = button
            self.tool_status_labels[spec.tool_id] = status

        fullmap_row = tk.Frame(content, bg=widgets.PANEL)
        fullmap_row.pack(fill="x", pady=(6, 0))
        tk.Label(
            fullmap_row,
            text="全屏地图标定",
            bg=widgets.PANEL,
            fg=widgets.TEXT,
            font=widgets.font(10),
            width=14,
            anchor="w",
        ).pack(side="left")
        self.fullmap_status = tk.Label(
            fullmap_row,
            text="未标定（当前不承诺自动航行）",
            bg=widgets.PANEL,
            fg=widgets.WARN_ORANGE,
            font=widgets.font(9),
            anchor="w",
        )
        self.fullmap_status.pack(side="left", fill="x", expand=True)
        self.fullmap_button = widgets.make_button(fullmap_row, "说明", self.show_fullmap_note)
        self.fullmap_button.pack(side="right")
        self.fullmap_note_label = tk.Label(
            content,
            text="",
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
            justify="left",
            wraplength=1000,
        )
        self.fullmap_note_label.pack(fill="x", pady=(2, 0))

        self.tool_result_label = tk.Label(
            content,
            text="",
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9),
            anchor="w",
            justify="left",
            wraplength=1000,
        )
        self.tool_result_label.pack(fill="x", pady=(4, 0))

    def _build_validation_section(self, body: tk.Misc) -> None:
        validation_section = widgets.SectionFrame(body, "工具退出后的配置校验（只读）")
        validation_section.pack(fill="x", padx=10, pady=4)
        self.validate_summary = tk.Label(
            validation_section.content,
            text="尚未校验",
            bg=widgets.PANEL,
            fg=widgets.SECONDARY,
            font=widgets.font(9, bold=True),
            anchor="w",
        )
        self.validate_summary.pack(fill="x")
        self.validate_text = tk.Text(
            validation_section.content,
            height=3,
            wrap="word",
            bg=widgets.PANEL,
            fg=widgets.TEXT,
            font=widgets.font(9),
            relief="flat",
            state="disabled",
        )
        self.validate_text.pack(fill="x", pady=(2, 0))

    def _build_plan_section(self, body: tk.Misc) -> None:
        plan_section = widgets.SectionFrame(body, "生效值（只读；来自运行服务快照/校验）")
        plan_section.pack(fill="x", padx=10, pady=4)
        self.plan_grid = widgets.KeyValueGrid(
            plan_section.content,
            columns=2,
            key_width=12,
            value_wraplength=230,
        )
        self.plan_grid.pack(fill="both", expand=True)

    def _build_capability_section(self, body: tk.Misc) -> None:
        capability_section = widgets.SectionFrame(body, "能力开关（未接入能力不承诺）")
        capability_section.pack(fill="x", padx=10, pady=(4, 10))
        self.capability_grid = widgets.KeyValueGrid(
            capability_section.content,
            columns=2,
            key_width=17,
            value_wraplength=230,
        )
        self.capability_grid.pack(fill="both", expand=True)

    # ------------------------------------------------------------------
    # 全屏开关（M1 口径保持不变）
    # ------------------------------------------------------------------
    def _on_fullscreen_clicked(self) -> None:
        self.apply_fullscreen(bool(self.fullscreen_var.get()))

    def apply_fullscreen(self, enabled: bool) -> None:
        self.fullscreen_var.set(bool(enabled))
        if self._on_fullscreen is not None:
            self._on_fullscreen(bool(enabled))

    @property
    def fullscreen(self) -> bool:
        return bool(self.fullscreen_var.get())

    # ------------------------------------------------------------------
    # 船只/船卡
    # ------------------------------------------------------------------
    def set_ships(self, ships: Sequence[ShipCandidate]) -> None:
        self._ships = tuple(ships)
        for item in self.ships_table.get_children():
            self.ships_table.delete(item)
        for ship in self._ships:
            self.ships_table.insert("", "end", values=(ship.ship_id, ship.display_name))

    def set_import_enabled(self, enabled: bool) -> None:
        self._import_enabled = bool(enabled)
        self._apply_import_state()

    def _apply_import_state(self) -> None:
        enabled = self._import_enabled and not self.tool_running
        self.import_ship_button.configure(state="normal" if enabled else "disabled")

    def request_import(self) -> None:
        if self._import_enabled and not self.tool_running:
            self._on_import_ship()

    def show_fullmap_note(self) -> None:
        self.fullmap_note_label.configure(text=FULLMAP_CALIBRATION_NOTE)

    # ------------------------------------------------------------------
    # 标定工具子进程
    # ------------------------------------------------------------------
    @property
    def tool_running(self) -> bool:
        return self._running_tool is not None or self._validate_proc is not None

    @property
    def running_tool(self) -> str | None:
        return self._running_tool

    @property
    def last_validate(self) -> tuple[list[dict[str, str]], list[dict[str, str]], int] | None:
        return self._last_validate

    def set_tool_allowed(self, allowed: bool) -> None:
        self._tool_allowed = bool(allowed)
        self._apply_tool_states()

    def _apply_tool_states(self) -> None:
        busy = self.tool_running
        for _tool_id, button in self.tool_buttons.items():
            allowed = self._tool_allowed and not busy
            button.configure(state="normal" if allowed else "disabled")
        self._apply_import_state()

    def start_tool(self, tool_id: str) -> bool:
        if self.tool_running:
            self._set_tool_result("已有工具在运行：同一时间只允许一个标定/校验工具。")
            return False
        if not self._tool_allowed:
            self._set_tool_result("请先停止脚本并等待清理完成，再运行标定工具。")
            return False
        profile_id, ship_id, account_alias = self._context_provider()
        try:
            arguments = tool_arguments(
                tool_id,
                root=self.project_root,
                profile_id=profile_id,
                ship_id=ship_id,
                account_alias=account_alias,
            )
        except ValueError as exc:
            self._set_tool_result(str(exc))
            return False
        argv = [sys.executable, *arguments]
        self.launched_commands.append(tuple(argv))
        try:
            proc = self._popen_factory(
                argv,
                cwd=str(self.project_root),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            )
        except OSError as exc:
            self._set_tool_status(tool_id, f"启动失败：{exc}")
            self._set_tool_result(f"工具启动失败：{exc}")
            return False
        self._running_tool = tool_id
        self._tool_proc = proc
        self._set_tool_status(tool_id, "运行中…")
        self._set_tool_result(f"已启动 {tool_id}（同一时间只允许一个工具；不会阻塞界面）。")
        self._notify_busy()
        self._apply_tool_states()
        self._schedule_poll()
        return True

    def poll_once(self) -> None:
        """执行一次轮询（测试直接调用；GUI 由 after(1000) 驱动）。"""

        if self._tool_proc is not None:
            try:
                code = self._tool_proc.poll()
            except Exception as exc:  # noqa: BLE001 - 子进程对象异常不应拖垮 GUI
                self._finish_tool(None, error=f"{type(exc).__name__}: {exc}")
                return
            if code is None:
                self._schedule_poll()
                return
            self._finish_tool(int(code))
            return
        if self._validate_proc is not None:
            try:
                code = self._validate_proc.poll()
            except Exception as exc:  # noqa: BLE001
                self._finish_validate(None, error=f"{type(exc).__name__}: {exc}")
                return
            if code is None:
                self._schedule_poll()
                return
            self._finish_validate(int(code))
            return
        self._cancel_poll()

    def _schedule_poll(self) -> None:
        self._cancel_poll()
        if not self._auto_poll:
            return
        try:
            self._poll_after = self.after(self.POLL_INTERVAL_MS, self._poll_tick)
        except tk.TclError:
            self._poll_after = None

    def _poll_tick(self) -> None:
        self._poll_after = None
        self.poll_once()

    def _cancel_poll(self) -> None:
        if self._poll_after is not None:
            try:
                self.after_cancel(self._poll_after)
            except (tk.TclError, ValueError):
                pass
            self._poll_after = None

    def shutdown_timers(self) -> None:
        """GUI 退出前取消 after 轮询（不结束、不强杀工具子进程）。"""

        self._cancel_poll()

    def _finish_tool(self, exit_code: int | None, *, error: str | None = None) -> None:
        tool_id = self._running_tool
        self._tool_proc = None
        self._running_tool = None
        if tool_id is not None:
            if error is not None:
                self._set_tool_status(tool_id, f"失败：{error}")
                self._set_tool_result(f"工具异常退出：{error}；请查看工具窗口/终端输出。")
            elif exit_code == 0:
                self._set_tool_status(tool_id, "已完成（退出码 0）")
                self._set_tool_result(TOOL_FINISHED_TEXT)
            else:
                self._set_tool_status(tool_id, TOOL_FAILED_TEXT.format(code=exit_code))
                self._set_tool_result(
                    TOOL_FAILED_TEXT.format(code=exit_code)
                    + "；标定工具自身会打印原因，可查看工具窗口/终端或 logs/ 下日志。"
                )
        self._start_validate()
        self._apply_tool_states()

    def _start_validate(self) -> None:
        profile_id, ship_id, account_alias = self._context_provider()
        try:
            arguments = tool_arguments(
                "validate",
                root=self.project_root,
                profile_id=profile_id,
                ship_id=ship_id,
                account_alias=account_alias,
                json_output=True,
            )
        except ValueError as exc:  # pragma: no cover - 固定工具名不会失败
            self._set_tool_result(f"校验参数错误：{exc}")
            return
        argv = [sys.executable, *arguments]
        self.launched_commands.append(tuple(argv))
        self.validate_summary.configure(text=TOOL_VALIDATE_RUNNING_TEXT, fg=widgets.SECONDARY)
        try:
            proc = self._popen_factory(
                argv,
                cwd=str(self.project_root),
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._finish_validate(None, error=f"{type(exc).__name__}: {exc}")
            return
        self._validate_proc = proc
        self._notify_busy()
        self._apply_tool_states()
        self._schedule_poll()

    def _finish_validate(self, exit_code: int | None, *, error: str | None = None) -> None:
        proc = self._validate_proc
        self._validate_proc = None
        stdout = stderr = ""
        if proc is not None:
            try:
                stdout, stderr = proc.communicate(timeout=10)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                error = error or f"读取校验输出失败：{type(exc).__name__}: {exc}"
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        if error is None:
            try:
                payload = json.loads(stdout or "{}")
                errors = [dict(item) for item in payload.get("errors", []) if isinstance(item, dict)]
                warnings = [dict(item) for item in payload.get("warnings", []) if isinstance(item, dict)]
            except (json.JSONDecodeError, AttributeError, TypeError) as exc:
                error = f"校验输出解析失败（退出码 {exit_code}）：{exc}"
        if error is not None:
            self.validate_summary.configure(text=f"校验失败：{error}", fg=widgets.ERROR_RED)
            self._fill_validate_text((f"【错误】{error}",))
            self._last_validate = ([], [], exit_code if exit_code is not None else -1)
            self._on_validate(
                [{"code": "validate_error", "location": "src.tools.validate", "message": error}],
                [],
                exit_code if exit_code is not None else -1,
            )
        else:
            mark = "✔" if not errors else "✗"
            summary = (
                f"校验：{mark} {len(errors)} error / {len(warnings)} warning（退出码 {exit_code}）"
            )
            self.validate_summary.configure(
                text=summary, fg=widgets.OK_GREEN if not errors else widgets.ERROR_RED
            )
            lines = [
                f"【错误】{item.get('location', '?')}：{item.get('message', '')}" for item in errors
            ]
            lines.extend(
                f"【警告】{item.get('location', '?')}：{item.get('message', '')}" for item in warnings
            )
            if not lines and stderr.strip():
                lines.append(f"【提示】{stderr.strip().splitlines()[-1]}")
            self._fill_validate_text(tuple(lines))
            self._last_validate = (errors, warnings, int(exit_code or 0))
            self._on_validate(errors, warnings, int(exit_code or 0))
        self._notify_busy()
        self._apply_tool_states()
        self._cancel_poll()

    def _fill_validate_text(self, lines: Sequence[str]) -> None:
        self.validate_text.configure(state="normal")
        self.validate_text.delete("1.0", "end")
        if lines:
            self.validate_text.insert("end", "\n".join(lines))
        self.validate_text.configure(state="disabled")

    def _set_tool_status(self, tool_id: str, text: str) -> None:
        label = self.tool_status_labels.get(tool_id)
        if label is not None:
            label.configure(text=text, fg=widgets.OK_GREEN if "已完成" in text else widgets.SECONDARY)

    def _set_tool_result(self, text: str) -> None:
        self.tool_result_label.configure(text=text)

    def _notify_busy(self) -> None:
        busy = self.tool_running
        if busy == self._busy_notified:
            return
        self._busy_notified = busy
        try:
            self._on_tool_running(busy)
        except Exception:  # noqa: BLE001 - 回调异常由 App 上报
            pass

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render(
        self,
        plan: EffectivePlan | None,
        capabilities: CapabilityFlags | None,
    ) -> None:
        self.plan_grid.set_rows(effective_plan_rows(plan))
        self.capability_grid.set_rows(capability_rows(capabilities))
        if capabilities is not None and capabilities.autopilot_full_map:
            self.fullmap_status.configure(text="已接入（full_map_rect 见 config.yaml）", fg=widgets.OK_GREEN)
        else:
            self.fullmap_status.configure(text="待实机验证", fg=widgets.SECONDARY)


__all__ = ["SettingsPage", "TOOL_POLL_MS"]
