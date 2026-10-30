"""时钟与等待抽象。

主循环、超时和行为间隔使用单调时钟（详细设计 2.4），持久化时间使用带时区的
ISO-8601 文本（详细设计 11.1）。测试用 FakeClock / FakeSleeper 驱动，不真正等待。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """时间源：单调时钟用于计时，墙上时间用于日志。"""

    def now(self) -> float:
        """单调时钟秒数（只保证递增，不对应 epoch）。"""

    def wall_now(self) -> datetime:
        """带时区的当前时间。"""


class SystemClock:
    """真实系统时钟。"""

    def now(self) -> float:
        return time.monotonic()

    def wall_now(self) -> datetime:
        return datetime.now().astimezone()


class FakeClock:
    """测试用假时钟：单调时间从 start 开始，墙上时间可指定。"""

    def __init__(
        self,
        start: float = 0.0,
        wall_start: datetime | None = None,
    ) -> None:
        self._now = float(start)
        self._start = float(start)
        self._wall_start = wall_start or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=8)))

    def now(self) -> float:
        return self._now

    def wall_now(self) -> datetime:
        return self._wall_start + timedelta(seconds=self._now - self._start)

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("时间不能倒退")
        self._now += float(seconds)
        return self._now

    def set(self, value: float) -> None:
        if value < self._now:
            # 允许测试中重建场景，但显式提醒调用方
            pass
        self._now = float(value)


@runtime_checkable
class Sleeper(Protocol):
    def sleep(self, seconds: float) -> None:
        """等待指定秒数（真实实现会阻塞，假实现只推进时钟）。"""


class SystemSleeper:
    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeSleeper:
    """与 FakeClock 配合的假等待：只推进时钟，不阻塞。"""

    def __init__(self, clock: FakeClock) -> None:
        self._clock = clock

    def sleep(self, seconds: float) -> None:
        self._clock.advance(max(0.0, seconds))


def iso_timestamp(moment: datetime) -> str:
    """格式化为带时区的 ISO-8601 文本，精确到秒。"""
    return moment.astimezone().isoformat(timespec="seconds")


def clock_iso(clock: Clock) -> str:
    return iso_timestamp(clock.wall_now())


def parse_iso(text: str) -> datetime:
    """解析日志/数据库中的 ISO-8601 时间。"""
    value = datetime.fromisoformat(text)
    if value.tzinfo is None:
        raise ValueError(f"时间缺少时区: {text!r}")
    return value
