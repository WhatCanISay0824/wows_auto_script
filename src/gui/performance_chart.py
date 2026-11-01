"""最近一分钟的真实 CPU 采样，不补造缺失数据。"""

from __future__ import annotations

import tkinter as tk
from collections import deque

from src.gui import widgets


class CpuHistoryChart(tk.Canvas):
    def __init__(self, master):
        super().__init__(master, height=180, bg=widgets.PANEL, bd=0, highlightthickness=0)
        self.samples: deque[tuple[float, float]] = deque(maxlen=60)
        self._latest_at: float | None = None
        self.bind("<Configure>", lambda event: self._draw())

    def update_sample(self, sampled_at: float | None, cpu_pct: float | None):
        if sampled_at is None:
            return
        if self._latest_at is not None and sampled_at <= self._latest_at:
            return
        self._latest_at = sampled_at
        while self.samples and sampled_at - self.samples[0][0] > 60:
            self.samples.popleft()
        if cpu_pct is not None:
            self.samples.append((sampled_at, max(0, min(100, cpu_pct))))
        self._draw()

    def _draw(self):
        self.delete("all")
        width, height = max(100, self.winfo_width()), max(70, self.winfo_height())
        left, right, top, bottom = 42, width - 12, 10, height - 25
        for value in (100, 50, 0):
            y = bottom - (bottom - top) * value / 100
            self.create_line(left, y, right, y, fill=widgets.BORDER)
            self.create_text(left - 8, y, text=f"{value}%", anchor="e", fill=widgets.SECONDARY,
                             font=widgets.font(8))
        self.create_text(left, height - 6, text="−60 秒", anchor="sw", fill=widgets.SECONDARY,
                         font=widgets.font(8))
        self.create_text(right, height - 6, text="现在", anchor="se", fill=widgets.SECONDARY,
                         font=widgets.font(8))
        if not self.samples:
            return
        latest = self._latest_at
        segment: list[float] = []
        previous = None
        for sampled_at, value in self.samples:
            if previous is not None and sampled_at - previous > 3:
                if len(segment) >= 4:
                    self.create_line(*segment, fill="#616b72", width=1.5)
                segment = []
            segment.extend((right - (latest - sampled_at) / 60 * (right - left),
                            bottom - value / 100 * (bottom - top)))
            previous = sampled_at
        if len(segment) >= 4:
            self.create_line(*segment, fill="#616b72", width=1.5)
        elif segment:
            x, y = segment
            self.create_oval(x - 2, y - 2, x + 2, y + 2, fill="#616b72", outline="")
