"""数据页：真实托管摘要、最新小地图和场次记录。Tk 调用只发生在主线程。"""

from __future__ import annotations

import io
import logging
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from PIL import Image, ImageTk

from src.gui import widgets
from src.gui.viewmodel import game_state_display
from src.runtime.messages import (
    EffectivePlan,
    HistoryView,
    MinimapView,
    RunCommand,
    RunCommandKind,
    RunLifecycle,
    RunSnapshot,
)

logger = logging.getLogger("wows_script.gui.data_page")

_FOCUS_PAUSE_REASONS = frozenset({"window_lost", "window_unfocused", "window_unavailable"})


def map_point(x: float, y: float, bounds: tuple[int, int, int, int]) -> tuple[float, float] | None:
    """画布留白不参与坐标换算，边界之外不生成目标。"""
    left, top, width, height = bounds
    if width <= 0 or height <= 0 or not (left <= x < left + width and top <= y < top + height):
        return None
    return ((x - left) / width, (y - top) / height)


class DataPage(tk.Frame):
    def __init__(self, master: tk.Misc, *, on_target: Callable[[RunCommand], bool],
                 on_confirm: Callable[[], None], on_abort: Callable[[], None]) -> None:
        super().__init__(master, bg=widgets.BG)
        self._on_target = on_target
        self._snapshot: RunSnapshot | None = None
        self._frame: MinimapView | None = None
        self._image: Image.Image | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._bounds = (0, 0, 0, 0)
        self._history = HistoryView()
        self._records: tuple = ()
        self._last_target: tuple[float, float] | None = None
        self._local_target_feedback = ""
        self._runtime_target_status = ""
        self.scroll = widgets.ScrollableFrame(self, fill_height=True)
        self.scroll.pack(fill="both", expand=True)
        body = self.scroll.body
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(3, weight=1)

        self.wait = tk.Frame(body, bg=widgets.WARN_BG)
        self.wait_text = tk.Label(self.wait, bg=widgets.WARN_BG, fg=widgets.WARN_ORANGE, font=widgets.font(10))
        self.wait_text.pack(side="left", padx=12, pady=8)
        ttk.Button(self.wait, text="确认已切换账号", command=on_confirm).pack(side="right", padx=8)
        ttk.Button(self.wait, text="结束轮换", command=on_abort).pack(side="right")

        summary = tk.Frame(body, bg=widgets.PANEL)
        summary.grid(row=1, column=0, sticky="ew", padx=20, pady=(18, 12))
        heading = tk.Frame(summary, bg=widgets.PANEL)
        heading.pack(fill="x", pady=(0, 10))
        self._label(heading, "本次托管").pack(side="left")
        self.game_state = self._label(heading, "—", muted=True)
        self.game_state.pack(side="right")
        metrics = tk.Frame(summary, bg=widgets.PANEL)
        metrics.pack(fill="x")
        self.metrics: dict[str, tk.Label] = {}
        self.limits: dict[str, tk.Label] = {}
        for index, (key, title) in enumerate((("completed", "完成场数"), ("today", "今日场数"),
                                              ("elapsed", "托管时长"))):
            metrics.grid_columnconfigure(index, weight=1, uniform="metric")
            cell = tk.Frame(metrics, bg=widgets.PANEL)
            cell.grid(row=0, column=index, sticky="ew")
            self._label(cell, title, muted=True, size=9).pack(anchor="w")
            values = tk.Frame(cell, bg=widgets.PANEL)
            values.pack(anchor="w", pady=(5, 0))
            value = self._label(values, "—", size=18)
            value.pack(side="left")
            self.metrics[key] = value
            self.limits[key] = self._label(values, "", muted=True, size=9)
            self.limits[key].pack(side="left", padx=(5, 0), anchor="s", pady=(0, 4))
        self.context = self._label(summary, "—", muted=True, size=9)
        self.context.pack(fill="x", pady=(10, 0))
        tk.Frame(summary, bg=widgets.BORDER, height=1).pack(fill="x", pady=(10, 0))

        map_area = tk.Frame(body, bg=widgets.PANEL)
        map_area.grid(row=2, column=0, sticky="nsew", padx=20)
        map_area.grid_columnconfigure(0, weight=1)
        map_area.grid_rowconfigure(1, weight=1)
        header = tk.Frame(map_area, bg=widgets.PANEL)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self._label(header, "小地图").pack(side="left")
        self.edit_button = ttk.Button(header, text="更改目标", width=8, command=self._focus_map)
        self.edit_button.pack(side="right")
        self.map_body = tk.Frame(map_area, bg=widgets.PANEL)
        self.map_body.grid(row=1, column=0, sticky="nsew")
        self.map_body.grid_columnconfigure(0, weight=0)
        self.map_body.grid_columnconfigure(1, weight=1)
        self.map_body.grid_rowconfigure(0, minsize=300)
        self.map_box = tk.Frame(self.map_body, bg=widgets.PANEL, width=300, height=300)
        self.map_box.grid(row=0, column=0, sticky="nsew", padx=(0, 24))
        self.canvas = tk.Canvas(self.map_box, bg="#e7eef1", highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, width=300, height=300)
        self.canvas.bind("<Configure>", lambda _event: self._draw_map())
        self.canvas.bind("<Button-1>", self._click)
        details = tk.Frame(self.map_body, bg=widgets.PANEL)
        self.details_box = details
        details.grid(row=0, column=1, sticky="new")
        self.details: dict[str, tk.Label] = {}
        for key, title in (("mode", "导航方式"), ("target", "当前目标"), ("coordinates", "目标坐标"),
                           ("state", "目标状态"), ("age", "画面更新")):
            self._label(details, title, muted=True, size=9).pack(anchor="w")
            label = self._label(details, "—", size=9)
            label.configure(wraplength=widgets.px(210), justify="left")
            label.pack(anchor="w", pady=(4, 13))
            self.details[key] = label
        self.nav_status = self.details["state"]
        self.map_status = self.details["age"]
        self.feedback = self._label(map_area, "", muted=True, size=9)
        self.feedback.grid(row=2, column=0, sticky="ew", pady=(5, 5))
        self.feedback.grid_remove()

        history = tk.Frame(body, bg=widgets.PANEL)
        history.grid(row=3, column=0, sticky="nsew", padx=20, pady=(12, 18))
        heading = tk.Frame(history, bg=widgets.PANEL)
        heading.pack(fill="x", pady=(0, 12))
        self._label(heading, "托管记录").pack(side="left")
        self._label(heading, "当前账号", muted=True, size=9).pack(side="right")
        self.history_status = self._label(history, "暂无记录", muted=True, size=9)
        self.history_status.pack(fill="x")
        table = tk.Frame(history, bg=widgets.PANEL)
        table.pack(fill="both", expand=True)
        self.history_table = ttk.Treeview(table, columns=("battle", "time", "duration", "result"),
                                          show="headings", height=3, selectmode="browse", style="Records.Treeview")
        for key, title, width in (("battle", "场次", 80), ("time", "开始时间", 160),
                                  ("duration", "时长", 70), ("result", "结果", 130)):
            self.history_table.heading(key, text=title, anchor="e" if key == "result" else "w")
            self.history_table.column(key, width=width, minwidth=65, anchor="e" if key == "result" else "w")
        scrollbar = ttk.Scrollbar(table, command=self.history_table.yview)
        self.history_table.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.history_table.pack(side="left", fill="both", expand=True)
        self.map_body.bind("<Configure>", self._resize_map)
        self.scroll.canvas.bind("<Configure>", self._resize_map, add="+")

    @staticmethod
    def _label(master, text, *, muted=False, size=10):
        return tk.Label(master, text=text, bg=widgets.PANEL,
                        fg=widgets.SECONDARY if muted else widgets.TEXT, font=widgets.font(size), anchor="w",
                        bd=0, padx=0, pady=0)

    def _set_feedback(self, text: str) -> None:
        self.feedback.configure(text=text)
        if text:
            self.feedback.grid()
        else:
            self.feedback.grid_remove()

    def _set_local_target_feedback(self, text: str) -> None:
        """记录本地点选结果；空/未变化的周期快照不能立即抹掉它。"""
        self._runtime_target_status = (
            self._snapshot.navigation.target_status if self._snapshot is not None else ""
        )
        self._local_target_feedback = text
        self._set_feedback(text)

    def _resize_map(self, event):
        width = self.map_body.winfo_width()
        narrow = width < widgets.px(540)
        detail_width = widgets.px(205)
        gap = widgets.px(24)
        available = width if narrow else width - detail_width - gap
        side = max(100, min(available, max(widgets.px(300), self.scroll.canvas.winfo_height() - widgets.px(370)), widgets.px(720)))
        self.map_box.grid_configure(row=0, column=0, columnspan=2 if narrow else 1,
                                    sticky="nw", padx=0 if narrow else (0, gap))
        self.map_box.configure(width=side, height=side)
        self.details_box.grid_configure(row=1 if narrow else 0, column=0 if narrow else 1,
                                       columnspan=2 if narrow else 1, sticky="nw" if narrow else "ne",
                                       pady=(12, 0) if narrow else 0)
        self.map_body.grid_rowconfigure(0, minsize=side)
        self.canvas.place_configure(width=side, height=side)

    def _focus_map(self):
        if self.can_choose_target:
            self.canvas.focus_set()

    def set_history(self, view: HistoryView) -> None:
        from datetime import datetime

        self._history = view
        self.history_status.configure(text=view.error or ("" if view.records else "暂无记录"))
        if view.records and not view.error:
            self.history_status.pack_forget()
        elif not self.history_status.winfo_manager():
            self.history_status.pack(fill="x", after=self.history_status.master.winfo_children()[0])
        if view.records != self._records:
            self._records = view.records
            self.history_table.delete(*self.history_table.get_children())
            results = {"finished": "完成", "dead": "检测到死亡", "aborted": "中断", None: "进行中"}
            for row in view.records:
                duration = "—"
                if row.ended_at:
                    seconds = max(0, int((datetime.fromisoformat(row.ended_at) - datetime.fromisoformat(row.started_at)).total_seconds()))
                    duration = f"{seconds // 60:02}:{seconds % 60:02}"
                self.history_table.insert("", "end", iid=str(row.battle_id), values=(
                    f"#{row.battle_id}", row.started_at[:19].replace("T", " "), duration,
                    results.get(row.result, row.result)))
        self.metrics["today"].configure(text="—" if view.error else str(view.today_completed))

    def render(self, snapshot: RunSnapshot, plan: EffectivePlan | None) -> None:
        self._snapshot = snapshot
        session = snapshot.session
        self.game_state.configure(text=game_state_display(snapshot.game.state).split("（")[0])
        self.metrics["completed"].configure(text=str(session.battles_completed))
        self.limits["completed"].configure(text=f"/ {session.battles_target}" if session.battles_target else "")
        self.limits["today"].configure(text=f"/ {plan.daily_cap}" if plan else "")
        seconds = max(0, int(session.elapsed_s))
        self.metrics["elapsed"].configure(text=f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}")
        context = [plan.ship_display_name if plan else "—", session.account_alias or "默认账号"]
        if snapshot.game.session_id is not None:
            context.append(f"会话 #{snapshot.game.session_id}")
        if snapshot.game.battle_id is not None:
            context.append(f"本场 #{snapshot.game.battle_id}")
        self.context.configure(text="    ".join(context))
        if snapshot.lifecycle.lifecycle is RunLifecycle.WAIT_ACCOUNT:
            self.wait.grid(row=0, column=0, sticky="ew", padx=24, pady=(10, 0))
            self.wait_text.configure(text=f"等待切换账号：{session.next_alias or '—'}")
        else:
            self.wait.grid_remove()
        frame = snapshot.minimap
        if frame != self._frame or snapshot.navigation.target_override != self._last_target or frame is None:
            self._last_target = snapshot.navigation.target_override
            self._frame = frame
            try:
                self._image = Image.open(io.BytesIO(frame.png)).convert("RGB") if frame else None
            except (OSError, ValueError):
                self._image = None
            self._draw_map()
        age = max(0.0, snapshot.at - frame.captured_at) if frame else None
        selectable = self.can_choose_target
        self.edit_button.configure(state="normal" if selectable else "disabled")
        self.canvas.configure(cursor="crosshair" if selectable else "")
        nav = snapshot.navigation
        target = nav.target_override or nav.last_click_target_norm
        self.details["mode"].configure(text="游戏自动驾驶" if nav.mode == "autopilot" else "预设航线")
        if target:
            cell = chr(65 + min(9, int(target[1] * 10))) + str(min(10, int(target[0] * 10) + 1))
            self.details["target"].configure(text=cell)
            self.details["coordinates"].configure(text=f"{target[0]:.3f}, {target[1]:.3f}")
        else:
            self.details["target"].configure(text="—")
            self.details["coordinates"].configure(text="—")
        state = "已检测到移动" if nav.move_confirmed else (
            "未检测到移动 · 重试中" if nav.verify_failed else (
                "已识别激活 · 待移动" if nav.activation_detected else "已发出 · 待验证" if nav.click_count else "尚未选点"))
        self.details["state"].configure(text=state if snapshot.game.battle_active else "—")
        self.details["age"].configure(text=f"{age:.1f} 秒前" if age is not None else snapshot.minimap_status or "暂无画面")
        runtime_status = nav.target_status
        if runtime_status and runtime_status != self._runtime_target_status:
            self._runtime_target_status = runtime_status
            self._local_target_feedback = ""
        self._set_feedback(
            self._local_target_feedback
            or runtime_status
            or ("游戏输入等待中" if snapshot.gui_waiting else "")
        )

    @property
    def can_choose_target(self) -> bool:
        return self._target_unavailable_reason() is None

    def _target_unavailable_reason(self) -> str | None:
        snapshot, frame = self._snapshot, self._frame
        if snapshot is None:
            return "尚未收到运行状态"
        if frame is None or self._image is None:
            return snapshot.minimap_status or "暂无可选的小地图画面"
        if frame.battle_id != snapshot.game.battle_id:
            return "小地图不属于当前场次"
        if snapshot.navigation.mode != "autopilot":
            return "当前不是游戏自动驾驶模式"
        if snapshot.at < frame.captured_at:
            return "小地图画面时间异常"
        lifecycle = snapshot.lifecycle.lifecycle
        if snapshot.game.state == "BATTLE_ACTIVE":
            if lifecycle is not RunLifecycle.RUNNING:
                return f"运行未就绪（{lifecycle.value}）"
            return None
        if snapshot.game.state in ("PAUSED", "PAUSING"):
            if lifecycle not in (RunLifecycle.RUNNING, RunLifecycle.PAUSING, RunLifecycle.PAUSED):
                return f"运行未就绪（{lifecycle.value}）"
            reason = snapshot.game.pause_reason or "unknown"
            if reason not in _FOCUS_PAUSE_REASONS:
                return f"当前暂停不可选点（{reason}）"
            return None
        return f"当前不是战斗画面（{snapshot.game.state}）"

    def _draw_map(self) -> None:
        self.canvas.delete("all")
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        if self._image is None:
            self._bounds = (0, 0, 0, 0)
            status = self._snapshot.minimap_status if self._snapshot else ""
            self.canvas.create_text(width // 2, height // 2, text=status or "暂无战斗画面", width=max(100, width - 40),
                                    fill=widgets.SECONDARY, font=widgets.font(10))
            return
        scale = min(max(1, width - 2) / self._image.width, max(1, height - 2) / self._image.height)
        w, h = max(1, int(self._image.width * scale)), max(1, int(self._image.height * scale))
        self._photo = ImageTk.PhotoImage(self._image.resize((w, h), Image.Resampling.LANCZOS), master=self)
        x, y = (width - w) // 2, (height - h) // 2
        self._bounds = (x, y, w, h)
        self.canvas.create_image(x, y, image=self._photo, anchor="nw")
        target = self._snapshot.navigation.target_override if self._snapshot else None
        if target:
            tx, ty = x + target[0] * w, y + target[1] * h
            self.canvas.create_oval(tx - 5, ty - 5, tx + 5, ty + 5, outline="#ffffff", width=2)

    def _click(self, event: tk.Event) -> None:
        reason = self._target_unavailable_reason()
        if reason is not None:
            self._set_local_target_feedback(f"无法选点：{reason}")
            logger.warning("gui_target_click accepted=False battle=%s frame=%s reason=%s",
                           self._snapshot.game.battle_id if self._snapshot else None,
                           self._frame.frame_id if self._frame else None, reason)
            return
        target = map_point(event.x, event.y, self._bounds)
        if target is None or self._frame is None:
            reason = "点击位置不在小地图内容区"
            self._set_local_target_feedback(f"无法选点：{reason}")
            logger.warning("gui_target_click accepted=False battle=%s frame=%s reason=%s",
                           self._snapshot.game.battle_id if self._snapshot else None,
                           self._frame.frame_id if self._frame else None, reason)
            return
        command = RunCommand(RunCommandKind.REPLACE_TARGET, target=target,
                             battle_id=self._frame.battle_id, frame_id=self._frame.frame_id)
        accepted = self._on_target(command)
        self._set_local_target_feedback("目标已提交，等待游戏前台" if accepted else "目标提交失败")
        logger.info(
            "gui_target_click accepted=%s battle=%s frame=%s target=(%.3f,%.3f)",
            accepted,
            command.battle_id,
            command.frame_id,
            target[0],
            target[1],
        )
