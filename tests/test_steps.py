"""非阻塞步骤序列执行器单元测试（详细设计 10.1，评审 P0-4）。"""

from __future__ import annotations

from src.common.errors import StepFailure
from src.common.types import Action, ActionKind, MatchResult, Observation
from src.config.models import KeyMap, NormalizedRect, StepDef
from src.state_machine.steps import StepResult, StepSequenceExecutor, StepStatus

WINDOW = (1000, 500, 2560, 1600)


def make_observation(
    features: dict[str, MatchResult] | None = None,
    *,
    now: float = 0.0,
    window_rect: tuple[int, int, int, int] = WINDOW,
) -> Observation:
    return Observation(timestamp=now, window_rect=window_rect, features=features or {})


def found(feature_id: str, center: tuple[int, int] = (100, 200)) -> MatchResult:
    return MatchResult(feature_id=feature_id, found=True, score=0.95, center=center)


def make_keymap() -> KeyMap:
    return KeyMap(actions={"escape": "escape", "confirm": "enter"})


def make_executor(
    *,
    to_screen=None,
    on_step=None,
) -> tuple[StepSequenceExecutor, list[StepResult]]:
    results: list[StepResult] = []
    executor = StepSequenceExecutor(
        make_keymap(),
        to_screen=to_screen,
        on_step=results.append if on_step is None else on_step,
    )
    return executor, results


def test_empty_sequence_is_done_immediately() -> None:
    executor, results = make_executor()
    executor.start([])
    assert executor.status is StepStatus.DONE
    status, actions = executor.advance(0.0, None)
    assert status is StepStatus.DONE
    assert actions == []
    assert results == []


def test_key_step_returns_press_and_completes() -> None:
    executor, results = make_executor()
    executor.start([StepDef(action="key", key="escape")], label="start")
    status, actions = executor.advance(0.0, None)
    assert status is StepStatus.DONE
    assert [action.kind for action in actions] == [ActionKind.KEY_DOWN, ActionKind.KEY_UP]
    assert actions[0].key == "escape"
    assert "step:start:key:escape" in actions[0].label
    assert results and results[0].ok and results[0].action == "key"
    assert executor.results[0].attempts == 1


def test_key_step_uses_keymap_resolution() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="key", key="confirm")])
    _, actions = executor.advance(0.0, None)
    assert actions[0].key == "enter"


def test_key_step_requires_key_parameter() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="key")])
    status, actions = executor.advance(0.0, None)
    assert status is StepStatus.FAILED
    assert actions == []
    assert executor.last_error is not None
    assert "缺少 key" in str(executor.last_error)
    assert executor.last_error.step_index == 0


def test_wait_ms_uses_deadline_without_blocking() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="wait_ms", duration_ms=500)])
    assert executor.advance(0.0, None) == (StepStatus.RUNNING, [])
    assert executor.advance(0.3, None) == (StepStatus.RUNNING, [])
    assert executor.advance(0.49, None) == (StepStatus.RUNNING, [])
    status, actions = executor.advance(0.5, None)
    assert status is StepStatus.DONE
    assert actions == []


def test_wait_after_ms_delays_next_step() -> None:
    executor, _ = make_executor()
    steps = [
        StepDef(action="wait_ms", duration_ms=100, wait_after_ms=1000),
        StepDef(action="key", key="escape"),
    ]
    executor.start(steps)
    assert executor.advance(0.0, None) == (StepStatus.RUNNING, [])
    # 第一步完成（还要等 wait_after），此时不应该执行按键
    assert executor.advance(0.1, None) == (StepStatus.RUNNING, [])
    assert executor.advance(0.5, None) == (StepStatus.RUNNING, [])
    status, actions = executor.advance(1.1, None)
    assert status is StepStatus.DONE
    assert [action.kind for action in actions] == [ActionKind.KEY_DOWN, ActionKind.KEY_UP]


def test_click_feature_found_returns_absolute_click_with_conversion() -> None:
    executor, results = make_executor(to_screen=lambda x, y: (x + 1000, y + 500))
    executor.start([StepDef(action="click_feature", feature="battle_button", timeout_ms=1000)])
    observation = make_observation({"battle_button": found("battle_button", (360, 210))})
    status, actions = executor.advance(0.0, observation)
    assert status is StepStatus.DONE
    assert len(actions) == 1
    assert actions[0].kind is ActionKind.CLICK_ABS
    assert (actions[0].x, actions[0].y) == (1360, 710)
    assert actions[0].priority == 10
    assert results[0].ok
    assert "screen" in results[0].message


def test_click_feature_applies_pixel_offset() -> None:
    executor, _ = make_executor(to_screen=lambda x, y: (x, y))
    executor.start([StepDef(action="click_feature", feature="btn", offset_x=12, offset_y=-6)])
    observation = make_observation({"btn": found("btn", (200, 300))})
    _, actions = executor.advance(0.0, observation)
    assert (actions[0].x, actions[0].y) == (212, 294)


def test_click_feature_without_to_screen_marks_identity() -> None:
    executor, results = make_executor()
    executor.start([StepDef(action="click_feature", feature="btn")])
    observation = make_observation({"btn": found("btn", (200, 300))})
    _, actions = executor.advance(0.0, observation)
    assert (actions[0].x, actions[0].y) == (200, 300)
    assert "未注入 to_screen" in results[0].message


def test_click_feature_timeout_optional_is_skipped() -> None:
    executor, results = make_executor()
    executor.start([StepDef(action="click_feature", feature="missing", timeout_ms=1000, optional=True)])
    assert executor.advance(0.0, make_observation()) == (StepStatus.RUNNING, [])
    # 默认 retries=1：第一次超时后重试，第二次超时后可选步骤跳过
    assert executor.advance(1.0, make_observation()) == (StepStatus.RUNNING, [])
    status, actions = executor.advance(2.0, make_observation())
    assert status is StepStatus.DONE
    assert actions == []
    assert results[-1].skipped
    assert executor.last_error is None


def test_click_feature_timeout_required_fails_without_raising() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="click_feature", feature="missing", timeout_ms=1000)])
    executor.advance(0.0, make_observation())
    assert executor.advance(1.0, make_observation()) == (StepStatus.RUNNING, [])
    status, actions = executor.advance(2.0, make_observation())
    assert status is StepStatus.FAILED
    assert actions == []
    assert isinstance(executor.last_error, StepFailure)
    # 失败后继续 advance 保持 FAILED，不重复触发
    assert executor.advance(2.0, make_observation()) == (StepStatus.FAILED, [])


def test_click_feature_retry_then_success() -> None:
    executor, results = make_executor()
    executor.start([StepDef(action="click_feature", feature="btn", timeout_ms=1000, retries=1)])
    assert executor.advance(0.0, make_observation()) == (StepStatus.RUNNING, [])
    # 第一次超时 -> 重试，计时重置
    assert executor.advance(1.0, make_observation()) == (StepStatus.RUNNING, [])
    assert results == []
    assert executor.advance(1.5, make_observation()) == (StepStatus.RUNNING, [])
    observation = make_observation({"btn": found("btn", (50, 60))})
    status, actions = executor.advance(1.6, observation)
    assert status is StepStatus.DONE
    assert len(actions) == 1
    assert results[-1].attempts == 2


def test_wait_feature_success_and_running() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="wait_feature", feature="queue", timeout_ms=3000, retries=0)])
    assert executor.advance(0.0, make_observation()) == (StepStatus.RUNNING, [])
    assert executor.advance(2.0, make_observation()) == (StepStatus.RUNNING, [])
    status, actions = executor.advance(2.9, make_observation({"queue": found("queue")}))
    assert status is StepStatus.DONE
    assert actions == []


def test_wait_feature_absent() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="wait_feature_absent", feature="loading", timeout_ms=1000, retries=0)])
    status, _ = executor.advance(0.0, make_observation())
    assert status is StepStatus.DONE
    executor.start([StepDef(action="wait_feature_absent", feature="loading", timeout_ms=1000, retries=0)])
    assert executor.advance(0.0, make_observation({"loading": found("loading")})) == (StepStatus.RUNNING, [])
    status, _ = executor.advance(1.0, make_observation({"loading": found("loading")}))
    assert status is StepStatus.FAILED


def test_wait_feature_absent_requires_usable_frame() -> None:
    """没有可用感知帧时不能确认“特征消失”，应继续等待而不是误判成功。"""
    executor, _ = make_executor()
    executor.start([StepDef(action="wait_feature_absent", feature="panel", timeout_ms=1000, retries=0)])
    assert executor.advance(0.0, None) == (StepStatus.RUNNING, [])
    assert executor.advance(0.5, None) == (StepStatus.RUNNING, [])
    status, _ = executor.advance(1.0, None)
    assert status is StepStatus.FAILED


def test_scroll_step_returns_wheel_action() -> None:
    executor, results = make_executor()
    region = NormalizedRect(0.1, 0.2, 0.4, 0.8)
    executor.start([StepDef(action="scroll", scroll_clicks=-3, region=region)])
    status, actions = executor.advance(0.0, None)
    assert status is StepStatus.DONE
    assert len(actions) == 1
    assert actions[0].kind is ActionKind.MOUSE_WHEEL
    assert actions[0].wheel_delta == -3
    assert "region" in results[0].message


def test_scroll_zero_clicks_is_noop_success() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="scroll", scroll_clicks=0)])
    assert executor.advance(0.0, None) == (StepStatus.DONE, [])


def test_unsupported_action_fails_gracefully() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="teleport")])
    status, actions = executor.advance(0.0, None)
    assert status is StepStatus.FAILED
    assert actions == []
    assert executor.last_error is not None
    assert "不支持" in str(executor.last_error)


def test_sequence_progresses_through_all_steps() -> None:
    executor, _ = make_executor(to_screen=lambda x, y: (x, y))
    steps = [
        StepDef(action="key", key="escape"),
        StepDef(action="wait_ms", duration_ms=10),
        StepDef(action="click_feature", feature="btn"),
        StepDef(action="scroll", scroll_clicks=2),
    ]
    executor.start(steps, label="exit")
    observation = make_observation({"btn": found("btn", (10, 20))})
    all_actions: list[Action] = []
    status = StepStatus.RUNNING
    now = 0.0
    while status is StepStatus.RUNNING and now < 10.0:
        status, actions = executor.advance(now, observation)
        all_actions.extend(actions)
        now += 0.1
    assert status is StepStatus.DONE
    assert executor.index == 4
    kinds = [action.kind for action in all_actions]
    assert ActionKind.KEY_DOWN in kinds
    assert ActionKind.CLICK_ABS in kinds
    assert ActionKind.MOUSE_WHEEL in kinds


def test_start_resets_previous_sequence_state() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="key", key="escape")])
    executor.advance(0.0, None)
    assert executor.status is StepStatus.DONE
    executor.start([StepDef(action="wait_ms", duration_ms=100)])
    assert executor.status is StepStatus.RUNNING
    assert executor.index == 0
    assert executor.results == ()


def test_reset_clears_state() -> None:
    executor, _ = make_executor()
    executor.start([StepDef(action="wait_ms", duration_ms=100)])
    executor.advance(0.0, None)
    executor.reset()
    assert executor.status is StepStatus.DONE
    assert not executor.active
    assert executor.current_step is None
    assert executor.last_error is None
    assert executor.advance(1.0, None) == (StepStatus.DONE, [])
