"""输入线程与动作队列单元测试（详细设计 9.4 / 13.5，Lead 评审纠偏第 4/5 项）。

全部使用假后端 + FakeClock/FakeSleeper，不真正发送键鼠事件、不真正睡眠。
覆盖：串行执行、批次原子顺序、最小间隔抖动、过期丢弃、优先级丢弃策略、
释放动作只由输入线程执行、stop 清空队列、连续失败状态。
"""

from __future__ import annotations

import random
import threading
import time

import pytest
from src.common.clock import FakeClock, FakeSleeper
from src.common.errors import InputUnavailable
from src.common.types import Action
from src.config.models import InputConfig
from src.execution.queue import (
    INPUT_THREAD_NAME,
    MAX_CONSECUTIVE_ERRORS,
    InputExecutor,
)


class RecordingBackend:
    """记录动作与释放调用的假后端；可阻塞第一个 send 用于测试并发/停止。"""

    def __init__(self) -> None:
        self.actions: list[Action] = []
        self.send_threads: list[str] = []
        self.release_threads: list[str] = []
        self.release_count = 0
        self.release_processed = threading.Event()
        # 阻塞控制：启用后第一个 send 阻塞，直到 unblock 被设置
        self.block_enabled = threading.Event()
        self.block_entered = threading.Event()
        self.unblock = threading.Event()
        self.fail_times = 0
        self._lock = threading.Lock()

    def send(self, actions) -> None:  # noqa: ANN001 - 与 InputBackend 签名一致
        self.send_threads.append(threading.current_thread().name)
        if self.block_enabled.is_set() and not self.block_entered.is_set():
            self.block_entered.set()
            if not self.unblock.wait(5):
                raise AssertionError("测试未解除后端阻塞")
        if self.fail_times > 0:
            with self._lock:
                if self.fail_times > 0:
                    self.fail_times -= 1
                    raise InputUnavailable("模拟 SendInput 失败")
        self.actions.extend(actions)

    def release_all(self) -> None:
        self.release_threads.append(threading.current_thread().name)
        self.release_count += 1
        self.release_processed.set()


@pytest.fixture
def executors():
    """创建可自动回收的 InputExecutor 工厂。"""
    created: list[InputExecutor] = []

    def factory(
        backend: RecordingBackend,
        *,
        config: InputConfig | None = None,
        clock: FakeClock | None = None,
        seed: int = 42,
        auto_start: bool = True,
    ) -> InputExecutor:
        fake_clock = clock or FakeClock()
        executor = InputExecutor(
            backend,
            config or InputConfig(),
            clock=fake_clock,
            sleeper=FakeSleeper(fake_clock),
            rng=random.Random(seed),
            auto_start=auto_start,
        )
        created.append(executor)
        return executor

    yield factory

    for executor in created:
        try:
            executor.stop(drain=False)
        finally:
            executor.join(2.0)


def wait_until(predicate, timeout: float = 2.0, message: str = "等待条件超时") -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.001)
    raise AssertionError(message)


def labels(actions: list[Action]) -> list[str]:
    return [action.label for action in actions]


# ---------------------------------------------------------------------------
# 基本执行
# ---------------------------------------------------------------------------


def test_submit_batch_executes_in_order(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    first = Action.move_rel(1, 1, label="first")
    second = Action.move_rel(2, 2, label="second")
    third = Action.wait(1, label="third")

    executor.submit([first, second])
    executor.submit(third)

    assert executor.wait_until_idle(2.0)
    assert backend.actions == [first, second, third]
    assert all(name == INPUT_THREAD_NAME for name in backend.send_threads)
    assert executor.executed_actions == 3


def test_submit_accepts_single_action(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend)
    action = Action.move_rel(10, 20, label="single")
    executor.submit(action)
    assert executor.wait_until_idle(2.0)
    assert backend.actions == [action]


def test_batch_does_not_interleave_with_other_batches(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    a1 = Action.move_rel(1, 1, label="a1")
    a2 = Action.move_rel(2, 2, label="a2")
    b1 = Action.move_rel(3, 3, label="b1")
    executor.submit([a1, a2])
    executor.submit([b1])
    assert executor.wait_until_idle(2.0)
    assert labels(backend.actions) == ["a1", "a2", "b1"]


def test_submit_rejects_non_action(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend)
    with pytest.raises(TypeError, match="只接受 Action"):
        executor.submit([object()])  # type: ignore[list-item]


def test_queue_max_must_be_positive() -> None:
    with pytest.raises(ValueError, match="queue_max"):
        InputExecutor(RecordingBackend(), InputConfig(queue_max=0), auto_start=False)


# ---------------------------------------------------------------------------
# 最小间隔与抖动（注入 Clock + 可复现 RNG，不真正 sleep）
# ---------------------------------------------------------------------------


def test_min_interval_with_jitter_is_deterministic(executors) -> None:
    backend = RecordingBackend()
    clock = FakeClock()
    seed = 7
    executor = executors(
        backend,
        config=InputConfig(min_action_interval_ms=150, interval_jitter=0.2),
        clock=clock,
        seed=seed,
    )
    executor.submit([Action.wait(1), Action.wait(1), Action.wait(1)])
    assert executor.wait_until_idle(2.0)

    expected_rng = random.Random(seed)
    expected_draws = [0.150 * (1.0 + expected_rng.uniform(-0.2, 0.2)) for _ in range(3)]
    expected_elapsed = expected_draws[0] + expected_draws[1]
    assert clock.now() == pytest.approx(expected_elapsed)
    bucket = 0.150 * 0.2
    assert 0.150 - bucket <= expected_draws[0] <= 0.150 + bucket
    assert 0.150 - bucket <= expected_draws[1] <= 0.150 + bucket


def test_zero_interval_does_not_advance_clock(executors) -> None:
    backend = RecordingBackend()
    clock = FakeClock()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0), clock=clock)
    executor.submit([Action.move_rel(1, 1), Action.move_rel(1, 1)])
    assert executor.wait_until_idle(2.0)
    assert clock.now() == 0.0


# ---------------------------------------------------------------------------
# 过期动作
# ---------------------------------------------------------------------------


def test_expired_action_is_dropped(executors) -> None:
    backend = RecordingBackend()
    clock = FakeClock(start=100.0)
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0), clock=clock)

    expired = Action.wait(1, label="expired").with_expiry(99.0)
    boundary = Action.wait(1, label="boundary").with_expiry(100.0)  # now == expires_at 不算过期
    normal = Action.wait(1, label="normal")

    executor.submit([expired, boundary, normal])
    assert executor.wait_until_idle(2.0)

    assert labels(backend.actions) == ["boundary", "normal"]
    assert executor.expired_actions == 1
    assert executor.executed_actions == 2


def test_action_expiring_during_interval_wait_is_dropped(executors) -> None:
    """等待最小间隔期间越过 expires_at 的动作也不能发送（避免补发过期点击）。"""
    backend = RecordingBackend()
    clock = FakeClock(start=100.0)
    executor = executors(
        backend,
        config=InputConfig(min_action_interval_ms=150, interval_jitter=0.2),
        clock=clock,
    )
    first = Action.wait(1, label="first").with_expiry(200.0)
    second = Action.wait(1, label="second").with_expiry(100.1)

    executor.submit([first, second])
    assert executor.wait_until_idle(2.0)

    assert labels(backend.actions) == ["first"]
    assert executor.expired_actions == 1
    assert clock.now() > 100.1


# ---------------------------------------------------------------------------
# 队列积压丢弃策略：丢最早的 priority<20，保留 priority>=20
# ---------------------------------------------------------------------------


def test_overflow_drops_earliest_low_priority_batch(executors) -> None:
    backend = RecordingBackend()
    backend.block_enabled.set()
    executor = executors(backend, config=InputConfig(queue_max=3, min_action_interval_ms=0))

    first = Action.move_rel(0, 0, label="first")
    executor.submit([first])
    assert backend.block_entered.wait(2.0)

    low1 = Action.move_rel(1, 1, label="low1")
    low2 = Action.move_rel(2, 2, label="low2")
    high = Action.key_up("m", label="high")
    new_low = Action.move_rel(3, 3, label="new_low")
    executor.submit([low1, low2])
    executor.submit([high])
    executor.submit([new_low])

    backend.unblock.set()
    assert executor.wait_until_idle(2.0)
    assert labels(backend.actions) == ["first", "high", "new_low"]
    assert executor.dropped_actions == 2, "被丢弃的是最早的 priority<20 批次 [low1, low2]"


def test_overflow_preserves_critical_actions_with_soft_limit(executors) -> None:
    backend = RecordingBackend()
    backend.block_enabled.set()
    executor = executors(backend, config=InputConfig(queue_max=2, min_action_interval_ms=0))

    executor.submit([Action.move_rel(0, 0, label="first")])
    assert backend.block_entered.wait(2.0)
    executor.submit([Action.key_up("m", label="crit1")])
    executor.submit([Action.key_up("a", label="crit2")])
    executor.submit([Action.key_up("b", label="crit3")])  # 只能软超限，关键动作不能丢

    backend.unblock.set()
    assert executor.wait_until_idle(2.0)
    assert labels(backend.actions) == ["first", "crit1", "crit2", "crit3"]
    assert executor.dropped_actions == 0


def test_overflow_drops_incoming_low_priority_when_all_pending_critical(executors) -> None:
    backend = RecordingBackend()
    backend.block_enabled.set()
    executor = executors(backend, config=InputConfig(queue_max=1, min_action_interval_ms=0))

    executor.submit([Action.move_rel(0, 0, label="first")])
    assert backend.block_entered.wait(2.0)
    executor.submit([Action.key_up("m", label="critical")])
    executor.submit([Action.move_rel(1, 1, label="incoming_low")])

    backend.unblock.set()
    assert executor.wait_until_idle(2.0)
    assert labels(backend.actions) == ["first", "critical"]
    assert executor.dropped_actions == 1


# ---------------------------------------------------------------------------
# release_all 只由输入线程执行
# ---------------------------------------------------------------------------


def test_release_all_runs_on_input_thread(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend)
    executor.submit([Action.key_down("w", label="hold_w")])
    assert executor.wait_until_idle(2.0)

    executor.release_all()
    assert backend.release_count == 1
    assert backend.release_threads == [INPUT_THREAD_NAME]
    assert executor.release_count == 1


def test_release_all_from_watchdog_thread_still_runs_on_input_thread(executors) -> None:
    """看门狗等其它线程只发请求，SendInput 仍由输入线程执行（Lead 评审第 4 条）。"""
    backend = RecordingBackend()
    executor = executors(backend)

    caller = threading.Thread(target=executor.release_all, name="fake-watchdog")
    caller.start()
    caller.join(2.0)

    assert not caller.is_alive()
    assert backend.release_threads == [INPUT_THREAD_NAME]
    assert "fake-watchdog" not in backend.release_threads


# ---------------------------------------------------------------------------
# stop：清空队列、释放键鼠、之后不再执行动作
# ---------------------------------------------------------------------------


def test_stop_clears_pending_and_releases_from_input_thread(executors) -> None:
    backend = RecordingBackend()
    backend.block_enabled.set()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))

    in_flight = Action.move_rel(0, 0, label="in_flight")
    executor.submit([in_flight])
    assert backend.block_entered.wait(2.0)
    executor.submit([Action.move_rel(1, 1, label="pending1"), Action.move_rel(2, 2, label="pending2")])

    stopper = threading.Thread(target=executor.stop, kwargs={"drain": True}, name="stopper")
    stopper.start()
    wait_until(lambda: executor.stop_requested, message="stop 未生效")
    backend.unblock.set()
    stopper.join(5.0)

    assert not stopper.is_alive()
    assert labels(backend.actions) == ["in_flight"], "排队动作必须被清空，不再执行"
    assert backend.release_count >= 1
    assert backend.release_threads
    assert all(name == INPUT_THREAD_NAME for name in backend.release_threads)
    assert executor.pending_actions == 0
    assert executor.executed_actions == 1

    # stop 之后提交的动作被拒绝
    executor.submit([Action.move_rel(9, 9, label="after_stop")])
    assert executor.wait_until_idle(1.0)
    assert labels(backend.actions) == ["in_flight"]
    assert executor.rejected_actions >= 1


def test_stop_drain_false_returns_immediately_and_release_still_happens(executors) -> None:
    backend = RecordingBackend()
    backend.block_enabled.set()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    executor.submit([Action.move_rel(0, 0, label="in_flight")])
    assert backend.block_entered.wait(2.0)
    executor.submit([Action.move_rel(1, 1, label="pending")])

    started = time.monotonic()
    executor.stop(drain=False)  # 不等待输入线程退出
    assert time.monotonic() - started < 1.0
    assert executor.stop_requested

    backend.unblock.set()
    assert backend.release_processed.wait(2.0), "释放仍必须由输入线程执行"
    assert executor.join(2.0)
    assert labels(backend.actions) == ["in_flight"]
    assert all(name == INPUT_THREAD_NAME for name in backend.release_threads)


def test_stop_is_idempotent(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend)
    executor.submit([Action.move_rel(1, 1)])
    assert executor.wait_until_idle(2.0)

    executor.stop()
    first_release_count = backend.release_count
    assert not executor.is_running
    executor.stop()  # 重复停止不应抛异常、不应在调用线程发送输入
    assert not executor.is_running
    assert backend.release_count >= first_release_count
    assert all(name == INPUT_THREAD_NAME for name in backend.release_threads)
    assert executor.fallback_release_count == 0


def test_submit_after_stop_is_ignored(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    executor.stop()
    executor.submit([Action.move_rel(1, 1, label="late")])
    assert executor.wait_until_idle(1.0)
    assert backend.actions == []
    assert executor.rejected_actions == 1


def test_epoch_increments_on_stop(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend)
    before = executor.epoch
    executor.stop()
    assert executor.epoch == before + 1


def test_restart_after_stop_executes_new_actions(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    executor.submit([Action.move_rel(1, 1, label="before")])
    assert executor.wait_until_idle(2.0)
    executor.stop()
    assert not executor.is_running

    executor.start()
    executor.submit([Action.move_rel(2, 2, label="after")])
    assert executor.wait_until_idle(2.0)
    assert labels(backend.actions) == ["before", "after"]


# ---------------------------------------------------------------------------
# 连续失败：置 failed，停止执行动作
# ---------------------------------------------------------------------------


def test_consecutive_failures_mark_executor_failed(executors) -> None:
    backend = RecordingBackend()
    backend.fail_times = 10
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    executor.submit([Action.move_rel(1, 1), Action.move_rel(2, 2), Action.move_rel(3, 3)])

    wait_until(lambda: executor.failed, message="连续失败未进入 failed 状态")
    assert executor.consecutive_errors >= MAX_CONSECUTIVE_ERRORS
    assert executor.last_error is not None
    assert executor.executed_actions == 0
    assert executor.error_count >= MAX_CONSECUTIVE_ERRORS

    executor.submit([Action.move_rel(4, 4, label="after_failed")])
    assert executor.rejected_actions >= 1
    assert backend.actions == []


def test_transient_backend_error_is_recorded_and_recovers(executors) -> None:
    backend = RecordingBackend()
    backend.fail_times = 1
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    good = Action.move_rel(2, 2, label="good")
    executor.submit([Action.move_rel(1, 1, label="bad"), good])

    assert executor.wait_until_idle(2.0)
    assert backend.actions == [good]
    assert executor.failed is False
    assert executor.consecutive_errors == 0
    assert executor.error_count == 1
    assert executor.executed_actions == 1


# ---------------------------------------------------------------------------
# ISS-004 统一输入门禁：暂停/停止后旧动作失效、新动作被拒绝、恢复后只执行新动作
# ---------------------------------------------------------------------------


def test_close_gate_rejects_new_actions_and_releases(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    executor.submit([Action.key_down("w", label="hold_w")])
    assert executor.wait_until_idle(2.0)
    assert executor.gate_open

    executor.close_gate_and_release("pause", wait=True)
    assert not executor.gate_open
    assert executor.gate_reason == "pause"
    assert backend.release_count == 1
    releases_before = executor.release_count

    executor.submit([Action.move_rel(3, 3, label="after_pause")])
    assert executor.wait_until_idle(1.0)
    assert labels(backend.actions) == ["hold_w"]
    assert executor.rejected_actions >= 1
    assert executor.release_count == releases_before

    assert executor.open_gate("resume")
    executor.submit([Action.move_rel(4, 4, label="after_resume")])
    assert executor.wait_until_idle(2.0)
    assert labels(backend.actions) == ["hold_w", "after_resume"]


def test_pause_cancels_queued_batches_and_invalidates_inflight(executors) -> None:
    backend = RecordingBackend()
    backend.block_enabled.set()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    executor.submit([Action.move_rel(0, 0, label="in_flight")])
    assert backend.block_entered.wait(2.0)
    executor.submit(
        [Action.move_rel(1, 1, label="pending1"), Action.move_rel(2, 2, label="pending2")]
    )

    executor.close_gate_and_release("pause", wait=False)
    backend.unblock.set()
    assert backend.release_processed.wait(3.0)
    assert executor.wait_until_idle(2.0)

    assert labels(backend.actions) == ["in_flight"], "暂停后排队动作必须全部丢弃"
    assert all(name == INPUT_THREAD_NAME for name in backend.release_threads)
    assert executor.dropped_actions >= 2


def test_pause_twice_and_resume_does_not_replay_old_actions(executors) -> None:
    backend = RecordingBackend()
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    executor.close_gate_and_release("pause1", wait=True)
    releases_after_first = backend.release_count
    executor.close_gate_and_release("pause2", wait=True)
    assert backend.release_count == releases_after_first + 1

    executor.submit([Action.move_rel(1, 1, label="queued_while_paused")])
    assert executor.open_gate("resume")
    executor.submit([Action.move_rel(2, 2, label="fresh")])
    assert executor.wait_until_idle(2.0)
    assert labels(backend.actions) == ["fresh"]


# ---------------------------------------------------------------------------
# ISS-009 连续失败后立即释放，并可显式重置后恢复
# ---------------------------------------------------------------------------


def test_consecutive_failures_release_and_reset(executors) -> None:
    backend = RecordingBackend()
    backend.fail_times = 10
    executor = executors(backend, config=InputConfig(min_action_interval_ms=0))
    executor.submit([Action.move_rel(1, 1), Action.move_rel(2, 2), Action.move_rel(3, 3)])

    wait_until(lambda: executor.failed, message="连续失败未进入 failed 状态")
    wait_until(lambda: backend.release_count >= 1, message="失败后输入线程未尝试释放")
    assert not executor.gate_open
    assert executor.gate_reason == "input_failed"
    assert backend.actions == []

    executor.submit([Action.move_rel(4, 4, label="after_failed")])
    assert executor.wait_until_idle(1.0)
    assert labels(backend.actions) == []

    backend.fail_times = 0
    assert executor.reset_after_failure()
    assert executor.failed is False
    assert executor.gate_open
    executor.submit([Action.move_rel(5, 5, label="recovered")])
    assert executor.wait_until_idle(2.0)
    assert labels(backend.actions) == ["recovered"]
