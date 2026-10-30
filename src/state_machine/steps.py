"""非阻塞步骤序列执行器（详细设计 10.1，评审 P0-4 / 实现补充 5 节）。

设计口径：
- 步骤序列只描述“单个状态内的界面操作”，由状态机在每一轮主循环调用
  :meth:`StepSequenceExecutor.advance` 推进；执行器不 sleep、不阻塞、不自己截屏，
  避免拖慢 10Hz 主循环与 10s 心跳看门狗。
- ``advance`` 每次只推进“当前步骤”的状态机（等待特征命中/等待特征消失/等待
  ``wait_ms`` 截止时间/重试计数）；一个步骤成功后才会在下一次 ``advance`` 进入下一步。
- 特征命中信息来自传入的 :class:`~src.common.types.Observation`（由感知引擎统一
  截屏与匹配），执行器不重复匹配；``Observation`` 为 ``None`` 时视为全部未命中。
- ``click_feature`` 返回绝对点击动作，坐标换算由注入的 ``to_screen`` 回调完成
  （输入是窗口客户区像素，输出是屏幕物理像素）；未注入时按恒等映射处理并在
  :class:`StepResult` 的 message 中标注，便于集成方发现配置问题。
- 失败不抛异常：返回 :attr:`StepStatus.FAILED` 并把 :class:`~src.common.errors.StepFailure`
  放进 :attr:`last_error`，由状态机决定进入异常恢复还是继续。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..common.errors import StepFailure
from ..common.types import Action, MatchResult, Observation
from ..config.models import KeyMap, NormalizedRect, StepDef

StepResultCallback = Callable[["StepResult"], None]
ToScreenCallback = Callable[[int, int], tuple[int, int]]


class StepStatus(StrEnum):
    """步骤序列状态（实现补充 5 节：RUNNING / DONE / FAILED）。"""

    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StepResult:
    """单个步骤的最终结果（供状态事件/战斗动作日志消费）。"""

    index: int
    action: str
    ok: bool
    skipped: bool
    attempts: int
    elapsed_s: float
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "action": self.action,
            "ok": self.ok,
            "skipped": self.skipped,
            "attempts": self.attempts,
            "elapsed_s": round(self.elapsed_s, 3),
            "message": self.message,
        }


#: 支持的步骤类型（详细设计 10.1 的四种 + 评审新增的 wait_feature_absent + scroll）
SUPPORTED_ACTIONS: frozenset[str] = frozenset(
    {"key", "click_feature", "wait_feature", "wait_feature_absent", "wait_ms", "scroll"}
)


class StepSequenceExecutor:
    """逐步骤推进的界面操作序列执行器（非阻塞，评审 P0-4）。

    典型用法::

        executor.start(profile.start_sequence, label="start")
        while True:
            status, actions = executor.advance(now, observation)
            if actions:
                input_executor.submit(actions)
            if status is not StepStatus.RUNNING:
                break
    """

    def __init__(
        self,
        keymap: KeyMap,
        *,
        to_screen: ToScreenCallback | None = None,
        on_step: StepResultCallback | None = None,
    ) -> None:
        self._keymap = keymap
        self._to_screen = to_screen
        self._on_step = on_step
        self._steps: tuple[StepDef, ...] = ()
        self._label = ""
        self._index = 0
        self._status = StepStatus.DONE
        self._started = False
        self._step_started_at: float | None = None
        self._attempts = 0
        self._wait_until: float | None = None
        self._last_error: StepFailure | None = None
        self._results: list[StepResult] = []

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    @property
    def status(self) -> StepStatus:
        return self._status

    @property
    def label(self) -> str:
        return self._label

    @property
    def index(self) -> int:
        return self._index

    @property
    def total(self) -> int:
        return len(self._steps)

    @property
    def active(self) -> bool:
        """是否处于运行中（已 start 且未结束）。"""
        return self._started and self._status is StepStatus.RUNNING

    @property
    def last_error(self) -> StepFailure | None:
        return self._last_error

    @property
    def results(self) -> tuple[StepResult, ...]:
        return tuple(self._results)

    @property
    def current_step(self) -> StepDef | None:
        if 0 <= self._index < len(self._steps):
            return self._steps[self._index]
        return None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空序列与内部计时状态（不触发回调）。"""
        self._steps = ()
        self._label = ""
        self._index = 0
        self._status = StepStatus.DONE
        self._started = False
        self._step_started_at = None
        self._attempts = 0
        self._wait_until = None
        self._last_error = None
        self._results = []

    def start(self, steps: Sequence[StepDef], *, label: str = "") -> None:
        """开始执行一条步骤序列；空序列立即为 DONE。"""
        self.reset()
        self._steps = tuple(steps)
        self._label = label
        self._started = True
        if not self._steps:
            self._status = StepStatus.DONE
        else:
            # 实际开始时间由第一次 advance(now) 决定，避免拿到 start 之前的时间戳
            self._status = StepStatus.RUNNING
            self._step_started_at = None

    def advance(self, now: float, observation: Observation | None = None) -> tuple[StepStatus, list[Action]]:
        """推进当前步骤，返回 (状态, 本 tick 需要提交的动作列表)。

        本方法不 sleep、不重复截屏；时间全部来自 ``now``（单调时钟秒数）。
        """
        if not self._started or self._status is not StepStatus.RUNNING:
            return self._status, []
        # 上一步的 wait_after_ms 截止时间
        if self._wait_until is not None:
            if now < self._wait_until:
                return StepStatus.RUNNING, []
            self._wait_until = None
        if self._index >= len(self._steps):
            self._status = StepStatus.DONE
            return self._status, []
        step = self._steps[self._index]
        if step.action not in SUPPORTED_ACTIONS:
            return self._fail(now, f"不支持的步骤类型: {step.action!r}")
        if self._step_started_at is None:
            self._begin_step(now)
        handlers: dict[str, Callable[[StepDef, float, Observation | None], tuple[StepStatus, list[Action]]]] = {
            "key": self._do_key,
            "click_feature": self._do_click_feature,
            "wait_feature": self._do_wait_feature,
            "wait_feature_absent": self._do_wait_feature_absent,
            "wait_ms": self._do_wait_ms,
            "scroll": self._do_scroll,
        }
        return handlers[step.action](step, now, observation)

    # ------------------------------------------------------------------
    # 步骤实现
    # ------------------------------------------------------------------
    def _do_key(self, step: StepDef, now: float, observation: Observation | None) -> tuple[StepStatus, list[Action]]:
        del observation
        if not step.key:
            return self._fail(now, "key 步骤缺少 key 参数")
        key = self._keymap.resolve(step.key)
        down, up = Action.key_press(key, label=f"step:{self._label}:key:{step.key}")
        return self._complete(now, [down, up], message=f"key={step.key}->{key}")

    def _do_click_feature(
        self, step: StepDef, now: float, observation: Observation | None
    ) -> tuple[StepStatus, list[Action]]:
        match = self._match(step, observation)
        if match is not None and match.center is not None:
            wx = int(match.center[0]) + int(step.offset_x)
            wy = int(match.center[1]) + int(step.offset_y)
            if self._to_screen is not None:
                sx, sy = self._to_screen(wx, wy)
                note = f"click {step.feature} at ({wx},{wy}) -> screen ({sx},{sy})"
            else:
                sx, sy = wx, wy
                note = f"click {step.feature} at ({wx},{wy})（未注入 to_screen，按客户区坐标处理）"
            action = Action.click_abs(sx, sy, label=f"step:{self._label}:click:{step.feature}")
            return self._complete(now, [action], message=note)
        reason = f"未找到特征 {step.feature!r}" if self._has_usable_frame(observation) else "感知帧不可用"
        return self._wait_or_retry(step, now, reason)

    def _do_wait_feature(
        self, step: StepDef, now: float, observation: Observation | None
    ) -> tuple[StepStatus, list[Action]]:
        if self._match(step, observation) is not None:
            return self._complete(now, [], message=f"特征 {step.feature!r} 命中")
        reason = f"等待特征 {step.feature!r} 超时" if self._has_usable_frame(observation) else "感知帧不可用"
        return self._wait_or_retry(step, now, reason)

    def _do_wait_feature_absent(
        self, step: StepDef, now: float, observation: Observation | None
    ) -> tuple[StepStatus, list[Action]]:
        # 没有可用感知帧时无法确认“特征消失”，继续等待而不是误判成功
        if self._has_usable_frame(observation) and self._match(step, observation) is None:
            return self._complete(now, [], message=f"特征 {step.feature!r} 已消失")
        reason = f"等待特征 {step.feature!r} 消失超时"
        return self._wait_or_retry(step, now, reason)

    def _do_wait_ms(self, step: StepDef, now: float, observation: Observation | None) -> tuple[StepStatus, list[Action]]:
        del observation
        duration = max(0, int(step.duration_ms))
        if duration <= 0:
            return self._complete(now, [], message="wait_ms=0")
        assert self._step_started_at is not None
        deadline = self._step_started_at + duration / 1000.0
        if now >= deadline:
            return self._complete(now, [], message=f"等待 {duration}ms 完成")
        return StepStatus.RUNNING, []

    def _do_scroll(self, step: StepDef, now: float, observation: Observation | None) -> tuple[StepStatus, list[Action]]:
        del observation
        clicks = int(step.scroll_clicks)
        if clicks == 0:
            return self._complete(now, [], message="scroll_clicks=0")
        action = Action.wheel(clicks, label=f"step:{self._label}:scroll")
        region = self._format_region(step.region)
        return self._complete(now, [action], message=f"滚轮 {clicks:+d} 格 region={region}")

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _match(self, step: StepDef, observation: Observation | None) -> MatchResult | None:
        if not step.feature or observation is None:
            return None
        result = observation.feature(step.feature)
        return result if result is not None and result.found else None

    @staticmethod
    def _has_usable_frame(observation: Observation | None) -> bool:
        """感知帧是否可用于判断特征有无（窗口缺失/截屏失败时不可用）。"""
        return not (
            observation is None
            or observation.window_missing
            or observation.capture_error
        )

    def _begin_step(self, now: float) -> None:
        self._step_started_at = now
        self._attempts = 0
        self._wait_until = None

    def _timeout_s(self, step: StepDef) -> float:
        return max(0.0, float(step.timeout_ms) / 1000.0)

    def _wait_or_retry(self, step: StepDef, now: float, reason: str) -> tuple[StepStatus, list[Action]]:
        assert self._step_started_at is not None
        elapsed = now - self._step_started_at
        if elapsed < self._timeout_s(step):
            return StepStatus.RUNNING, []
        if self._attempts < max(0, int(step.retries)):
            self._attempts += 1
            # 单次尝试超时后重置计时，进入下一次尝试
            self._step_started_at = now
            return StepStatus.RUNNING, []
        if step.optional:
            return self._complete(now, [], skipped=True, message=f"{reason}（可选步骤，跳过）")
        return self._fail(now, f"{reason}（尝试 {self._attempts + 1} 次）")

    def _complete(
        self,
        now: float,
        actions: list[Action],
        *,
        message: str = "",
        skipped: bool = False,
    ) -> tuple[StepStatus, list[Action]]:
        step = self._steps[self._index]
        elapsed = 0.0 if self._step_started_at is None else now - self._step_started_at
        result = StepResult(
            index=self._index,
            action=step.action,
            ok=True,
            skipped=skipped,
            attempts=self._attempts + 1,
            elapsed_s=elapsed,
            message=message,
        )
        self._emit_result(result)
        self._index += 1
        self._step_started_at = None
        self._attempts = 0
        if step.wait_after_ms > 0:
            self._wait_until = now + step.wait_after_ms / 1000.0
        if self._index >= len(self._steps):
            self._status = StepStatus.DONE
        return self._status, actions

    def _fail(self, now: float, message: str) -> tuple[StepStatus, list[Action]]:
        step = self._steps[self._index]
        elapsed = 0.0 if self._step_started_at is None else now - self._step_started_at
        error = StepFailure(message, self._index, step.action)
        self._last_error = error
        self._emit_result(
            StepResult(
                index=self._index,
                action=step.action,
                ok=False,
                skipped=False,
                attempts=self._attempts + 1,
                elapsed_s=elapsed,
                message=message,
            )
        )
        self._status = StepStatus.FAILED
        return self._status, []

    def _emit_result(self, result: StepResult) -> None:
        self._results.append(result)
        if self._on_step is not None:
            self._on_step(result)

    @staticmethod
    def _format_region(region: NormalizedRect | None) -> str:
        if region is None:
            return "none"
        return (
            f"[{region.x1:.3f},{region.y1:.3f},{region.x2:.3f},{region.y2:.3f}]"
        )
