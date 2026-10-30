"""会话与场次控制（详细设计 6.7 / 10.6，评审 P2 会话上限）。

职责：
- 会话开始/结束时间与停止原因记录；
- 单场时长计时、会话累计场次与累计时长；
- 会话上限（``safety.max_battles_per_session`` / ``safety.max_session_minutes``）；
- 每日场次上限（跨进程，从数据库按本地日期查询，回调注入）；
- 允许运行时段（``session.allowed_hours``，支持跨零点区间）；
- 两场之间的随机等待（可只采样不阻塞，也可用注入的 Sleeper 等待）；
- 连续开局失败计数（达到阈值返回停止请求，供状态机进入 PAUSED）。

本模块只做决策，不写数据库、不直接操作输入；记录由回调交由存储层处理。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from datetime import time as dt_time
from enum import StrEnum
from typing import Any

from ..common.clock import Clock, Sleeper
from ..common.errors import StopRequest
from ..config.models import SafetyConfig, SessionConfig

SessionEventCallback = Callable[[str, dict[str, Any]], None]
DailyBattleCounter = Callable[[date], int]


class StopReason(StrEnum):
    """会话停止原因（详细设计 6.7；写入 sessions.stop_reason）。"""

    SESSION_BATTLES_CAP = "session_battles_cap"
    SESSION_DURATION_CAP = "session_duration_cap"
    DAILY_BATTLE_CAP = "daily_battle_cap"
    OUTSIDE_ALLOWED_HOURS = "outside_allowed_hours"
    EMERGENCY_STOP = "emergency_stop"
    CONSECUTIVE_START_FAILURES = "consecutive_start_failures"
    WINDOW_LOST = "window_lost"
    GAME_CRASH = "game_crash"
    MANUAL = "manual"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SessionSummary:
    """会话结束时写入会话记录/日志的摘要。"""

    battles_completed: int
    elapsed_s: float
    started_at: datetime | None
    stop: StopRequest | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "battles_completed": self.battles_completed,
            "elapsed_s": round(self.elapsed_s, 1),
            "started_at": self.started_at.isoformat(timespec="seconds") if self.started_at else None,
            "stop_reason": self.stop.reason if self.stop else None,
            "stop_detail": self.stop.detail if self.stop else "",
        }


class SessionController:
    """会话/场次上限与停止原因控制。

    ``count_battles_on`` 由存储层提供（数据库按本地日期统计已完成场次）；
    未注入时跳过每日上限检查（记录一次 warning 事件）。
    """

    def __init__(
        self,
        session: SessionConfig,
        safety: SafetyConfig,
        clock: Clock,
        *,
        sleeper: Sleeper | None = None,
        count_battles_on: DailyBattleCounter | None = None,
        rng: Any = None,
        on_event: SessionEventCallback | None = None,
    ) -> None:
        self._session = session
        self._safety = safety
        self._clock = clock
        self._sleeper = sleeper
        self._count_battles_on = count_battles_on
        self._rng = rng
        self._on_event = on_event

        self._started_at: float | None = None
        self._wall_started_at: datetime | None = None
        self._battle_started_at: float | None = None
        self._battles_completed = 0
        self._last_battle_duration_s: float | None = None
        self._consecutive_start_failures = 0
        self._stop_request: StopRequest | None = None
        # 每日场次查询走 SQLite，主循环每 0.1s 调 check_stop()，缓存几秒避免刷爆数据库
        self._daily_count_cache: tuple[float, Any, int | None] | None = None
        self._allowed_hours = self._parse_allowed_hours(session.allowed_hours)
        if self._rng is None:
            import random

            self._rng = random.Random()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> StopRequest | None:
        """开始会话；若当前不在允许时段，返回停止请求（会话不启动）。"""
        if self._started_at is not None and self._stop_request is None:
            raise RuntimeError("会话已经启动")
        self._started_at = self._clock.now()
        self._wall_started_at = self._clock.wall_now()
        self._battle_started_at = None
        self._battles_completed = 0
        self._last_battle_duration_s = None
        self._consecutive_start_failures = 0
        self._stop_request = None
        self._emit(
            "session_start",
            {
                "started_at": self._wall_started_at.isoformat(timespec="seconds"),
                "max_battles": self._safety.max_battles_per_session,
                "max_minutes": self._safety.max_session_minutes,
            },
        )
        if not self.in_allowed_hours():
            return self._make_stop(StopReason.OUTSIDE_ALLOWED_HOURS, self._allowed_hours_detail())
        return None

    def stop(self, reason: StopReason | str, detail: str = "") -> StopRequest:
        """记录停止原因并返回停止请求；重复调用保留第一次的原因。"""
        if self._stop_request is not None:
            return self._stop_request
        request = self._make_stop(reason, detail)
        self._stop_request = request
        self._emit(
            "session_stop",
            {
                "reason": request.reason,
                "detail": request.detail,
                "battles_completed": self._battles_completed,
                "elapsed_s": round(self.elapsed_s, 1),
            },
        )
        return request

    # ------------------------------------------------------------------
    # 场次
    # ------------------------------------------------------------------
    def begin_battle(self) -> None:
        """记录一场战斗开始（用于单场计时与周转指标）。"""
        if self._started_at is None:
            raise RuntimeError("会话尚未启动")
        if self._battle_started_at is not None:
            raise RuntimeError("上一场战斗尚未结束")
        self._battle_started_at = self._clock.now()
        self._emit("battle_start", {"battles_completed": self._battles_completed})

    def end_battle(self, result: str = "") -> float:
        """记录一场战斗结束，返回本场时长（秒）。"""
        if self._battle_started_at is None:
            raise RuntimeError("没有进行中的战斗")
        duration = max(0.0, self._clock.now() - self._battle_started_at)
        self._battle_started_at = None
        self._battles_completed += 1
        self._last_battle_duration_s = duration
        self._daily_count_cache = None  # 场次已变化，下次 check_stop 重新查询
        self._emit(
            "battle_end",
            {
                "duration_s": round(duration, 1),
                "battles_completed": self._battles_completed,
                "result": result,
            },
        )
        return duration

    # ------------------------------------------------------------------
    # 上限检查
    # ------------------------------------------------------------------
    def check_stop(self) -> StopRequest | None:
        """检查会话/每日/时段上限，返回停止请求或 None（不改变停止状态）。"""
        if self._stop_request is not None:
            return self._stop_request
        if self._started_at is None:
            raise RuntimeError("会话尚未启动")
        if self._battles_completed >= self._safety.max_battles_per_session:
            return self._make_stop(
                StopReason.SESSION_BATTLES_CAP,
                f"已完成 {self._battles_completed} 场，上限 {self._safety.max_battles_per_session}",
            )
        elapsed = self.elapsed_s
        max_seconds = float(self._safety.max_session_minutes) * 60.0
        if elapsed >= max_seconds:
            return self._make_stop(
                StopReason.SESSION_DURATION_CAP,
                f"已运行 {elapsed / 60.0:.1f} 分钟，上限 {self._safety.max_session_minutes:g} 分钟",
            )
        if not self.in_allowed_hours():
            return self._make_stop(StopReason.OUTSIDE_ALLOWED_HOURS, self._allowed_hours_detail())
        daily = self.count_battles_today()
        if daily is not None and daily >= self._session.daily_battle_cap:
            return self._make_stop(
                StopReason.DAILY_BATTLE_CAP,
                f"今日已完成 {daily} 场，每日上限 {self._session.daily_battle_cap}",
            )
        return None

    def count_battles_today(self) -> int | None:
        """查询今日已完成场次；未注入回调时返回 None（跳过检查）。

        结果缓存若干秒（默认 5s），因为 check_stop() 在主循环里每 tick 都会调用。
        """
        if self._count_battles_on is None:
            return None
        now = self._clock.now()
        today = self._clock.wall_now().date()
        cached = self._daily_count_cache
        if cached is not None and cached[1] == today and now - cached[0] < 30.0:
            return cached[2]
        try:
            count = int(self._count_battles_on(today))
        except Exception as exc:  # 存储失败不应中断会话，降级为跳过每日上限检查
            self._emit("session_count_error", {"error": repr(exc)})
            return None
        self._daily_count_cache = (now, today, count)
        return count

    # ------------------------------------------------------------------
    # 允许时段
    # ------------------------------------------------------------------
    def in_allowed_hours(self, moment: datetime | None = None) -> bool:
        """当前墙上时间是否在允许时段内（跨零点区间按跨夜处理）。"""
        if self._allowed_hours is None:
            return True
        now = moment if moment is not None else self._clock.wall_now()
        current = now.time()
        start, end = self._allowed_hours
        if start <= end:
            return start <= current <= end
        return current >= start or current <= end

    # ------------------------------------------------------------------
    # 两场之间的随机等待
    # ------------------------------------------------------------------
    def sample_between_battle_s(self) -> float:
        """采样两场之间的等待秒数（均匀分布，落在 between_battle_s 区间内）。"""
        low, high = self._session.between_battle_s
        if high < low:
            raise ValueError(f"between_battle_s 区间非法: {self._session.between_battle_s!r}")
        return float(self._rng.uniform(low, high))

    def wait_between_battles(self) -> float:
        """采样并等待两场之间的随机间隔，返回等待秒数。

        状态机若不想阻塞主循环，可只用 :meth:`sample_between_battle_s` 自行计时；
        本方法用于简单的串行流程。
        """
        seconds = self.sample_between_battle_s()
        if self._sleeper is not None:
            self._sleeper.sleep(seconds)
        return seconds

    # ------------------------------------------------------------------
    # 连续开局失败
    # ------------------------------------------------------------------
    def record_start_success(self) -> None:
        if self._consecutive_start_failures:
            self._consecutive_start_failures = 0
            self._emit("start_failure_reset", {})

    def record_start_failure(self, detail: str = "") -> StopRequest | None:
        """记录一次开局失败；达到阈值返回停止请求。"""
        self._consecutive_start_failures += 1
        self._emit(
            "start_failure",
            {
                "consecutive_failures": self._consecutive_start_failures,
                "detail": detail,
            },
        )
        threshold = max(1, self._session.max_consecutive_start_failures)
        if self._consecutive_start_failures >= threshold:
            return self._make_stop(
                StopReason.CONSECUTIVE_START_FAILURES,
                f"连续 {self._consecutive_start_failures} 次开局失败（{detail}）",
            )
        return None

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def started(self) -> bool:
        return self._started_at is not None

    @property
    def stopped(self) -> bool:
        return self._stop_request is not None

    @property
    def stop_request(self) -> StopRequest | None:
        return self._stop_request

    @property
    def battles_completed(self) -> int:
        return self._battles_completed

    @property
    def consecutive_start_failures(self) -> int:
        return self._consecutive_start_failures

    @property
    def last_battle_duration_s(self) -> float | None:
        return self._last_battle_duration_s

    @property
    def elapsed_s(self) -> float:
        if self._started_at is None:
            return 0.0
        return max(0.0, self._clock.now() - self._started_at)

    @property
    def battle_in_progress(self) -> bool:
        return self._battle_started_at is not None

    @property
    def session_elapsed_s(self) -> float:
        return self.elapsed_s

    def summary(self) -> SessionSummary:
        return SessionSummary(
            battles_completed=self._battles_completed,
            elapsed_s=self.elapsed_s,
            started_at=self._wall_started_at,
            stop=self._stop_request,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _make_stop(self, reason: StopReason | str, detail: str = "") -> StopRequest:
        return StopRequest(reason=reason.value if isinstance(reason, StopReason) else str(reason), detail=detail)

    def _allowed_hours_detail(self) -> str:
        if self._allowed_hours is None:
            return ""
        start, end = self._allowed_hours
        current = self._clock.wall_now().isoformat(timespec="seconds")
        return f"当前 {current} 不在允许时段 {start.strftime('%H:%M')}-{end.strftime('%H:%M')}"

    @staticmethod
    def _parse_allowed_hours(
        value: tuple[str, str] | None,
    ) -> tuple[dt_time, dt_time] | None:
        if value is None:
            return None
        try:
            start = dt_time.fromisoformat(value[0])
            end = dt_time.fromisoformat(value[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"allowed_hours 需要两个 HH:MM 时间: {value!r}") from exc
        return start, end

    def _emit(self, event: str, detail: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, detail)
