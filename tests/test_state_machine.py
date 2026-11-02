"""状态机主链路离线测试（用假依赖驱动，不需要游戏/不需要真实键鼠）。"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.common.clock import FakeClock
from src.common.errors import StopRequest
from src.common.types import (
    Action,
    DeathSignals,
    MatchResult,
    NormalizedRect,
    Observation,
    Rect,
    State,
    WindowInfo,
)
from src.config.models import (
    AppConfig,
    BattleHudConfig,
    ClientProfile,
    ExitConfig,
    FeatureDef,
    GlobalConfig,
    KeyMap,
    MapPack,
    SafetyConfig,
    SessionConfig,
    ShipProfile,
    SpawnPoint,
    StepDef,
    WindowConfig,
)
from src.execution.safety import StopController
from src.state_machine.accounts import AccountRotator
from src.state_machine.machine import MachineDeps, StateMachine
from src.state_machine.session import SessionController
from src.state_machine.ship_check import ShipCheckStatus
from src.state_machine.steps import StepSequenceExecutor, StepStatus

# ---------------------------------------------------------------------------
# 假依赖
# ---------------------------------------------------------------------------


class FakePerception:
    def __init__(self) -> None:
        self.state = State.INIT
        self.features: dict[str, MatchResult] = {}
        self.detected: State | None = None
        self.death: DeathSignals | None = None
        self.map_match = None
        self.calibration = None
        self.begin_battle_calls = 0
        self.identify_map_calls = 0
        self.last_frame = None

    def set_state(self, state: State) -> None:
        self.state = state

    def observe(self, state: State | None = None, now: float | None = None) -> Observation:
        return Observation(
            timestamp=now or 0.0,
            window_rect=(0, 0, 2560, 1600),
            features=dict(self.features),
            death=self.death,
            frame_id=1,
            frame_timestamp=now or 0.0,
        )

    def match_feature(self, feature_id: str, frame=None, now=None) -> MatchResult:
        return self.features.get(feature_id, MatchResult(feature_id, False, 0.0))

    def has_feature(self, feature_id: str) -> bool:
        return True

    def detect_state(self, now: float | None = None) -> State | None:
        return self.detected

    def identify_map(self, now: float | None = None):
        self.identify_map_calls += 1
        return self.map_match

    def force_map(self, map_id: str, now: float | None = None):
        return self.map_match

    def begin_battle(self, now: float) -> None:
        self.begin_battle_calls += 1

    def end_battle(self) -> None:
        pass

    def minimap_content_rect(self, frame=None) -> Rect:
        return Rect(10, 10, 200, 200)

    def locate_own_ship(self, now: float | None = None, frame=None):
        return None

    def check_death(self, now: float | None = None, frame=None, features=None, ship=None) -> DeathSignals:
        return self.death or DeathSignals(False, False, False, False, False, 0.0, 0, timestamp=now or 0.0)


class FakeEvents:
    def __init__(self) -> None:
        self.session_id = 11
        self.battle_id = 21
        self.states: list[str] = []
        self.actions: list[tuple[str, dict]] = []
        self.battles: list[tuple] = []
        self.ended_sessions: list[tuple] = []
        self.saved_profiles: list[dict] = []
        self.context_calls: list[dict] = []
        self.battle_marks: list[str] = []
        self.account_alias: str | None = None

    def begin_session(self, profile_id, config_hash, account_alias=None) -> int:
        self.account_alias = account_alias
        return self.session_id

    def end_session(self, session_id, stop_reason, total_battles) -> None:
        self.ended_sessions.append((stop_reason, total_battles))

    def begin_battle(self, session_id, map_id=None, account_alias=None) -> int:
        self.battle_id += 1
        return self.battle_id

    def set_battle_context(self, battle_id, **kwargs) -> None:
        self.context_calls.append({"battle_id": battle_id, **kwargs})

    def save_random_profile(self, battle_id, profile) -> int:
        self.saved_profiles.append(dict(profile))
        return 1

    def mark_queued(self, battle_id) -> None:
        self.battle_marks.append("queued")

    def mark_death(self, battle_id) -> None:
        self.battle_marks.append("death")

    def mark_exit_requested(self, battle_id) -> None:
        self.battle_marks.append("exit_requested")

    def mark_returned_to_port(self, battle_id) -> None:
        self.battle_marks.append("returned")

    def end_battle(self, battle_id, result, exit_method, exit_error=None) -> None:
        self.battles.append((result, exit_method, exit_error))

    def log_action(self, battle_id, action_type, detail, random_profile_id=None) -> None:
        self.actions.append((action_type, dict(detail or {})))

    def log_state(self, session_id, battle_id, state, confidence=None, detail=None) -> None:
        self.states.append(state)

    def count_battles_on(self, day) -> int:
        return 0


class FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[Action] = []
        self.stopped = False
        self.released = 0
        self.gate_open = True

    def submit(self, actions) -> None:
        self.submitted.extend(actions)

    def release_all(self) -> None:
        self.released += 1

    def close_gate_and_release(self, reason: str = "", *, wait: bool = False, timeout: float = 2.0) -> int:
        self.gate_open = False
        self.released += 1
        return 1

    def open_gate(self, reason: str = "") -> bool:
        self.gate_open = True
        return True

    def reset_after_failure(self) -> bool:
        return True

    def stop(self, drain: bool = True) -> None:
        self.stopped = True

    @property
    def is_running(self) -> bool:
        return not self.stopped


class FakeAutopilot:
    """autopilot 简化导航的假实现。"""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.ticks = 0

    def start(self, now: float) -> None:
        self.started += 1

    def tick(self, observation, now: float) -> list[Action]:
        self.ticks += 1
        return [Action.click_abs(10, 10, label="autopilot_click")]

    def stop(self) -> None:
        self.stopped += 1


class FakeSession:
    def __init__(self) -> None:
        self.stop_requests: list[StopRequest] = []
        self.start_failures = 0
        self.successes = 0
        self.battle_starts = 0
        self.battle_ends = 0
        self.battle_aborts: list[str] = []
        self.battle_active = False
        self._stop: StopRequest | None = None

    def start(self):
        return None

    def stop(self, reason, detail="") -> StopRequest:
        request = StopRequest(str(reason), detail)
        if self._stop is None:
            self._stop = request
        self.stop_requests.append(request)
        return request

    def begin_battle(self) -> None:
        self.battle_starts += 1
        self.battle_active = True

    def end_battle(self, result="") -> float:
        self.battle_ends += 1
        self.battle_active = False
        return 0.0

    def abort_battle(self, reason: str = "") -> float | None:
        if not self.battle_active:
            return None
        self.battle_active = False
        self.battle_aborts.append(reason)
        return 0.0

    def check_stop(self):
        return None

    def sample_between_battle_s(self) -> float:
        return 0.0

    def record_start_success(self) -> None:
        self.successes += 1

    def record_start_failure(self, detail=""):
        self.start_failures += 1
        return None

    @property
    def battles_completed(self) -> int:
        return self.battle_ends

    @property
    def battle_in_progress(self) -> bool:
        return self.battle_active


class FakeSteps:
    def __init__(self, outcomes: list[StepStatus] | None = None) -> None:
        self.status = StepStatus.DONE
        self.outcomes = list(outcomes or [])
        self.label = ""
        self.started = 0
        self.advanced = 0
        self.reset_count = 0

    def start(self, steps, *, label: str = "") -> None:
        self.label = label
        self.status = StepStatus.RUNNING
        self.started += 1

    def advance(self, now, observation=None):
        self.advanced += 1
        if self.outcomes:
            self.status = self.outcomes.pop(0)
        else:
            self.status = StepStatus.DONE
        return self.status, [Action.click_abs(1, 1, label=self.label or "step")] if self.status is StepStatus.DONE else []

    def reset(self) -> None:
        self.reset_count += 1
        self.status = StepStatus.DONE


class FakeNavigator:
    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.ticks = 0

    def start(self, route, calibration, now) -> None:
        self.started += 1

    def tick(self, observation, now) -> list[Action]:
        self.ticks += 1
        return []

    def stop(self) -> None:
        self.stopped += 1


class FakeBehavior:
    def __init__(self, fail: bool = False) -> None:
        self.started = 0
        self.stopped = 0
        self.fail = fail

    def start(self, profile, now) -> None:
        self.started += 1

    def tick(self, observation, now, context=None) -> list[Action]:
        if self.fail:
            raise RuntimeError("behavior boom")
        return []

    def stop(self) -> None:
        self.stopped += 1


class FakeScreenshots:
    def __init__(self) -> None:
        self.begun_sessions: list[int] = []
        self.begun_battles: list[int] = []
        self.ended_battles: list[int] = []
        self.diagnostics: list[str] = []

    def begin_session(self, session_id=None) -> Path:
        self.begun_sessions.append(session_id or 0)
        return Path("screenshots/live")

    def begin_battle(self, battle_id: int) -> Path:
        self.begun_battles.append(battle_id)
        return Path("screenshots/live") / str(battle_id)

    def end_battle(self, battle_id: int) -> bool:
        self.ended_battles.append(battle_id)
        return True

    def cleanup_stale_live(self) -> list[Path]:
        return []

    def cleanup_session(self) -> bool:
        return True

    def save_diagnostic(self, kind: str, image) -> Path | None:
        self.diagnostics.append(kind)
        return None


class FakeShipChecker:
    def __init__(self, status: ShipCheckStatus = ShipCheckStatus.OK) -> None:
        self.verify_each_battle = True
        self.status = status
        self.failure_reason = ""
        self.resets = 0

    def reset(self) -> None:
        self.resets += 1

    def tick(self, now, observation):
        return self.status, []


# ---------------------------------------------------------------------------
# 构造配置与状态机
# ---------------------------------------------------------------------------


def make_app(tmp_path: Path, *, maps: dict[str, MapPack] | None = None) -> AppConfig:
    global_config = GlobalConfig()
    feature = FeatureDef(
        feature_id="port_indicator",
        template="features/port_indicator.png",
        region=NormalizedRect(0, 0, 0.3, 0.2),
    )
    profile = ClientProfile(
        client_id="test",
        display_name="test",
        window=WindowConfig(process_names=("game.exe",), title_patterns=("game",)),
        language="zh_cn",
        reference_resolution=(2560, 1600),
        features={"port_indicator": feature},
        start_sequence=(StepDef(action="wait_ms", duration_ms=10),),
        start_sequence_full=(StepDef(action="wait_ms", duration_ms=10),),
        result_sequence=(StepDef(action="wait_ms", duration_ms=10),),
        popup_sequence=(StepDef(action="click_feature", feature="popup_close_button"),),
        ship_selection_sequence=(StepDef(action="wait_ms", duration_ms=10),),
        popup_features=("reward_panel",),
        exit=ExitConfig(
            menu_key="escape",
            menu_open_feature="exit_menu_panel",
            return_port_feature="return_port",
            confirm_feature="exit_confirm",
            port_feature="port_indicator",
        ),
        battle_hud=BattleHudConfig(
            result_feature="battle_results",
            result_continue_feature="result_continue_battle_button",
            result_return_feature="result_return_port_button",
        ),
    )
    ship = ShipProfile(ship_id="test_ship", display_name="测试船")
    keymap = KeyMap(actions={"escape": "escape", "fire": "mouse_left", "full_map": "m"})
    if maps is None:
        maps = {
            "test_map": MapPack(
                map_id="test_map",
                name="测试地图",
                size_meters=(12000.0, 12000.0),
                world_bounds=(-6000.0, -6000.0, 6000.0, 6000.0),
                image_bounds=(0.0, 0.0, 1760.0, 1760.0),
                spawns=(SpawnPoint("east", (4200.0, -3500.0), 270.0),),
                routes=(),
            )
        }
    return AppConfig(
        global_config=global_config,
        profile=profile,
        keymap=keymap,
        ship=ship,
        maps=maps,
        project_root=tmp_path,
        config_hash="testhash",
    )


class Harness:
    def __init__(self, tmp_path: Path, *, steps: FakeSteps | None = None, behavior: FakeBehavior | None = None,
                 ship_checker: FakeShipChecker | None = None) -> None:
        self.clock = FakeClock()
        self.app = make_app(tmp_path)
        self.perception = FakePerception()
        self.events = FakeEvents()
        self.executor = FakeExecutor()
        self.steps = steps or FakeSteps()
        self.navigator = FakeNavigator()
        self.autopilot = FakeAutopilot()
        self.behavior = behavior or FakeBehavior()
        self.session = FakeSession()
        self.screenshots = FakeScreenshots()
        self.ship_checker = ship_checker or FakeShipChecker()
        self.controller = StopController()
        self.battle_contexts: list[int | None] = []
        self.machine = StateMachine(
            MachineDeps(
                config=self.app,
                clock=self.clock,
                perception=self.perception,  # type: ignore[arg-type]
                executor=self.executor,  # type: ignore[arg-type]
                events=self.events,  # type: ignore[arg-type]
                steps=self.steps,  # type: ignore[arg-type]
                navigator=self.navigator,  # type: ignore[arg-type]
                behavior=self.behavior,
                session=self.session,  # type: ignore[arg-type]
                screenshots=self.screenshots,  # type: ignore[arg-type]
                ship_checker=self.ship_checker,  # type: ignore[arg-type]
                autopilot=self.autopilot,
                controller=self.controller,
                on_battle_context=self.battle_contexts.append,
            )
        )
        self.machine.update_window_info(
            WindowInfo(hwnd=1, title="game", process_name="game.exe", client_rect=Rect(0, 0, 2560, 1600),
                       foreground=True, minimized=False, valid=True)
        )

    def obs(self, now: float | None = None) -> Observation:
        return self.perception.observe(state=self.machine.state, now=now or self.clock.now())

    def step(self, now: float | None = None) -> list[Action]:
        moment = self.clock.now() if now is None else now
        return self.machine.step(self.obs(moment), moment)

    def tick_until(self, target: State, *, max_ticks: int = 20) -> list[Action]:
        actions: list[Action] = []
        for _ in range(max_ticks):
            if self.machine.state is target:
                return actions
            self.clock.advance(0.5)
            actions.extend(self.step())
        raise AssertionError(f"状态未到达 {target.value}，当前 {self.machine.state.value}")


def _match(feature_id: str, found: bool = True) -> MatchResult:
    return MatchResult(feature_id, found, 0.95, center=(100, 100), rect=(90, 90, 20, 20), scale=1.0)


def _death_signals(
    now: float, *, authorized: bool = True, banner: bool = False, hp_evidence: str | None = None
) -> DeathSignals:
    """构造一帧死亡信号；ISS-002 后退出只认 exit_authorized（血条确认归零证据）。"""
    return DeathSignals(
        hp_bar_absent=False,
        hp_zero=authorized,
        banner=banner,
        marker_lost=False,
        spectator_ui=False,
        score=0.0,
        stable_ms=1500,
        timestamp=now,
        hp_fill_ratio=0.0 if authorized else 0.5,
        hp_evidence=hp_evidence or ("zero" if authorized else "unknown"),
        exit_authorized=authorized,
        evidence_at=now if authorized else 0.0,
    )


def drive_to_battle_active(h: Harness) -> None:
    """把状态机从 INIT 推到 BATTLE_ACTIVE（假数据）。"""
    h.machine.start()
    h.perception.detected = State.PORT
    h.step()  # INIT -> PORT
    h.step()  # PORT idle -> verify
    h.step()  # verify -> start
    h.step()  # start -> starting（启动步骤序列）
    h.step()  # 步骤完成 -> START_COOP
    assert h.machine.state is State.START_COOP
    h.perception.features["queue_indicator"] = _match("queue_indicator")
    h.step()  # -> QUEUEING
    assert h.machine.state is State.QUEUEING
    h.perception.features["loading_indicator"] = _match("loading_indicator")
    h.step()  # -> LOADING
    assert h.machine.state is State.LOADING
    h.perception.features["battle_hud_indicator"] = _match("battle_hud_indicator")
    h.step()  # -> BATTLE_ACTIVE
    assert h.machine.state is State.BATTLE_ACTIVE


def _frame_observation(
    now: float,
    *,
    frame_id: int,
    frame_timestamp: float,
    features: dict[str, MatchResult] | None = None,
) -> Observation:
    return Observation(
        timestamp=now,
        window_rect=(0, 0, 2560, 1600),
        features=dict(features or {}),
        frame_id=frame_id,
        frame_timestamp=frame_timestamp,
    )


def _continue_from_active_battle(h: Harness) -> None:
    """战斗内结算并点“继续战斗”，推进到下一场的 BATTLE_ACTIVE。"""
    h.perception.features = {
        "battle_results": _match("battle_results"),
        "result_continue_battle_button": _match("result_continue_battle_button"),
    }
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    h.clock.advance(0.2)
    actions = h.step()
    assert h.machine.state is State.EXITING
    assert any(action.label == "result_continue" for action in actions)
    h.perception.features = {"queue_indicator": _match("queue_indicator")}
    h.step()
    assert h.machine.state is State.START_COOP
    h.step()
    assert h.machine.state is State.QUEUEING
    h.perception.features = {"battle_hud_indicator": _match("battle_hud_indicator")}
    h.step()
    assert h.machine.state is State.BATTLE_ACTIVE


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


def test_init_to_port(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    assert h.machine.state is State.INIT
    h.perception.detected = State.PORT
    h.step()
    assert h.machine.state is State.PORT
    assert h.events.account_alias is None


def test_init_restart_mid_battle_pauses(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.perception.detected = State.BATTLE_ACTIVE
    h.step()
    assert h.machine.state is State.PAUSED


def test_port_start_sequence_flow(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.perception.detected = State.PORT
    h.step()
    assert h.machine.state is State.PORT
    # idle -> verify -> start -> starting
    h.step()
    h.step()
    h.step()
    assert h.steps.started == 1
    assert h.steps.label == "start_fast"
    # 步骤完成 -> START_COOP
    h.step()
    assert h.machine.state is State.START_COOP
    # 排队界面 -> QUEUEING
    h.perception.features["queue_indicator"] = _match("queue_indicator")
    h.step()
    assert h.machine.state is State.QUEUEING
    assert h.events.battle_id == 22
    assert h.events.battle_marks and h.events.battle_marks[0] == "queued"
    assert h.session.battle_starts == 1
    # 加载 -> LOADING
    h.perception.features["loading_indicator"] = _match("loading_indicator")
    h.step()
    assert h.machine.state is State.LOADING
    # 战斗 HUD -> BATTLE_ACTIVE
    h.perception.features["battle_hud_indicator"] = _match("battle_hud_indicator")
    h.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert h.perception.begin_battle_calls == 1


def test_death_esc_opens_confirm_dialog_directly(tmp_path: Path) -> None:
    """2026-10-31 实机回归：死亡后 ESC 直接弹“确认离开战斗?”，不等菜单也要能退出。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    # ESC 后直接出现确认框（没有“菜单→离开战斗”按钮）
    h.perception.features["exit_confirm"] = _match("exit_confirm")
    h.step()
    assert h.machine.state is State.EXIT_CONFIRMING
    h.step()
    assert h.machine.state is State.EXITING
    assert "exit_requested" in h.events.battle_marks


def test_exit_menu_timeout_saves_diagnostic(tmp_path: Path) -> None:
    """退出菜单超时要保存诊断截图，便于定位是没开菜单还是模板失配。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    h.perception.last_frame = SimpleNamespace(image=object())  # 让诊断截图有帧可存
    h.step()  # 首次发送 Esc。
    h.clock.advance(9.0)
    h.perception.death = _death_signals(h.clock.now())
    h.step()  # 超时前先触发一次 ESC 重试（并保存 retry 诊断）
    assert "exit_menu_retry" in h.screenshots.diagnostics
    h.step()  # 超过 exit_menu_s -> ERROR_RECOVERY
    assert h.machine.state is State.ERROR_RECOVERY
    assert "exit_menu_timeout" in h.screenshots.diagnostics


@pytest.mark.parametrize("visible", ["confirm", "menu", "direct"])
def test_visible_exit_interface_discards_pending_escape(tmp_path: Path, visible: str) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    if visible == "confirm":
        h.perception.features["exit_confirm"] = _match("exit_confirm")
    elif visible == "menu":
        h.perception.features["exit_menu_panel"] = _match("exit_menu_panel")
        h.perception.features["return_port"] = _match("return_port")
    else:
        h.machine.profile = replace(h.machine.profile, exit=replace(
            h.machine.profile.exit, direct_exit_feature="direct_exit"
        ))
        h.perception.features["direct_exit"] = _match("direct_exit")

    actions = h.step()

    assert not any(action.key == "escape" for action in actions)
    assert h.machine._pending_exit_actions == []
    assert h.machine.state is (State.EXITING if visible == "direct" else State.EXIT_CONFIRMING)


@pytest.mark.parametrize(
    "evidence", ["missing", "unknown", "expired", "same_frame", "before_key", "stale_frame"]
)
def test_exit_menu_retry_requires_fresh_zero_after_first_escape(tmp_path: Path, evidence: str) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features.clear()
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    first = h.obs()
    actions = h.machine.step(first, h.clock.now())
    assert any(action.label == "exit_menu" for action in actions)
    h.clock.advance(3.0)
    now = h.clock.now()
    death = _death_signals(now)
    if evidence == "missing":
        death = None
    elif evidence == "unknown":
        death = _death_signals(now, authorized=False)
    elif evidence == "expired":
        death = _death_signals(first.frame_timestamp)
    observation = replace(h.obs(), death=death, frame_id=2)
    if evidence == "same_frame":
        observation = replace(observation, frame_id=first.frame_id, frame_timestamp=first.frame_timestamp)
    elif evidence == "before_key":
        observation = replace(observation, frame_timestamp=first.frame_timestamp - 0.5)
    elif evidence == "stale_frame":
        observation = replace(observation, frame_timestamp=now - 2.0)

    actions = h.machine.step(observation, now)

    assert not any(action.key == "escape" for action in actions)
    assert h.machine.state is State.EXIT_MENU_OPEN
    assert not h.machine._exit_retried


def test_exit_menu_retry_still_works_for_new_zero_frame(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.step()  # 首次 Esc。
    h.clock.advance(3.0)
    h.perception.death = _death_signals(h.clock.now())

    actions = h.step()

    assert any(action.label == "exit_menu_retry" for action in actions)
    assert h.machine._exit_retried
    assert h.step() == []


def test_exit_menu_retry_waits_from_actual_first_escape(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(3.0)
    h.perception.death = _death_signals(h.clock.now())
    assert any(action.label == "exit_menu" for action in h.step())
    h.clock.advance(0.6)
    h.perception.death = _death_signals(h.clock.now())

    actions = h.step()

    assert actions == []
    assert not h.machine._exit_retried
    assert h.machine.state is State.EXIT_MENU_OPEN


def test_dead_exit_flow_to_port(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    # 死亡：只有“血条确认归零”证据（exit_authorized）才允许提前退出
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    assert "death" in h.events.battle_marks
    # 等待 0.8~2.0s 后进入退出菜单（每帧重新给出新鲜证据）
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    # 菜单打开 + 返回港口按钮 -> EXIT_CONFIRMING
    h.perception.features["exit_menu_panel"] = _match("exit_menu_panel")
    h.perception.features["return_port"] = _match("return_port")
    h.step()
    assert h.machine.state is State.EXIT_CONFIRMING
    assert "exit_requested" in h.events.battle_marks
    # 确认框 -> EXITING
    h.perception.features["exit_confirm"] = _match("exit_confirm")
    h.step()
    assert h.machine.state is State.EXITING
    # 回港 -> POST_EXIT_INTERFACE -> PORT
    h.perception.features["port_indicator"] = _match("port_indicator")
    h.step()
    assert h.machine.state is State.POST_EXIT_INTERFACE
    assert "returned" in h.events.battle_marks
    h.perception.features.clear()
    h.perception.features["port_indicator"] = _match("port_indicator")
    h.step()
    assert h.machine.state is State.PORT
    assert h.events.battles and h.events.battles[-1][0] == "dead"
    assert h.machine.battles_completed == 1


def test_queue_to_battle_without_loading_template(tmp_path: Path) -> None:
    """用户确认加载界面可跳过：排队后直接检测到战斗 HUD 即进战斗。"""
    h = Harness(tmp_path)
    h.machine.start()
    h.perception.detected = State.PORT
    h.step()
    h.step()
    h.step()
    h.step()
    h.step()
    assert h.machine.state is State.START_COOP
    h.perception.features["queue_indicator"] = _match("queue_indicator")
    h.step()
    assert h.machine.state is State.QUEUEING
    # 不设置 loading 特征，直接出现战斗 HUD
    h.perception.features["battle_hud_indicator"] = _match("battle_hud_indicator")
    h.step()
    assert h.machine.state is State.BATTLE_ACTIVE


def test_autopilot_mode_starts_without_map_recognition(tmp_path: Path) -> None:
    """简化需求：autopilot 模式开局后自动驾驶启动，不做地图识别。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.step()  # 战斗内 bootstrap 在进入 BATTLE_ACTIVE 后的下一轮执行
    assert h.autopilot.started == 1
    assert h.behavior.started == 1
    assert h.perception.identify_map_calls == 0  # 不需要识别地图
    h.clock.advance(0.5)
    actions = h.step()
    assert h.autopilot.ticks >= 1
    assert any(action.label == "autopilot_click" for action in actions)


def test_result_priority_over_death(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.perception.features["battle_results"] = _match("battle_results")
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    assert h.machine.battle is not None


def test_result_continue_button_prefers_next_battle(tmp_path: Path) -> None:
    """2026-10-31 用户口径：结算页有“继续战斗！”时直接点击，不经过港口继续下一局。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features["battle_results"] = _match("battle_results")
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    # 结算序列（wait_ms 10）完成 + “继续战斗！”按钮出现
    h.clock.advance(0.2)
    h.perception.features["result_continue_battle_button"] = _match("result_continue_battle_button")
    actions = h.step()
    assert h.machine.state is State.EXITING
    assert any(action.label == "result_continue" for action in actions)
    # 继续战斗后游戏直接进入排队界面（不经过港口）
    h.perception.features.clear()
    h.perception.features["queue_indicator"] = _match("queue_indicator")
    h.clock.advance(0.5)
    h.step()
    assert h.machine.state is State.START_COOP
    assert "returned" not in h.events.battle_marks
    assert h.events.battles and h.events.battles[-1][0] == "finished"
    assert h.machine.battles_completed == 1
    # START_COOP 识别到排队 -> 新一场记录 + QUEUEING
    h.step()
    assert h.machine.state is State.QUEUEING
    assert h.session.battle_starts == 2


def test_result_skips_continue_when_session_stopping(tmp_path: Path) -> None:
    """本场后触发 --max-battles 上限时不点“继续战斗”，改回港后再停止（不把游戏留在队列）。"""
    h = Harness(tmp_path)
    h.machine.set_rotation_limit(1)
    drive_to_battle_active(h)
    h.perception.features["battle_results"] = _match("battle_results")
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    h.clock.advance(0.2)
    h.perception.features["result_continue_battle_button"] = _match("result_continue_battle_button")
    h.perception.features["result_return_port_button"] = _match("result_return_port_button")
    actions = h.step()
    assert h.machine.state is State.EXITING
    labels = [action.label for action in actions]
    assert "result_continue" not in labels
    assert "result_return_port" in labels


def test_result_returns_to_port_at_global_session_cap(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    safety = replace(h.app.global_config.safety, max_battles_per_session=1)
    h.app = replace(h.app, global_config=replace(h.app.global_config, safety=safety))
    h.machine.config = h.app
    h.session = SessionController(h.app.global_config.session, safety, h.clock)
    h.machine.deps.session = h.session
    drive_to_battle_active(h)
    h.perception.features = {
        "battle_results": _match("battle_results"),
        "result_continue_battle_button": _match("result_continue_battle_button"),
        "result_return_port_button": _match("result_return_port_button"),
    }
    h.step()

    actions = h.step()

    assert any(action.label == "result_return_port" for action in actions)
    assert not any(action.label == "result_continue" for action in actions)
    h.perception.features = {"port_indicator": _match("port_indicator")}
    h.step()
    h.step()
    assert h.machine.state is State.STOPPING
    assert h.machine.battles_completed == h.session.battles_completed == 1


@pytest.mark.parametrize("phase", ["starting", "start_full"])
@pytest.mark.parametrize("visible, target", [
    ("queue_indicator", State.QUEUEING),
    ("loading_indicator", State.LOADING),
    ("battle_hud_indicator", State.BATTLE_ACTIVE),
])
def test_port_start_adopts_actual_game_progress(tmp_path: Path, phase: str, visible: str, target: State) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.perception.detected = State.PORT
    for _ in range(4):
        h.step()
    assert h.machine._port_phase == "starting"
    steps = StepSequenceExecutor(h.app.keymap)
    steps.start([StepDef(action="wait_feature", feature="queue_indicator", timeout_ms=20000)])
    h.machine.deps.steps = steps
    h.machine._port_phase = phase
    h.perception.features = {visible: _match(visible)}
    next_id = h.events.battle_id + 1

    actions = h.step()

    assert h.machine.state is target
    assert h.machine.battle_id == next_id
    assert h.session.battle_starts == 1
    assert steps.status is StepStatus.DONE
    assert actions == []
    h.step()
    assert h.events.battle_id == next_id
    assert h.session.battle_starts == 1


def test_port_start_unknown_frame_keeps_waiting(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.perception.detected = State.PORT
    for _ in range(4):
        h.step()
    steps = StepSequenceExecutor(h.app.keymap)
    steps.start([StepDef(action="wait_feature", feature="queue_indicator", timeout_ms=20000)])
    h.machine.deps.steps = steps

    assert h.step() == []
    assert h.machine.state is State.PORT
    assert h.machine.battle_id is None
    assert steps.status is StepStatus.RUNNING


def test_successful_resume_clears_old_pause_reason(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.machine.request_pause("manual")
    h.controller.resume()
    h.perception.detected = State.BATTLE_ACTIVE
    h.machine.request_resume()

    h.step()

    assert h.machine.state is State.BATTLE_ACTIVE
    assert h.machine.pause_reason == ""


@pytest.mark.parametrize("detected", [State.EXIT_MENU_OPEN, State.EXIT_CONFIRMING])
def test_gui_return_preserves_visible_exit_interface(tmp_path: Path, detected: State) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    battle_id = h.machine.battle_id
    h.machine.suspend_for_gui()
    h.perception.detected = detected
    h.perception.features = {"exit_confirm": _match("exit_confirm")}

    h.machine.continue_after_gui(h.obs(), h.clock.now())

    assert h.machine.state is detected
    assert h.machine.battle_id == battle_id
    assert not h.controller.paused
    assert h.executor.gate_open
    actions = h.step()
    assert not any(action.key == "escape" for action in actions)


@pytest.mark.parametrize("source", ["gui", "pause"])
def test_return_to_exit_dialog_does_not_authorize_living_ship_exit(tmp_path: Path, source: str) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.detected = State.EXIT_CONFIRMING
    h.perception.features = {"exit_confirm": _match("exit_confirm")}
    if source == "gui":
        h.machine.suspend_for_gui()
        h.machine.continue_after_gui(h.obs(), h.clock.now())
    else:
        h.machine.request_pause("manual")
        h.controller.resume()
        h.machine.request_resume()
        h.step()

    assert h.machine.state is State.PAUSED
    assert h.step() == []
    assert h.machine.battle_id is not None


def test_result_returns_to_port_without_continue_button(tmp_path: Path) -> None:
    """结算页没有“继续战斗！”时回退到“回到港口”。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features["battle_results"] = _match("battle_results")
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    h.clock.advance(0.2)
    h.perception.features["result_return_port_button"] = _match("result_return_port_button")
    actions = h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    assert not any(action.label == "result_return_port" for action in actions)
    h.clock.advance(20)
    actions = h.step()
    assert h.machine.state is State.EXITING
    assert any(action.label == "result_return_port" for action in actions)
    # 回到港口 -> POST_EXIT_INTERFACE -> PORT
    h.perception.features["port_indicator"] = _match("port_indicator")
    h.step()
    assert h.machine.state is State.POST_EXIT_INTERFACE
    h.step()
    assert h.machine.state is State.PORT
    assert h.events.battles and h.events.battles[-1][0] == "finished"
    assert h.machine.battles_completed == 1


def test_result_missing_buttons_waits_60s_without_consuming_recovery(tmp_path: Path) -> None:
    """复现旧 20 秒循环：按钮缺席时 20/40/59 秒仍留在结算，不进入异常恢复。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features = {"battle_results": _match("battle_results")}
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    entered_at = h.clock.now()

    for elapsed in (20.1, 40.1, 59.0):
        h.clock.advance(elapsed - (h.clock.now() - entered_at))
        actions = h.step()
        assert h.machine.state is State.BATTLE_FINISHED
        assert all(action.label == "battle_result" for action in actions)
        assert len(h.machine._recovery_times) == 0  # noqa: SLF001
        assert not any(item[2] is State.ERROR_RECOVERY for item in h.machine.transitions)


def test_result_missing_buttons_pauses_once_at_60s_with_scores(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.perception.last_frame = SimpleNamespace(image=object())
    drive_to_battle_active(h)
    h.perception.features = {"battle_results": _match("battle_results")}
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED

    h.clock.advance(60.1)
    actions = h.step()

    assert actions == []
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "result_buttons_missing"
    assert h.controller.pause_request is not None
    assert "60" in h.controller.pause_request.detail
    assert h.screenshots.diagnostics.count("result_buttons_missing") == 1
    missing_logs = [
        detail
        for action_type, detail in h.events.actions
        if action_type == "result_step" and detail.get("step") == "buttons_missing"
    ]
    assert len(missing_logs) == 1
    assert "continue_score" in missing_logs[0]
    assert "return_score" in missing_logs[0]
    assert h.machine.battle_id is not None
    assert h.machine.battles_completed == 0


def test_result_continue_button_at_50s_still_continues(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features = {"battle_results": _match("battle_results")}
    h.step()
    h.clock.advance(50.0)
    assert all(action.label == "battle_result" for action in h.step())
    assert h.machine.state is State.BATTLE_FINISHED

    h.perception.features["result_continue_battle_button"] = _match(
        "result_continue_battle_button"
    )
    actions = h.step()

    assert h.machine.state is State.EXITING
    assert any(action.label == "result_continue" for action in actions)


@pytest.mark.parametrize(
    ("request_action", "expected"),
    [
        (lambda h: h.machine.request_pause("manual"), State.PAUSED),
        (lambda h: h.machine.request_stop("emergency_stop", "F12"), State.STOPPING),
    ],
)
def test_manual_pause_and_stop_take_priority_over_result_wait(
    tmp_path: Path, request_action, expected: State
) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features = {"battle_results": _match("battle_results")}
    h.step()
    h.clock.advance(30.0)
    battle_id = h.machine.battle_id

    request_action(h)

    assert h.machine.state is expected
    assert h.machine.battle_id == (battle_id if expected is State.PAUSED else None)
    assert h.machine.battles_completed == 0


def test_banner_or_missing_hp_bar_does_not_authorize_exit(tmp_path: Path) -> None:
    """ISS-002：死亡提示/血条名称消失不得单独授权提前退出。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = DeathSignals(
        hp_bar_absent=True,
        hp_zero=False,
        banner=True,
        marker_lost=True,
        spectator_ui=False,
        score=0.9,
        stable_ms=5000,
        timestamp=h.clock.now(),
        hp_fill_ratio=None,
        hp_evidence="unavailable",
        exit_authorized=False,
        evidence_at=0.0,
    )
    for _ in range(3):
        h.clock.advance(0.6)
        h.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert "death" not in h.events.battle_marks


def test_death_banner_unknown_evidence_keeps_waiting_for_fresh_zero(tmp_path: Path) -> None:
    """死亡提示+未知证据留在死亡等待，后续新鲜零血证据仍可授权退出。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    # 等待期内只有死亡提示、血条未知：不能当作存活重启巡航，也不能发退出键。
    h.clock.advance(2.5)
    h.perception.death = _death_signals(
        h.clock.now(), authorized=False, banner=True, hp_evidence="unknown"
    )
    submitted_before = len(h.executor.submitted)
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    assert "exit_requested" not in h.events.battle_marks
    assert len(h.executor.submitted) == submitted_before
    # 新帧继续积累到零血证据后可以退出。
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN


def test_dead_wait_alive_evidence_returns_to_battle(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    h.clock.advance(2.5)
    h.perception.death = _death_signals(
        h.clock.now(), authorized=False, banner=True, hp_evidence="alive"
    )
    h.step()
    assert h.machine.state is State.BATTLE_ACTIVE


@pytest.mark.parametrize("pause_resume", [False, True])
def test_confirmed_numeric_zero_never_restarts_battle_flows(
    tmp_path: Path, pause_resume: bool
) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features = {}
    h.perception.death = replace(_death_signals(h.clock.now()), hp_current=0)
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    nav_starts, behavior_starts = h.autopilot.started, h.behavior.started
    h.clock.advance(20.0)
    h.perception.death = _death_signals(h.clock.now(), authorized=False, hp_evidence="unknown")
    if pause_resume:
        h.machine.request_pause("manual")
        h.perception.detected = State.BATTLE_ACTIVE
        h.controller.resume()
        h.machine.request_resume()
    assert h.step() == []
    assert h.machine.state is State.DEAD_PENDING_EXIT
    assert (h.autopilot.started, h.behavior.started) == (nav_starts, behavior_starts)
    h.clock.advance(3.0)
    h.perception.death = replace(_death_signals(h.clock.now()), hp_current=0)
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN


def test_result_during_dead_wait_takes_priority(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    h.perception.features["battle_results"] = _match("battle_results")
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    assert "exit_requested" not in h.events.battle_marks


def test_exit_menu_result_clears_pending_escape(tmp_path: Path) -> None:
    """退出菜单待发送 ESC 时出现结算，必须直接分流且不发送缓存动作。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    assert h.machine._pending_exit_actions  # noqa: SLF001 - 验证缓存动作被结算清除

    h.perception.features = {"battle_results": _match("battle_results")}
    actions = h.step()

    assert h.machine.state is State.BATTLE_FINISHED
    assert actions == []
    assert h.machine._pending_exit_actions == []  # noqa: SLF001


def test_exit_confirming_result_takes_priority_over_menu_fallback(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.perception.features = {
        "exit_menu_panel": _match("exit_menu_panel"),
        "return_port": _match("return_port"),
    }
    h.step()
    assert h.machine.state is State.EXIT_CONFIRMING

    h.perception.features["battle_results"] = _match("battle_results")
    actions = h.step()

    assert h.machine.state is State.BATTLE_FINISHED
    assert not any(action.label.startswith("exit_return_port") for action in actions)


def test_exiting_result_takes_priority_over_exit_timeout(tmp_path: Path) -> None:
    """复现实机：退出等待超过 exit_s 时同一帧已是结算页，应转结算而非暂停。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.perception.features = {"exit_confirm": _match("exit_confirm")}
    h.step()
    h.step()
    assert h.machine.state is State.EXITING

    h.perception.features = {"battle_results": _match("battle_results")}
    h.clock.advance(181.0)
    actions = h.step()

    assert h.machine.state is State.BATTLE_FINISHED
    assert actions == []
    assert h.machine.battle_id is not None


@pytest.mark.parametrize(
    ("limit", "result_feature", "clicked_label"),
    [
        (10, "result_continue_battle_button", "result_continue"),
        (1, "result_return_port_button", "result_return_port"),
    ],
)
def test_result_screen_left_after_click_does_not_repeat_button(
    tmp_path: Path, limit: int, result_feature: str, clicked_label: str
) -> None:
    h = Harness(tmp_path)
    h.machine.set_rotation_limit(limit)
    drive_to_battle_active(h)
    h.perception.features = {
        "battle_results": _match("battle_results"),
        result_feature: _match(result_feature),
    }
    h.step()
    h.clock.advance(0.2)
    actions = h.step()
    assert h.machine.state is State.EXITING
    assert any(action.label == clicked_label for action in actions)

    repeated = h.step()

    assert h.machine.state is State.EXITING
    assert not any(action.label == clicked_label for action in repeated)


def test_battle_error_recovery_does_not_send_escape(tmp_path: Path) -> None:
    """ISS-002：战斗内未知画面不得自动发送 ESC（退出菜单键只用于已授权退出）。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.detected = None  # 战斗内无法识别当前界面
    h.machine._transition(State.ERROR_RECOVERY, "test_unknown")  # noqa: SLF001 - 测试直接构造场景
    for _ in range(3):
        h.clock.advance(0.5)
        h.step()
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "battle_unknown_interface"
    assert not any(action.label == "recovery_escape" for action in h.executor.submitted)


@pytest.mark.parametrize(
    ("with_context", "with_result", "expected"),
    [
        (True, True, State.BATTLE_FINISHED),
        (True, False, State.PAUSED),
        (False, True, State.PAUSED),
    ],
)
def test_error_recovery_result_requires_current_context_and_evidence(
    tmp_path: Path, with_context: bool, with_result: bool, expected: State
) -> None:
    h = Harness(tmp_path)
    if with_context:
        drive_to_battle_active(h)
    else:
        h.machine.start()
        h.perception.detected = State.PORT
        h.step()
        assert h.machine.battle_id is None
    h.perception.detected = State.BATTLE_FINISHED
    if with_result:
        h.perception.features = {"battle_results": _match("battle_results")}
    h.machine._transition(State.ERROR_RECOVERY, "test_result_recovery")  # noqa: SLF001

    h.step()

    assert h.machine.state is expected
    if expected is State.PAUSED:
        assert h.machine.pause_reason == "recovery_mid_battle"


def test_third_battle_death_result_continues_to_fourth(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.set_rotation_limit(10)
    drive_to_battle_active(h)
    _continue_from_active_battle(h)
    assert h.machine.battles_completed == 1
    _continue_from_active_battle(h)
    assert h.machine.battles_completed == 2
    third_battle_id = h.machine.battle_id

    h.perception.features = {}
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    h.perception.features = {
        "battle_results": _match("battle_results"),
        "result_continue_battle_button": _match("result_continue_battle_button"),
    }
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    h.clock.advance(0.2)
    actions = h.step()
    assert h.machine.state is State.EXITING
    assert any(action.label == "result_continue" for action in actions)
    h.perception.features = {"queue_indicator": _match("queue_indicator")}
    h.step()
    assert h.machine.battles_completed == 3
    assert h.machine.state is State.START_COOP
    h.step()
    assert h.machine.state is State.QUEUEING
    assert h.machine.battle_id is not None
    assert h.machine.battle_id != third_battle_id
    h.perception.features = {"battle_hud_indicator": _match("battle_hud_indicator")}
    h.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert h.machine.battles_completed == 3
    assert h.session.battle_starts == 4


def test_ten_battle_target_returns_port_and_stops(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.set_rotation_limit(10)
    drive_to_battle_active(h)
    for completed in range(1, 10):
        _continue_from_active_battle(h)
        assert h.machine.battles_completed == completed
    assert h.machine.state is State.BATTLE_ACTIVE

    h.perception.features = {
        "battle_results": _match("battle_results"),
        "result_continue_battle_button": _match("result_continue_battle_button"),
        "result_return_port_button": _match("result_return_port_button"),
    }
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    h.clock.advance(0.2)
    actions = h.step()
    assert h.machine.state is State.EXITING
    assert any(action.label == "result_return_port" for action in actions)
    assert not any(action.label == "result_continue" for action in actions)
    h.perception.features = {"port_indicator": _match("port_indicator")}
    h.step()
    assert h.machine.state is State.POST_EXIT_INTERFACE
    h.step()

    assert h.machine.state is State.STOPPING
    assert h.machine.battles_completed == 10
    assert h.machine.stop_request is not None
    assert str(h.machine.stop_request.reason) == "account_rotation"


def test_exit_menu_fallback_waits_for_interval_and_new_frame(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    t0 = h.clock.now()
    h.perception.features = {
        "exit_menu_panel": _match("exit_menu_panel"),
        "return_port": _match("return_port"),
    }

    first = h.machine.step(
        _frame_observation(t0, frame_id=10, frame_timestamp=100.0), t0
    )
    assert h.machine.state is State.EXIT_CONFIRMING
    assert any(action.label == "exit_return_port" for action in first)

    h.clock.advance(0.2)
    early_new_frame = h.machine.step(
        _frame_observation(t0 + 0.2, frame_id=11, frame_timestamp=100.2),
        t0 + 0.2,
    )
    assert early_new_frame == []
    assert h.machine.state is State.EXIT_CONFIRMING

    h.clock.advance(0.4)
    same_frame = h.machine.step(
        _frame_observation(t0 + 0.6, frame_id=11, frame_timestamp=100.2),
        t0 + 0.6,
    )
    assert same_frame == []
    assert h.machine.state is State.EXIT_CONFIRMING

    h.clock.advance(0.1)
    retry = h.machine.step(
        _frame_observation(t0 + 0.7, frame_id=12, frame_timestamp=100.7),
        t0 + 0.7,
    )
    assert any(action.label == "exit_return_port_fallback" for action in retry)

    h.clock.advance(0.6)
    same_retry_frame = h.machine.step(
        _frame_observation(t0 + 1.3, frame_id=12, frame_timestamp=100.7),
        t0 + 1.3,
    )
    assert same_retry_frame == []


def test_exit_menu_fallback_still_times_out_with_new_frames(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    t0 = h.clock.now()
    h.perception.features = {
        "exit_menu_panel": _match("exit_menu_panel"),
        "return_port": _match("return_port"),
    }
    h.machine.step(_frame_observation(t0, frame_id=30, frame_timestamp=300.0), t0)
    assert h.machine.state is State.EXIT_CONFIRMING

    for offset in range(1, 10):
        h.clock.advance(0.6)
        now = t0 + offset * 0.6
        frame_timestamp = 300.0 + offset * 0.6
        actions = h.machine.step(
            _frame_observation(
                now, frame_id=30 + offset, frame_timestamp=frame_timestamp
            ),
            now,
        )
        if now - t0 > 5.0:
            assert h.machine.state is State.EXITING
            assert not any(action.label == "exit_return_port_fallback" for action in actions)
            break
        assert h.machine.state is State.EXIT_CONFIRMING
        assert any(action.label == "exit_return_port_fallback" for action in actions)
    else:
        pytest.fail("持续菜单补点未在确认等待超时后进入 EXITING")


def test_exit_confirm_has_priority_over_menu_fallback(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    t0 = h.clock.now()
    h.perception.features = {
        "exit_menu_panel": _match("exit_menu_panel"),
        "return_port": _match("return_port"),
    }
    h.machine.step(_frame_observation(t0, frame_id=20, frame_timestamp=200.0), t0)
    assert h.machine.state is State.EXIT_CONFIRMING

    h.perception.features["exit_confirm"] = _match("exit_confirm")
    h.clock.advance(0.6)
    actions = h.machine.step(
        _frame_observation(t0 + 0.6, frame_id=21, frame_timestamp=200.6),
        t0 + 0.6,
    )

    assert h.machine.state is State.EXITING
    assert any(action.label == "exit_confirm" for action in actions)
    assert not any(action.label == "exit_return_port_fallback" for action in actions)


def test_pause_and_resume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.machine.request_pause("window_unfocused")
    assert h.machine.state is State.PAUSED
    assert h.executor.released >= 1
    assert h.executor.gate_open is False
    # ISS-006：状态机暂停同步到共享 StopController，F11 恢复后主循环能看到同一状态
    assert h.controller.paused
    assert h.controller.resume()
    h.machine.request_resume()
    h.clock.advance(0.1)
    h.step()
    assert h.machine.state is State.INIT
    assert h.executor.gate_open is True
    assert not h.controller.paused


def test_battle_pause_resume_keeps_battle_context(tmp_path: Path) -> None:
    """ISS-006：战斗内暂停恢复保留场次编号/战斗上下文，不当作全新进程接管。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    battle_id = h.machine.battle_id
    assert battle_id is not None
    autopilot_starts = h.autopilot.started
    h.machine.request_pause("heartbeat_timeout")
    assert h.machine.state is State.PAUSED
    assert h.executor.gate_open is False
    # 画面仍是战斗 HUD：恢复后回到 BATTLE_ACTIVE 并重新启动自动驾驶/行为
    h.perception.detected = State.BATTLE_ACTIVE
    h.controller.resume()
    h.machine.request_resume()
    h.step()
    assert h.machine.state is State.BATTLE_ACTIVE
    assert h.machine.battle_id == battle_id
    assert h.autopilot.started > autopilot_starts
    assert h.executor.gate_open is True
    # 恢复时不重复创建随机档案
    saved = len(h.events.saved_profiles)
    h.step()
    assert len(h.events.saved_profiles) == saved


def test_resume_refuses_new_process_mid_battle(tmp_path: Path) -> None:
    """新进程在战斗中：F11 不接管已有战斗，保持暂停并给出原因。"""
    h = Harness(tmp_path)
    h.machine.start()
    h.perception.detected = State.BATTLE_ACTIVE
    h.step()
    assert h.machine.state is State.PAUSED
    h.perception.detected = State.BATTLE_ACTIVE
    h.machine.request_resume()
    h.step()
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "restart_mid_battle"
    assert h.executor.gate_open is False
    # 恢复失败后控制器重新写回暂停，避免主循环看到“无人暂停”却无法自动恢复
    assert h.controller.paused


def test_resume_to_port_aborts_unfinished_battle(tmp_path: Path) -> None:
    """ISS-006：战斗上下文被暂停后回港，必须显式写中断记录，不能静默清空场次。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    battle_id = h.machine.battle_id
    assert battle_id is not None
    h.machine.request_pause("window_lost")
    h.perception.detected = State.PORT
    h.controller.resume()
    h.machine.request_resume()
    h.step()
    assert h.machine.state is State.PORT
    assert h.machine.battle_id is None
    assert h.events.battles and h.events.battles[-1][0] == "aborted"
    assert h.battle_contexts[-1] is None


def test_resume_to_port_abort_clears_session_timer_even_if_db_write_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    assert h.session.battle_in_progress

    def fail_end_battle(*args, **kwargs) -> None:
        raise RuntimeError("db down")

    h.events.end_battle = fail_end_battle  # type: ignore[method-assign]
    h.machine.request_pause("manual")
    h.perception.detected = State.PORT
    h.controller.resume()
    h.machine.request_resume()
    h.step()

    assert h.machine.state is State.PORT
    assert not h.session.battle_in_progress
    assert len(h.session.battle_aborts) == 1
    assert h.session.battles_completed == 0


def test_real_session_port_interrupt_allows_next_battle_without_extra_completion(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    real_session = SessionController(SessionConfig(), SafetyConfig(), h.clock)
    h.session = real_session  # type: ignore[assignment]
    h.machine.deps.session = real_session
    drive_to_battle_active(h)
    old_battle_id = h.machine.battle_id
    assert old_battle_id is not None
    assert real_session.battle_in_progress

    h.clock.advance(12.0)
    h.machine.request_pause("manual")
    h.perception.detected = State.PORT
    h.perception.features = {"port_indicator": _match("port_indicator")}
    h.controller.resume()
    h.machine.request_resume()
    h.step()

    assert h.machine.state is State.PORT
    assert h.machine.battle_id is None
    assert not real_session.battle_in_progress
    assert real_session.battles_completed == 0

    h.step()  # PORT idle -> verify
    h.step()  # verify -> start
    h.step()  # start -> starting
    h.step()  # start sequence done -> START_COOP
    assert h.machine.state is State.START_COOP
    h.perception.features = {"queue_indicator": _match("queue_indicator")}
    h.step()

    assert h.machine.state is State.QUEUEING
    assert h.machine.battle_id is not None
    assert h.machine.battle_id != old_battle_id
    assert real_session.battle_in_progress
    assert real_session.battles_completed == 0
    assert h.events.battles and h.events.battles[-1][0] == "aborted"


def test_stop_while_in_battle_aborts_record(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    battle_id = h.machine.battle_id
    assert battle_id is not None
    h.machine.request_stop("emergency_stop", "F12")
    assert h.machine.state is State.STOPPING
    assert h.machine.battle_id is None
    assert h.events.battles and h.events.battles[-1][0] == "aborted"
    assert h.battle_contexts[-1] is None


def test_resume_unknown_interface_keeps_paused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.machine.request_pause("heartbeat_timeout")
    h.perception.detected = None
    h.controller.resume()
    h.machine.request_resume()
    h.step()
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "resume_unknown"
    assert h.controller.paused


def test_battle_context_callback_tracks_battle(tmp_path: Path) -> None:
    """ISS-010：创建战斗记录/结束/中断都更新回调场次编号。"""
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    battle_id = h.machine.battle_id
    assert battle_id is not None
    assert h.battle_contexts[-1] == battle_id
    h.machine.request_stop("manual", "test")
    assert h.battle_contexts[-1] is None


def test_emergency_stop_finishes_session(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.machine.request_stop("emergency_stop", "F12")
    assert h.machine.state is State.STOPPING
    assert h.machine.finished
    assert h.executor.stopped
    assert h.events.ended_sessions and h.events.ended_sessions[-1][0] == "emergency_stop"


def test_rotation_limit_triggers_account_rotation(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.set_rotation_limit(2)
    h.machine.start()
    h.perception.detected = State.PORT
    h.step()
    assert h.machine.state is State.PORT
    # 模拟已完成 2 场
    h.machine._battles_completed = 2  # noqa: SLF001 - 测试直接构造场景
    h.step()
    assert h.machine.state is State.STOPPING
    assert h.machine.stop_request is not None
    assert str(h.machine.stop_request.reason) == "account_rotation"


def test_navigator_failure_is_isolated(tmp_path: Path) -> None:
    h = Harness(tmp_path, behavior=FakeBehavior(fail=True))
    drive_to_battle_active(h)
    # 行为子流程抛异常不应打断状态机
    h.clock.advance(0.5)
    h.step()
    assert h.machine.state is State.BATTLE_ACTIVE


def test_window_missing_pauses(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.perception.detected = State.PORT
    h.step()
    h.machine.update_window_info(None)
    obs = Observation(timestamp=h.clock.now(), window_rect=(0, 0, 0, 0), window_missing=True)
    h.machine.step(obs, h.clock.now())
    assert h.machine.state is State.PAUSED


# ---------------------------------------------------------------------------
# 账号轮换计划
# ---------------------------------------------------------------------------


def test_account_rotator_round_robin() -> None:
    from src.config.models import AccountConfig, AccountsConfig

    accounts = AccountsConfig(
        accounts=(
            AccountConfig("alt1", max_battles_per_session=2),
            AccountConfig("alt2", max_battles_per_session=3),
            AccountConfig("alt3", enabled=False),
        ),
        rotation_enabled=True,
        max_battles_per_account=5,
    )
    rotator = AccountRotator(accounts)
    assert rotator.enabled
    assert rotator.aliases == ("alt1", "alt2")
    assert rotator.current is not None and rotator.current.alias == "alt1"
    assert rotator.current_limit() == 2
    assert not rotator.needs_rotation()
    rotator.record_battle()
    assert not rotator.needs_rotation()
    rotator.record_battle()
    assert rotator.needs_rotation()
    assert rotator.next_alias() == "alt2"
    rotator.advance()
    assert rotator.current is not None and rotator.current.alias == "alt2"
    assert rotator.current_limit() == 3
    rotator.advance()
    assert rotator.current is not None and rotator.current.alias == "alt1"


def test_account_rotator_disabled() -> None:
    from src.config.models import AccountConfig, AccountsConfig

    rotator = AccountRotator(AccountsConfig(accounts=(AccountConfig("alt1"),), rotation_enabled=False))
    assert not rotator.enabled
    assert rotator.current_limit() is None
    assert not rotator.needs_rotation()
    assert rotator.next_alias() is None


def test_first_port_visit_skips_between_battle_wait(tmp_path: Path) -> None:
    """本会话还没打完任何一场时（含暂停恢复）进港口不等待；两场之间才随机冷却。"""
    h = Harness(tmp_path)
    h.session.sample_between_battle_s = lambda: 30.0  # type: ignore[method-assign]
    h.machine.start()
    h.perception.detected = State.PORT
    h.step()
    assert h.machine.state is State.PORT
    assert h.machine._port_wait_until == pytest.approx(h.clock.now())
    h.clock.advance(1.0)
    h.machine._enter_port(h.clock.now(), "test_resume_visit")
    assert h.machine._port_wait_until <= h.clock.now()  # 暂停恢复不等待
    h.machine._battles_completed = 1
    h.clock.advance(1.0)
    h.machine._enter_port(h.clock.now(), "test_second_visit")
    assert h.machine._port_wait_until == pytest.approx(h.clock.now() + 10.0)


def test_paused_window_unavailable_auto_resumes_when_window_returns(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    broken = WindowInfo(
        hwnd=1, title="game", process_name="game.exe", client_rect=Rect(0, 0, 2560, 1600),
        foreground=True, minimized=False, valid=False,
    )
    h.machine.update_window_info(broken)
    h.step()
    assert h.machine.state is State.PAUSED
    restored = WindowInfo(
        hwnd=1, title="game", process_name="game.exe", client_rect=Rect(0, 0, 2560, 1600),
        foreground=True, minimized=False, valid=True,
    )
    h.machine.update_window_info(restored)
    h.step()
    assert h.machine.state is State.INIT


def test_delayed_continue_button_wins_over_early_return_button(tmp_path):
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features = {"battle_results": _match("battle_results"),
                             "result_return_port_button": _match("result_return_port_button")}
    h.step()
    h.clock.advance(1)
    actions = h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    assert not any(action.label == "result_return_port" for action in actions)
    h.clock.advance(2)
    h.perception.features["result_continue_battle_button"] = _match("result_continue_battle_button")
    actions = h.step()
    assert any(action.label == "result_continue" for action in actions)


@pytest.mark.parametrize("feature,state", [
    ("queue_indicator", State.QUEUEING), ("loading_indicator", State.LOADING),
    ("battle_hud_indicator", State.BATTLE_ACTIVE),
])
def test_continue_starts_next_battle_when_queue_or_loading_is_skipped(tmp_path, feature, state):
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    old_id = h.machine.battle_id
    h.perception.features = {"battle_results": _match("battle_results"),
                             "result_continue_battle_button": _match("result_continue_battle_button")}
    h.step()
    h.clock.advance(1)
    h.step()
    assert h.machine.state is State.EXITING
    h.perception.features = {feature: _match(feature)}
    h.step()
    assert h.machine.state is State.START_COOP
    h.step()
    assert h.machine.state is state
    assert h.machine.battle_id != old_id
    assert h.machine.battles_completed == 1
    assert h.session.battle_starts == 2
    assert h.events.battles == [("finished", "none", None)]


def test_death_returns_port_then_joins_next_battle_with_one_short_wait(tmp_path):
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    old_id = h.machine.battle_id
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.clock.advance(2.5)
    h.perception.death = _death_signals(h.clock.now())
    h.step()
    h.perception.features = {"exit_confirm": _match("exit_confirm")}
    h.step()
    h.step()
    assert h.machine.state is State.EXITING
    h.perception.death = None
    h.perception.features = {"port_indicator": _match("port_indicator")}
    calls = []
    h.session.sample_between_battle_s = lambda: calls.append(1) or 7
    h.step()
    h.step()
    assert h.machine.state is State.PORT
    deadline = h.machine._port_wait_until
    assert deadline == h.clock.now() + 7
    assert h.events.battles[-1][0] == "dead"
    h.clock.advance(2)
    h.machine.request_pause("manual")
    h.controller.resume()
    h.perception.detected = State.PORT
    h.machine.request_resume()
    h.step()
    assert h.machine._port_wait_until == deadline
    assert len(calls) == 1
    h.clock.advance(5)
    for _ in range(5):
        h.step()
    assert h.machine.state is State.START_COOP
    assert h.session.successes == 2
    h.perception.features = {"queue_indicator": _match("queue_indicator")}
    h.step()
    assert h.machine.state is State.QUEUEING
    assert h.machine.battle_id != old_id
