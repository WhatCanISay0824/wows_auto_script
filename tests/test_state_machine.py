"""状态机主链路离线测试（用假依赖驱动，不需要游戏/不需要真实键鼠）。"""

from __future__ import annotations

from pathlib import Path

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
    ShipProfile,
    SpawnPoint,
    StepDef,
    WindowConfig,
)
from src.state_machine.accounts import AccountRotator
from src.state_machine.machine import MachineDeps, StateMachine
from src.state_machine.ship_check import ShipCheckStatus
from src.state_machine.steps import StepStatus

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

    def submit(self, actions) -> None:
        self.submitted.extend(actions)

    def release_all(self) -> None:
        self.released += 1

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

    def end_battle(self, result="") -> float:
        self.battle_ends += 1
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
        battle_hud=BattleHudConfig(result_feature="battle_results"),
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


def test_dead_exit_flow_to_port(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    # 死亡
    h.perception.death = DeathSignals(True, False, True, False, False, score=0.85, stable_ms=1200, timestamp=h.clock.now())
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    assert "death" in h.events.battle_marks
    # 等待 0.8~2.0s 后进入退出菜单
    h.clock.advance(2.5)
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
    h.perception.death = DeathSignals(True, False, True, False, False, score=0.9, stable_ms=1500, timestamp=h.clock.now())
    h.perception.features["battle_results"] = _match("battle_results")
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    assert h.machine.battle is not None


def test_pause_and_resume(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine.start()
    h.machine.request_pause("window_unfocused")
    assert h.machine.state is State.PAUSED
    assert h.executor.released >= 1
    h.machine.request_resume()
    h.clock.advance(0.1)
    h.step()
    assert h.machine.state is State.INIT


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
