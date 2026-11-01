"""输入线程与动作队列（详细设计 2.4 / 9.4 / 13.5，评审补充第 3 节第 4/5 项）。

输入线程是唯一的键鼠出口：状态机、导航、行为等上层模块只通过
:meth:`InputExecutor.submit` 提交动作，由本模块的输入线程串行执行。

实现口径（Lead 评审纠偏）：

- ``submit(actions)`` 把一批动作作为**原子批次**入队，同一批次内不会被其它批次插入；
- 动作之间保持 ``min_action_interval_ms``（默认 150ms）± ``interval_jitter``（默认 20%）
  的随机间隔，时间推进使用注入的 :class:`~src.common.clock.Clock` /
  :class:`~src.common.clock.Sleeper`，测试不真正睡眠；
- 队列积压时丢弃“最早的 ``priority < 20`` 动作”，``priority >= 20`` 的动作保留
  （必要时允许队列软超限，也不丢关键动作）；
- 输入线程执行每个动作前检查 ``epoch`` / ``stop_flag`` 与 ``Action.expires_at``，
  过期动作丢弃并记录，避免卡顿后补发过期的小地图点击；
- ``release_all()`` 与 ``stop()`` 不直接调用后端，而是把“释放所有键鼠”作为最高优先级
  控制动作入队，**由输入线程消费后执行**；看门狗等其它线程不要直接调用 SendInput；
- ``stop()`` 设置停止标志、清空待执行动作、入队释放控制动作并等待输入线程退出；
- **统一输入门禁（ISS-004）**：暂停/停止/紧急停止时关闭门禁 —— 拒绝新业务动作、
  ``epoch+1`` 使在途与排队批次失效、清空待执行业务动作、入队释放控制动作；
  恢复（``open_gate()``）后只接受重新识别画面生成的新动作。
  ``release_all()`` 语义为“取消 + 释放”，``close_gate_and_release()`` 供暂停与安全监控
  线程调用（``wait=False`` 时非阻塞，只入队释放）；
- 连续 3 次后端失败（详细设计 9.2）时置 ``failed``、关闭门禁、清空队列，并由输入线程
  **立即释放一次键鼠**（ISS-009）；上层检测到 ``failed`` 后进入 PAUSED，恢复前必须
  ``reset_after_failure()``。
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from ..common.clock import Clock, Sleeper, SystemClock, SystemSleeper
from ..common.errors import InputUnavailable
from ..common.types import Action
from ..config.models import InputConfig
from .winput import InputBackend

logger = logging.getLogger(__name__)

#: 关键动作优先级下限：>= 该值的动作在队列积压时不会被丢弃
CRITICAL_PRIORITY = 20

#: 连续后端失败多少次后停止执行动作并置 failed（详细设计 9.2：连续三次失败进入暂停）
MAX_CONSECUTIVE_ERRORS = 3

#: 输入线程名（测试用于确认释放动作确实由输入线程执行）
INPUT_THREAD_NAME = "input-thread"


@dataclass(frozen=True, slots=True)
class _TaskBatch:
    """队列中的一批动作。

    ``release_all`` 为真时表示“释放所有键鼠”的控制批次；``release_seq`` 用于
    :meth:`InputExecutor.release_all` 等待输入线程确认已执行释放。
    """

    actions: tuple[Action, ...] = ()
    priority: int = 0
    epoch: int = 0
    label: str = ""
    release_all: bool = False
    release_seq: int = 0

    @classmethod
    def from_actions(cls, actions: tuple[Action, ...], *, epoch: int) -> _TaskBatch:
        priority = max((action.priority for action in actions), default=0)
        label = next((action.label for action in actions if action.label), "") or "+".join(
            action.kind.value for action in actions
        )
        return cls(actions=actions, priority=int(priority), epoch=epoch, label=label)

    @classmethod
    def release_batch(cls, *, epoch: int, seq: int) -> _TaskBatch:
        return cls(
            priority=100,
            epoch=epoch,
            label="release_all",
            release_all=True,
            release_seq=seq,
        )


@dataclass
class _Stats:
    """输入线程统计（供日志与测试读取）。"""

    executed: int = 0
    dropped: int = 0
    expired: int = 0
    rejected: int = 0
    errors: int = 0
    releases: int = 0
    fallback_releases: int = 0
    last_error: InputUnavailable | None = field(default=None)


class InputExecutor:
    """输入线程门面：唯一的键鼠出口（详细设计 2.4 / 9.4）。

    典型用法::

        executor = InputExecutor(make_backend(config.input), config_input, clock=clock)
        executor.submit([Action.click_abs(1600, 1400, label="nav_click")])
        ...
        executor.stop()   # 清空队列、由输入线程释放按键后退出

    构造函数可注入 ``clock`` / ``sleeper`` / ``rng`` 供离线测试；``auto_start=False``
    时输入线程延迟到 :meth:`start` 才创建。
    """

    def __init__(
        self,
        backend: InputBackend,
        config: InputConfig | None = None,
        *,
        clock: Clock | None = None,
        sleeper: Sleeper | None = None,
        rng: random.Random | None = None,
        auto_start: bool = True,
        can_execute: Callable[[], bool] | None = None,
    ) -> None:
        self._backend = backend
        self._can_execute = can_execute
        self._config = config or InputConfig()
        if self._config.queue_max < 1:
            raise ValueError(f"input.queue_max 必须 >= 1，当前 {self._config.queue_max!r}")
        self._clock = clock or SystemClock()
        self._sleeper = sleeper or SystemSleeper()
        self._rng = rng or random.Random()
        self._cond = threading.Condition()
        self._queue: deque[_TaskBatch] = deque()
        self._thread: threading.Thread | None = None
        self._shutdown = False
        self._failed = False
        # 业务输入门禁：关闭后拒绝新动作、旧批次由 epoch 失效；释放仍允许入队执行
        self._gate_open = True
        self._gate_reason = ""
        self._epoch = 0
        self._executing = False
        self._next_ready = 0.0
        self._release_seq = 0
        self._release_done = 0
        self._consecutive_errors = 0
        self._stats = _Stats()
        if auto_start:
            self.start()

    # --- 生命周期 ---------------------------------------------------
    def start(self) -> None:
        """创建并启动输入线程；已启动且未停止时幂等，停止后可重新启动。"""
        thread = self._thread
        if thread is not None and thread.is_alive():
            with self._cond:
                stopping = self._shutdown
            if not stopping:
                return
            thread.join(self._join_timeout())  # 等待上一轮停止流程收尾
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return
            self._shutdown = False
            self._failed = False
            self._gate_open = True
            self._gate_reason = ""
            self._consecutive_errors = 0
            new_thread = threading.Thread(target=self._run, name=INPUT_THREAD_NAME, daemon=True)
            self._thread = new_thread
        new_thread.start()

    def join(self, timeout: float | None = None) -> bool:
        """等待输入线程退出；返回是否已退出。"""
        thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and not self._shutdown

    @property
    def stop_requested(self) -> bool:
        with self._cond:
            return self._shutdown

    @property
    def gate_open(self) -> bool:
        """业务输入门禁是否打开（暂停/停止/失败时为 False）。"""
        with self._cond:
            return self._gate_open

    @property
    def gate_reason(self) -> str:
        with self._cond:
            return self._gate_reason

    # --- 提交动作 ---------------------------------------------------
    def submit(self, actions: Sequence[Action] | Action) -> None:
        """把一批动作原子入队；输入线程已停止或已失败时忽略。"""
        items = (actions,) if isinstance(actions, Action) else tuple(actions)
        if not items:
            return
        for action in items:
            if not isinstance(action, Action):
                raise TypeError(f"submit 只接受 Action，收到 {type(action)!r}")
        with self._cond:
            if self._shutdown or self._failed:
                self._stats.rejected += len(items)
                logger.warning("输入线程已停止/失败，忽略 %d 个动作", len(items))
                return
            if not self._gate_open:
                self._stats.rejected += len(items)
                logger.debug(
                    "输入门禁已关闭（原因=%s），忽略 %d 个业务动作",
                    self._gate_reason or "unknown",
                    len(items),
                )
                return
            batch = _TaskBatch.from_actions(items, epoch=self._epoch)
            self._enqueue_locked(batch)
            self._cond.notify_all()

    # --- 释放与停止 -------------------------------------------------
    def release_all(self, *, timeout: float = 2.0) -> None:
        """取消待执行业务动作 + 关闭门禁 + 请求输入线程释放所有键鼠（暂停/失焦使用）。

        语义（ISS-004）：先关闭业务输入门禁、使在途/排队批次失效并清空队列，
        再等待输入线程确认释放。恢复时必须显式 :meth:`open_gate`。
        """
        self.close_gate_and_release("release_all", wait=True, timeout=timeout)

    def close_gate_and_release(
        self, reason: str = "", *, wait: bool = False, timeout: float = 2.0
    ) -> int:
        """关闭业务输入门禁并请求释放键鼠（线程安全、可被安全监控线程调用）。

        ``wait=False``（默认）只入队释放控制动作后立即返回，由输入线程执行；
        ``wait=True`` 等待输入线程确认释放（最多 ``timeout`` 秒）。
        返回释放请求序号（输入线程未运行时为 0）。
        """
        with self._cond:
            self._gate_open = False
            if reason:
                self._gate_reason = reason
            self._epoch += 1  # 使在途/排队批次失效
            dropped = sum(len(batch.actions) for batch in self._queue if not batch.release_all)
            if dropped:
                # 只保留控制批次（释放），业务动作全部丢弃
                self._queue = deque(batch for batch in self._queue if batch.release_all)
                self._stats.dropped += dropped
            thread = self._thread
            alive = thread is not None and thread.is_alive()
            already_stopped = self._shutdown
            seq = self._enqueue_release_locked(reason or "gate_close") if alive else 0
            self._cond.notify_all()
        if not alive:
            if not already_stopped:
                self._fallback_release("输入线程未运行")
            return 0
        if wait:
            self._wait_release_done(seq, timeout)
        return seq

    def open_gate(self, reason: str = "") -> bool:
        """重新打开业务输入门禁（暂停恢复后、重新识别画面完成时调用）。"""
        with self._cond:
            if self._shutdown or self._failed:
                return False
            was_open = self._gate_open
            self._gate_open = True
            self._gate_reason = ""
            self._cond.notify_all()
        if not was_open:
            logger.info("输入门禁已重新打开（%s）", reason or "resume")
        return True

    def reset_after_failure(self) -> bool:
        """输入线程连续失败后的显式恢复：清失败状态、重置计数、重新打开门禁。

        必须在暂停状态、确认后端问题已排除后调用；输入线程若已退出会重新启动。
        返回是否恢复成功（停止状态返回 False）。
        """
        with self._cond:
            if self._shutdown:
                return False
            was_failed = self._failed
            self._failed = False
            self._consecutive_errors = 0
            self._gate_open = True
            self._gate_reason = ""
            self._epoch += 1
            thread = self._thread
            alive = thread is not None and thread.is_alive()
            self._cond.notify_all()
        if not alive:
            self.start()
        if was_failed:
            logger.warning("输入线程失败状态已重置，门禁重新打开（请确认输入后端可用）")
        return True

    def stop(self, drain: bool = True) -> None:
        """停止输入线程：清空待执行动作，由输入线程释放键鼠后退出。

        ``drain=True``（默认）等待输入线程退出，调用返回时键鼠已释放；
        ``drain=False`` 只入队释放控制动作后立即返回（释放仍由输入线程执行，
        调用方随后应通过 :meth:`join` 等待线程退出，例如进程退出流程）。
        """
        with self._cond:
            already_stopped = self._shutdown
            self._shutdown = True
            self._gate_open = False
            self._gate_reason = "stop"
            self._epoch += 1  # 使在途批次剩余动作失效
            dropped = sum(len(batch.actions) for batch in self._queue if not batch.release_all)
            self._queue.clear()
            self._stats.dropped += dropped
            self._enqueue_release_locked("stop")
            thread = self._thread
            alive = thread is not None and thread.is_alive()
            self._cond.notify_all()
        if not alive:
            if not already_stopped:
                self._fallback_release("输入线程未运行")
            return
        if drain and not self.join(self._join_timeout()):
            logger.error("输入线程在 %.1fs 内未退出", self._join_timeout())

    # --- 状态查询 ---------------------------------------------------
    @property
    def backend(self) -> InputBackend:
        return self._backend

    @property
    def config(self) -> InputConfig:
        return self._config

    @property
    def epoch(self) -> int:
        with self._cond:
            return self._epoch

    @property
    def failed(self) -> bool:
        with self._cond:
            return self._failed

    @property
    def consecutive_errors(self) -> int:
        with self._cond:
            return self._consecutive_errors

    @property
    def error_count(self) -> int:
        with self._cond:
            return self._stats.errors

    @property
    def last_error(self) -> InputUnavailable | None:
        with self._cond:
            return self._stats.last_error

    @property
    def executed_actions(self) -> int:
        with self._cond:
            return self._stats.executed

    @property
    def dropped_actions(self) -> int:
        with self._cond:
            return self._stats.dropped

    @property
    def expired_actions(self) -> int:
        with self._cond:
            return self._stats.expired

    @property
    def rejected_actions(self) -> int:
        with self._cond:
            return self._stats.rejected

    @property
    def release_count(self) -> int:
        with self._cond:
            return self._stats.releases

    @property
    def fallback_release_count(self) -> int:
        with self._cond:
            return self._stats.fallback_releases

    @property
    def pending_actions(self) -> int:
        with self._cond:
            return self._pending_actions_locked()

    def wait_until_idle(self, timeout: float = 5.0) -> bool:
        """等待队列清空且当前动作执行完毕（测试/退出前同步用，超时返回 False）。"""
        deadline = time.monotonic() + timeout
        with self._cond:
            while self._queue or self._executing:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    # --- 内部：入队 -------------------------------------------------
    def _pending_actions_locked(self) -> int:
        return sum(len(batch.actions) for batch in self._queue if not batch.release_all)

    def _enqueue_locked(self, batch: _TaskBatch) -> None:
        """入队普通批次；队列积压时按优先级丢弃旧动作（评审纠偏第 5 项）。"""
        max_actions = self._config.queue_max
        while self._pending_actions_locked() + len(batch.actions) > max_actions:
            if not self._drop_earliest_low_priority_locked():
                if batch.priority < CRITICAL_PRIORITY:
                    # 新批次本身可丢：丢弃它，保住队列中的关键动作
                    self._stats.dropped += len(batch.actions)
                    logger.warning("队列已满，丢弃新提交的低优先级批次: %s", batch.label)
                    return
                # 队列里全是关键动作：允许软超限，也不丢关键动作
                logger.warning(
                    "队列已满且全是关键动作，允许软超限: %s（当前 %d 个动作）",
                    batch.label,
                    self._pending_actions_locked(),
                )
                break
        self._queue.append(batch)

    def _drop_earliest_low_priority_locked(self) -> bool:
        for index, batch in enumerate(self._queue):
            if batch.release_all:
                continue
            if batch.priority < CRITICAL_PRIORITY:
                del self._queue[index]
                self._stats.dropped += len(batch.actions)
                logger.warning(
                    "队列积压，丢弃最早的低优先级批次: %s（priority=%d，%d 个动作）",
                    batch.label,
                    batch.priority,
                    len(batch.actions),
                )
                return True
        return False

    def _enqueue_release_locked(self, label: str) -> int:
        self._release_seq += 1
        batch = _TaskBatch.release_batch(epoch=self._epoch, seq=self._release_seq)
        # 释放控制动作最高优先级：插到队首，先于其他动作执行
        self._queue.appendleft(batch)
        logger.info("已入队释放控制动作: %s（seq=%d）", label, self._release_seq)
        return self._release_seq

    # --- 内部：输入线程 ---------------------------------------------
    def _run(self) -> None:
        try:
            while True:
                batch = self._take_batch()
                if batch is None:
                    break
                try:
                    if batch.release_all:
                        self._process_release(batch)
                    elif self._batch_invalid(batch):
                        with self._cond:
                            self._stats.dropped += len(batch.actions)
                    else:
                        self._run_batch(batch)
                except Exception:  # 输入线程绝不能因单个批次异常退出
                    logger.exception("输入线程处理批次异常: %s", batch.label)
                finally:
                    with self._cond:
                        self._executing = False
                        self._cond.notify_all()
        finally:
            with self._cond:
                self._executing = False
                self._cond.notify_all()
            # 本线程就是输入线程，退出前兜底释放；release_all 幂等
            try:
                self._backend.release_all()
            except InputUnavailable as exc:
                logger.error("输入线程退出时释放键鼠失败: %s", exc)

    def _take_batch(self) -> _TaskBatch | None:
        with self._cond:
            while not self._queue:
                if self._shutdown:
                    return None
                self._cond.wait()
            batch = self._queue.popleft()
            self._executing = True
            return batch

    def _batch_invalid(self, batch: _TaskBatch) -> bool:
        with self._cond:
            return (
                batch.epoch != self._epoch
                or self._shutdown
                or self._failed
                or not self._gate_open
            )

    def _run_batch(self, batch: _TaskBatch) -> None:
        for index, action in enumerate(batch.actions):
            if self._batch_invalid(batch):
                self._drop_remaining(batch, index)
                break
            now = self._clock.now()
            if self._action_expired(action, now):
                self._mark_expired(action, now)
                continue
            self._wait_turn()
            if self._batch_invalid(batch):
                self._drop_remaining(batch, index)
                break
            # 等待最小间隔期间可能已过期，发送前再检查一次
            now = self._clock.now()
            if self._action_expired(action, now):
                self._mark_expired(action, now)
                continue
            if self._can_execute is not None and not self._can_execute():
                self.close_gate_and_release("foreground_changed")
                self._drop_remaining(batch, index)
                break
            try:
                self._backend.send([action])
            except InputUnavailable as exc:
                self._record_failure(exc)
                if self._failed:
                    self._drop_remaining(batch, index + 1)
                    # ISS-009：连续失败后由输入线程立即尝试释放（幂等，只释放已按下状态）
                    self._release_after_failure()
                    break
                continue
            with self._cond:
                self._stats.executed += 1
                self._consecutive_errors = 0
                self._next_ready = self._clock.now() + self._interval_locked()

    @staticmethod
    def _action_expired(action: Action, now: float) -> bool:
        return action.expires_at is not None and now > action.expires_at

    def _mark_expired(self, action: Action, now: float) -> None:
        with self._cond:
            self._stats.expired += 1
        logger.info(
            "动作已过期，丢弃: kind=%s label=%s expires_at=%.3f now=%.3f",
            action.kind.value,
            action.label,
            action.expires_at if action.expires_at is not None else 0.0,
            now,
        )

    def _drop_remaining(self, batch: _TaskBatch, start: int) -> None:
        """批次执行被停止标志/失败状态打断时，统计剩余未执行动作。"""
        remaining = max(0, len(batch.actions) - start)
        if remaining:
            with self._cond:
                self._stats.dropped += remaining
            logger.info("批次 %s 剩余 %d 个动作被丢弃", batch.label, remaining)

    def _wait_turn(self) -> None:
        with self._cond:
            delay = self._next_ready - self._clock.now()
        if delay > 0:
            self._sleeper.sleep(delay)

    def _interval_locked(self) -> float:
        base = max(0.0, self._config.min_action_interval_ms / 1000.0)
        if base <= 0:
            return 0.0
        jitter = max(0.0, min(1.0, float(self._config.interval_jitter)))
        return base * (1.0 + self._rng.uniform(-jitter, jitter))

    def _record_failure(self, exc: InputUnavailable) -> None:
        with self._cond:
            self._stats.errors += 1
            self._stats.last_error = exc
            self._consecutive_errors += 1
            reached = self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS
            if reached:
                self._failed = True
                # 失败后关闭业务门禁：后续动作一律拒绝，避免继续误发输入
                self._gate_open = False
                self._gate_reason = "input_failed"
                self._stats.rejected += self._pending_actions_locked()
                self._queue.clear()
            self._cond.notify_all()
            count = self._consecutive_errors
        logger.error("输入后端执行失败（连续 %d 次）: %s", count, exc)
        if reached:
            logger.critical(
                "连续 %d 次输入失败，停止执行动作并关闭输入门禁；"
                "上层应释放输入并进入 PAUSED（详细设计 9.2 / ISS-009）",
                count,
            )

    def _release_after_failure(self) -> None:
        """输入线程内立即尝试释放一次键鼠（连续失败后调用，失败不抛）。"""
        logger.warning("输入线程连续失败，尝试释放已按下的键鼠")
        self._process_release(_TaskBatch.release_batch(epoch=self._epoch, seq=0))

    def _process_release(self, batch: _TaskBatch) -> None:
        try:
            self._backend.release_all()
        except InputUnavailable as exc:
            with self._cond:
                self._stats.last_error = exc
                self._stats.errors += 1
            logger.error("输入线程释放键鼠失败: %s", exc)
        with self._cond:
            self._stats.releases += 1
            self._release_done = max(self._release_done, batch.release_seq)
            self._cond.notify_all()

    def _wait_release_done(self, seq: int, timeout: float) -> bool:
        if seq <= 0:
            return True
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            while self._release_done < seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.error("等待输入线程执行释放控制动作超时（seq=%d）", seq)
                    return False
                self._cond.wait(remaining)
            return True

    def _join_timeout(self) -> float:
        base = max(0.0, self._config.min_action_interval_ms / 1000.0)
        return max(2.0, 4.0 * base + 1.0)

    def _fallback_release(self, reason: str) -> None:
        """输入线程不可用时的最后兜底：本线程直接释放（正常流程不会走到）。"""
        with self._cond:
            self._stats.fallback_releases += 1
        logger.error("输入线程不可用（%s），兜底在调用线程直接释放键鼠", reason)
        try:
            self._backend.release_all()
        except InputUnavailable as exc:
            logger.error("兜底释放键鼠失败: %s", exc)


__all__ = [
    "CRITICAL_PRIORITY",
    "INPUT_THREAD_NAME",
    "MAX_CONSECUTIVE_ERRORS",
    "InputExecutor",
]
