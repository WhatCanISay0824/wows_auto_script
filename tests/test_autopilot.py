"""autopilot 简化导航单元测试（ISS-001：M 全屏地图点选 + 旧小地图降级路径）。

覆盖：相位动作顺序、坐标换算与边界内缩、验证成功/失败与重试/降级、blocks_behavior、
stop/reset 不补发旧点击、异常路径先关图、无 minimap/full_map_rect 时不盲点、
map 按键从 keys.yaml 懒加载。
"""

from __future__ import annotations

import random
from dataclasses import replace
from pathlib import Path

import pytest
from src.common.types import (
    ActionKind,
    MatchResult,
    NormalizedRect,
    Observation,
    Rect,
    ShipPosition,
)
from src.config.models import (
    ClientProfile,
    KeyMap,
    MinimapConfig,
    NavigationConfig,
    WindowConfig,
)
from src.navigation.autopilot import AutoPilotNavigator

WINDOW = Rect(100, 50, 2000, 1200)  # 客户区屏幕矩形
MINIMAP_NORM = NormalizedRect(0.50, 0.50, 0.90, 0.90)  # 便于手算的小地图内容区
FULL_MAP_NORM = NormalizedRect(0.20, 0.10, 0.80, 0.90)  # 便于手算的全屏地图内容区

# 手算基准：全屏地图内容区像素矩形 = (400, 120, 1200, 960)；小地图内容区 = (1000, 600, 800, 480)
FULL_MAP_PX = (400, 120, 1200, 960)
MINIMAP_PX = (1000, 600, 800, 480)


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------
def make_profile(*, minimap: MinimapConfig | None = None, profile_dir: Path | None = None) -> ClientProfile:
    return ClientProfile(
        client_id="test",
        display_name="test",
        window=WindowConfig(process_names=("game.exe",)),
        language="zh_cn",
        reference_resolution=(2000, 1200),
        features={},
        minimap=MinimapConfig(rect=MINIMAP_NORM, border_px=0) if minimap is None else minimap,
        profile_dir=profile_dir,
    )


def make_config(
    *,
    surface: str = "full_map",
    target: str = "symmetric",
    reclick: tuple[float, float] = (60.0, 120.0),
    offset: tuple[float, float] = (0.0, 0.0),
    full_map_rect: NormalizedRect | None = FULL_MAP_NORM,
    open_wait: float = 1.0,
    close_wait: float = 0.4,
    verify_s: float = 8.0,
    retry: int = 2,
    min_move: float = 2.0,
    min_speed: float = 0.3,
    verify_feature: str | None = "autopilot_indicator",
) -> NavigationConfig:
    return NavigationConfig(
        mode="autopilot",
        surface=surface,
        autopilot_target=target,
        autopilot_reclick_s=reclick,
        autopilot_offset=offset,
        full_map_rect=full_map_rect,
        full_map_open_wait_s=open_wait,
        full_map_close_wait_s=close_wait,
        full_map_verify_s=verify_s,
        full_map_retry=retry,
        autopilot_verify_min_move_px=min_move,
        autopilot_verify_min_speed=min_speed,
        autopilot_verify_feature=verify_feature,
    )


def make_keymap() -> KeyMap:
    return KeyMap(actions={"escape": "escape", "fire": "mouse_left", "full_map": "m"})


def make_observation(
    ship_norm: tuple[float, float] | None = None,
    *,
    speed: float = 0.0,
    dx: float = 0.0,
    dy: float = 0.0,
    window: Rect = WINDOW,
    window_missing: bool = False,
    features: dict[str, MatchResult] | None = None,
    timestamp: float = 0.0,
) -> Observation:
    content = MINIMAP_NORM.to_pixels(window.w, window.h)
    ship = None
    if ship_norm is not None:
        ship = ShipPosition(
            minimap_px=(ship_norm[0] * content.w + dx, ship_norm[1] * content.h + dy),
            world_xy=None,
            heading_deg=0.0,
            speed_mps=speed,
            confidence=0.9,
            source="marker",
        )
    return Observation(
        timestamp=timestamp,
        frame_timestamp=timestamp,
        window_rect=window.to_tuple(),
        ship=ship,
        window_missing=window_missing,
        features=dict(features or {}),
    )


class _Events:
    """收集导航事件，便于断言事件序列。"""

    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []

    def __call__(self, event: str, detail: dict[str, object]) -> None:
        self.items.append(dict(detail))

    def names(self) -> list[str]:
        return [str(item["event"]) for item in self.items]

    def last(self, name: str) -> dict[str, object]:
        for item in reversed(self.items):
            if item["event"] == name:
                return item
        raise AssertionError(f"未找到事件 {name}，实际事件 {self.names()}")


def make_nav(
    config: NavigationConfig,
    *,
    profile: ClientProfile | None = None,
    keymap: KeyMap | None = None,
    events: _Events | None = None,
    seed: int = 1,
) -> AutoPilotNavigator:
    return AutoPilotNavigator(
        config,
        profile or make_profile(),
        rng=random.Random(seed),
        on_event=events,
        keymap=keymap,
    )


def key_actions(actions: list) -> list[tuple[str, str | None]]:
    return [(action.kind.value, action.key) for action in actions]


def test_target_override_survives_resume_and_reset_restores_default():
    nav = make_nav(make_config(), keymap=make_keymap())
    nav.start(0)
    nav.replace_target((0.3, 0.7), 1)
    observation = make_observation((0.5, 0.8))
    assert nav.compute_full_map_target(observation).normalized == (0.3, 0.7)
    nav.stop()
    nav.start(2)
    assert nav.compute_full_map_target(observation).normalized == (0.3, 0.7)
    nav.reset()
    nav.start(3)
    nav.tick(observation, 3)
    assert nav.compute_full_map_target(observation).normalized == pytest.approx((0.5, 0.2))


def test_cached_frame_after_map_close_cannot_confirm_movement():
    nav = make_nav(make_config(), keymap=make_keymap())
    nav.start(0)
    obs = make_observation((0.5, 0.8))
    for moment in (0, 0.1, 1.2, 1.3, 1.8):
        nav.tick(obs, moment)
    cached = make_observation((0.5, 0.8), dx=8, speed=6, timestamp=1.0)
    nav.tick(replace(cached, timestamp=2.2), 2.2)
    assert not nav.move_confirmed
    nav.tick(replace(cached, timestamp=2.4, frame_timestamp=2.4), 2.4)
    assert nav.move_confirmed


@pytest.mark.parametrize("reset", [False, True])
def test_resume_without_marker_does_not_reuse_old_movement_reference(reset):
    nav = make_nav(make_config(target="center"), keymap=make_keymap())
    nav.start(0)
    nav.tick(make_observation((0.5, 0.8)), 0)
    nav.stop()
    if reset:
        nav.reset()
    nav.start(100)
    for moment in (100, 100.1, 101.2, 101.3, 101.8):
        nav.tick(make_observation(None, timestamp=moment), moment)
    nav.tick(make_observation((0.5, 0.8), dx=50, timestamp=102), 102)
    assert nav.phase == "verify"
    assert not nav.move_confirmed


# ---------------------------------------------------------------------------
# full_map 主路径
# ---------------------------------------------------------------------------
def test_full_map_sequence_opens_clicks_closes_and_confirms() -> None:
    events = _Events()
    nav = make_nav(make_config(), keymap=make_keymap(), events=events)
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))  # 我方出生点下方，对称目标 (0.5, 0.2)
    all_actions: list = []

    # 第一帧：记录出生点并进入开图相位（不动作）
    assert nav.tick(obs, 0.0) == []
    assert nav.phase == "open_map"
    assert nav.blocks_behavior is True

    # M 开图：同一 tick 返回 key_down/key_up 两个动作
    actions = nav.tick(obs, 0.1)
    all_actions.extend(actions)
    assert key_actions(actions) == [("key_down", "m"), ("key_up", "m")]
    assert nav.phase == "wait_map" and nav.map_open is True

    # 等待地图就绪（full_map_open_wait_s=1.0）
    assert nav.tick(obs, 1.0) == []
    assert nav.tick(obs, 1.2) == []
    assert nav.phase == "click"

    # 全屏地图点选：目标 (0.5, 0.2) -> client (1000, 312) -> screen (1100, 362)
    actions = nav.tick(obs, 1.3)
    all_actions.extend(actions)
    assert len(actions) == 1
    assert actions[0].kind is ActionKind.CLICK_ABS
    assert (actions[0].x, actions[0].y) == (1100, 362)
    assert nav.phase == "wait_close" and nav.blocks_behavior is True
    assert nav.clicks == 1

    # 点击生效后按 M 关图
    assert nav.tick(obs, 1.6) == []
    actions = nav.tick(obs, 1.8)
    all_actions.extend(actions)
    assert key_actions(actions) == [("key_down", "m"), ("key_up", "m")]
    assert nav.phase == "verify" and nav.blocks_behavior is True
    assert nav.blocks_minimap is True

    # 关图后的新战斗帧可显示地图并执行行为，移动验证继续独立进行。
    nav.tick(make_observation((0.5, 0.8), timestamp=1.9), 1.9)
    assert nav.phase == "verify"
    assert not nav.blocks_behavior and not nav.blocks_minimap

    # 船速超过阈值 -> 确认成功，解除行为阻塞
    assert nav.tick(make_observation((0.5, 0.8), speed=6.0, timestamp=2.0), 2.0) == []
    assert nav.phase == "reclick"
    assert nav.blocks_behavior is False
    target = nav.last_target
    assert target is not None
    assert target.surface == "full_map"
    assert target.from_own_marker is True
    assert target.normalized == pytest.approx((0.5, 0.2))
    assert target.screen_px == (1100, 362)

    names = events.names()
    assert names[0] == "autopilot_start"
    for expected in (
        "autopilot_spawn",
        "autopilot_map_open",
        "autopilot_click",
        "autopilot_map_close",
        "autopilot_confirmed",
    ):
        assert expected in names, names

    # 输入隔离：全程只发 M 键和左键点击，绝不发送 WASD/ESC
    assert {action.key for action in all_actions if action.key is not None} == {"m"}
    assert {action.key for action in all_actions if action.key is not None}.isdisjoint(
        {"w", "a", "s", "d", "escape"}
    )


def test_full_map_target_uses_edge_margin() -> None:
    nav = make_nav(
        make_config(target="center", offset=(0.9, 0.9)),
        keymap=make_keymap(),
    )
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    assert nav.tick(obs, 0.0) == []
    assert key_actions(nav.tick(obs, 0.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.tick(obs, 1.2) == []
    actions = nav.tick(obs, 1.3)
    assert len(actions) == 1
    target = nav.last_target
    assert target is not None
    # 目标归一化被夹到全屏地图安全内缩：x∈[0.04, 0.96]、y∈[0.02, 0.98]
    assert target.normalized == (0.96, 0.98)
    x1, y1, w, h = FULL_MAP_PX
    assert target.content_px == (x1 + round(0.96 * w), y1 + round(0.98 * h))
    assert target.screen_px == (WINDOW.x + target.content_px[0], WINDOW.y + target.content_px[1])


def test_full_map_confirms_by_marker_displacement() -> None:
    nav = make_nav(make_config(), keymap=make_keymap())
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    assert nav.tick(obs, 0.0) == []
    assert nav.tick(obs, 0.1) != []
    assert nav.tick(obs, 1.2) == []
    assert nav.tick(obs, 1.3) != []
    assert nav.tick(obs, 1.8) != []
    assert nav.phase == "verify"
    # 船速仍为 0，但自身标记位移 6px >= 3px -> 确认成功
    moved = make_observation((0.5, 0.8), dx=6.0, timestamp=2.5)
    assert nav.tick(moved, 2.5) == []
    assert nav.phase == "reclick"
    assert nav.blocks_behavior is False


# 异常关图后也要在新帧到位时解除小地图隔离。
def test_aborted_map_close_unblocks_minimap_on_fresh_frame():
    nav = make_nav(make_config(), keymap=make_keymap())
    nav.start(0)
    obs = make_observation((0.5, 0.8))
    nav.tick(obs, 0)
    nav.tick(obs, 0.1)
    nav.tick(obs, 1.2)
    nav.tick(make_observation((0.5, 0.8), window=Rect(0, 0, 0, 0)), 1.3)
    assert nav.blocks_minimap
    nav.tick(make_observation((0.5, 0.8), timestamp=1.4), 1.4)
    assert not nav.blocks_minimap


def test_indicator_without_movement_does_not_confirm_cruise() -> None:
    """静止 HUD 提示不能证明舰船已经开始巡航。"""
    events = _Events()
    nav = make_nav(make_config(), keymap=make_keymap(), events=events)
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    assert nav.tick(obs, 0.0) == []
    assert nav.tick(obs, 0.1) != []  # 开图
    assert nav.tick(obs, 1.2) == []  # 等地图就绪
    assert nav.tick(obs, 1.3) != []  # 点选
    assert nav.tick(obs, 1.8) != []  # 关图，进入 verify
    assert nav.phase == "verify"
    indicator = {
        "autopilot_indicator": MatchResult(
            feature_id="autopilot_indicator",
            found=True,
            score=0.99,
            center=(400, 1270),
        )
    }
    confirm_obs = make_observation((0.5, 0.8), features=indicator, timestamp=2.2)
    assert nav.tick(confirm_obs, 2.2) == []
    assert nav.phase == "verify"
    assert nav.activation_detected
    assert not nav.move_confirmed
    assert "autopilot_confirmed" not in events.names()


def test_full_map_verify_feature_can_be_disabled() -> None:
    nav = make_nav(make_config(verify_feature=None), keymap=make_keymap())
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    nav.tick(obs, 0.0)
    nav.tick(obs, 0.1)
    nav.tick(obs, 1.2)
    nav.tick(obs, 1.3)
    nav.tick(obs, 1.8)
    assert nav.phase == "verify"
    indicator = {
        "autopilot_indicator": MatchResult(feature_id="autopilot_indicator", found=True, score=0.99)
    }
    # 关闭特征验证后，仅凭 HUD 特征不能确认；船速/位移仍为 0
    nav.tick(make_observation((0.5, 0.8), features=indicator), 2.2)
    assert nav.phase == "verify"


def test_periodic_reclick_opens_map_again_after_confirmed() -> None:
    nav = make_nav(make_config(reclick=(30.0, 30.0)), keymap=make_keymap())
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    nav.tick(obs, 0.0)
    nav.tick(obs, 0.1)
    nav.tick(obs, 1.2)
    nav.tick(obs, 1.3)
    nav.tick(obs, 1.8)
    nav.tick(make_observation((0.5, 0.8), speed=5.0, timestamp=2.0), 2.0)
    assert nav.phase == "reclick"
    assert abs(nav.next_click_at - 32.0) < 1e-6
    assert nav.tick(obs, 30.0) == []  # 未到重发时间
    assert nav.tick(obs, 32.0) == []  # 到点开始新一轮，进入开图相位
    assert nav.phase == "open_map"
    assert key_actions(nav.tick(obs, 32.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.clicks == 1  # 周期重发只增加点击计数


# ---------------------------------------------------------------------------
# 等待自身标记 / 回退中心
# ---------------------------------------------------------------------------
def test_full_map_waits_marker_then_falls_back_to_center() -> None:
    nav = make_nav(make_config(), keymap=make_keymap())
    nav.start(0.0)
    no_ship = make_observation(None)
    assert nav.tick(no_ship, 0.0) == []
    assert nav.phase == "wait_marker"
    assert nav.tick(no_ship, 9.9) == []
    assert nav.tick(no_ship, 10.0) == []  # 10s 到点：允许回退中心并进入开图相位
    assert nav.phase == "open_map"
    assert key_actions(nav.tick(no_ship, 10.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.tick(no_ship, 11.2) == []
    actions = nav.tick(no_ship, 11.3)
    assert len(actions) == 1
    target = nav.last_target
    assert target is not None
    assert target.normalized == (0.5, 0.5)
    assert target.from_own_marker is False


# ---------------------------------------------------------------------------
# 失败重试与降级
# ---------------------------------------------------------------------------
def test_full_map_verify_failure_keeps_full_map_and_limits_retries() -> None:
    events = _Events()
    nav = make_nav(make_config(retry=1), keymap=make_keymap(), events=events)
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))

    def run_failing_cycle(t0: float) -> None:
        """从周期开始推进到关图并进入验证相位（t0 为周期开始 tick）。"""
        assert nav.tick(obs, t0) == []  # begin cycle -> open_map
        assert nav.phase == "open_map"
        assert key_actions(nav.tick(obs, t0 + 0.1)) == [("key_down", "m"), ("key_up", "m")]
        assert nav.tick(obs, t0 + 1.1) == []  # wait_map 到点 -> click 相位
        assert nav.phase == "click"
        assert nav.tick(obs, t0 + 1.2) != []  # 点击
        assert nav.tick(obs, t0 + 1.6) != []  # 关图（verify deadline = t0+9.6）
        assert nav.phase == "verify"
        for index in range(15):
            moment = t0 + 2.0 + index * 0.5
            sample = replace(obs, timestamp=moment, frame_timestamp=moment)
            assert nav.tick(sample, moment) == []
        assert nav.tick(replace(obs, frame_timestamp=t0 + 9.4), t0 + 9.4) == []
        assert nav.phase == "verify"
        assert nav.tick(replace(obs, frame_timestamp=t0 + 9.7), t0 + 9.7) == []
        assert nav.blocks_behavior is False

    run_failing_cycle(0.0)
    assert nav.phase == "reclick"
    assert nav.surface == "full_map"
    assert events.last("autopilot_verify_failed")["attempt"] == 1

    run_failing_cycle(9.7 + 1.0)  # 重试：下一次周期从 next_click_at=10.7 开始
    assert nav.surface == "full_map"
    assert nav.degraded is False
    assert nav.phase == "reclick"
    assert events.names().count("autopilot_verify_failed") == 2
    assert "autopilot_degrade" not in events.names()

    retry_at = nav.next_click_at
    assert retry_at >= 30.4
    assert nav.tick(obs, retry_at - 0.1) == []
    assert nav.phase == "reclick"
    assert nav.tick(obs, retry_at) == []
    assert nav.phase == "open_map"
    actions = nav.tick(obs, retry_at + 0.1)
    assert key_actions(actions) == [("key_down", "m"), ("key_up", "m")]


def test_full_map_success_resets_retry_counter() -> None:
    nav = make_nav(make_config(retry=2), keymap=make_keymap())
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    # 第一个周期验证失败一次 -> 计数 1
    nav.tick(obs, 0.0)
    nav.tick(obs, 0.1)
    nav.tick(obs, 1.2)
    nav.tick(obs, 1.3)
    nav.tick(obs, 1.8)
    for index in range(16):
        moment = 2.0 + index * 0.5
        nav.tick(replace(obs, timestamp=moment, frame_timestamp=moment), moment)
    nav.tick(replace(obs, timestamp=9.9, frame_timestamp=9.9), 9.9)
    assert nav.phase == "reclick"
    # 第二个周期验证成功 -> 计数清零，不降级
    nav.tick(obs, 11.0)
    nav.tick(obs, 11.1)
    nav.tick(obs, 12.2)
    nav.tick(obs, 12.3)
    nav.tick(obs, 12.8)
    nav.tick(make_observation((0.5, 0.8), speed=5.0, timestamp=13.0), 13.0)
    assert nav.phase == "reclick"
    assert nav.surface == "full_map"
    assert nav.degraded is False


# ---------------------------------------------------------------------------
# 异常路径：先关图、不盲点
# ---------------------------------------------------------------------------
def test_target_unavailable_while_map_open_closes_map_then_degrades() -> None:
    events = _Events()
    nav = make_nav(make_config(), keymap=make_keymap(), events=events)
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    nav.tick(obs, 0.0)
    assert key_actions(nav.tick(obs, 0.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.tick(obs, 1.2) == []
    assert nav.phase == "click"

    # 窗口客户区无效：全屏地图目标无法换算。地图已经打开，必须先尽力按 M 关图。
    bad_window = make_observation((0.5, 0.8), window=Rect(0, 0, 0, 0))
    actions = nav.tick(bad_window, 1.3)
    assert key_actions(actions) == [("key_down", "m"), ("key_up", "m")]
    assert nav.surface == "minimap"
    assert nav.phase == "reclick"
    assert nav.blocks_behavior is False
    assert "autopilot_click_skipped" in events.names()
    assert events.last("autopilot_degrade")["reason"] == "target_unavailable"
    assert nav.clicks == 0


def test_missing_full_map_rect_degrades_without_blind_click() -> None:
    events = _Events()
    nav = make_nav(make_config(full_map_rect=None), keymap=make_keymap(), events=events)
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    assert nav.tick(obs, 0.0) == []
    assert nav.phase == "open_map"
    # 缺 full_map_rect：不开图、不点击，直接降级
    assert nav.tick(obs, 0.1) == []
    assert nav.surface == "minimap"
    assert nav.degraded is True
    assert nav.clicks == 0
    assert events.last("autopilot_degrade")["reason"] == "missing_full_map_rect"
    # 降级后的周期只点小地图
    assert nav.tick(obs, 1.1) == []
    actions = nav.tick(obs, 1.2)
    assert len(actions) == 1
    assert nav.last_target is not None and nav.last_target.surface == "minimap"


def test_missing_map_key_degrades_without_blind_click() -> None:
    # 无显式 keymap 且 profile 无目录：无法解析 full_map 动作 -> 降级
    nav = make_nav(make_config())
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    assert nav.tick(obs, 0.0) == []
    assert nav.tick(obs, 0.1) == []
    assert nav.surface == "minimap"
    assert nav.clicks == 0


def test_map_key_loaded_from_profile_keys_file(tmp_path: Path) -> None:
    (tmp_path / "keys.yaml").write_text("actions:\n  full_map: m\n", encoding="utf-8")
    nav = AutoPilotNavigator(
        make_config(),
        make_profile(profile_dir=tmp_path),
        rng=random.Random(3),
    )
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    assert nav.tick(obs, 0.0) == []
    assert key_actions(nav.tick(obs, 0.1)) == [("key_down", "m"), ("key_up", "m")]


def test_observation_missing_during_map_sequence_sends_close_and_aborts() -> None:
    events = _Events()
    nav = make_nav(make_config(), keymap=make_keymap(), events=events)
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    nav.tick(obs, 0.0)
    assert key_actions(nav.tick(obs, 0.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.phase == "wait_map"

    missing = make_observation(None, window_missing=True)
    assert nav.tick(missing, 1.2) == []  # 宽限期内不动作、不盲点
    actions = nav.tick(missing, 3.2)  # 超过 deadline+2s 宽限
    assert key_actions(actions) == [("key_down", "m"), ("key_up", "m")]
    assert nav.phase == "reclick"
    assert nav.blocks_behavior is False
    assert events.last("autopilot_abort")["reason"] == "observation_missing"
    assert events.last("autopilot_abort")["map_maybe_open"] is False


# ---------------------------------------------------------------------------
# 生命周期：stop/reset 不补发旧点击
# ---------------------------------------------------------------------------
def test_stop_during_map_sequence_resets_and_does_not_replay() -> None:
    events = _Events()
    nav = make_nav(make_config(), keymap=make_keymap(), events=events)
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    nav.tick(obs, 0.0)
    assert key_actions(nav.tick(obs, 0.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.blocks_behavior is True
    nav.stop()
    assert nav.active is False
    assert nav.phase == "idle"
    assert nav.blocks_behavior is False
    assert nav.map_open is False
    assert nav.tick(obs, 0.2) == []
    assert nav.tick(obs, 1.3) == []
    stop_event = events.last("autopilot_stop")
    assert stop_event["map_maybe_open"] is True


def test_stop_disables_navigation() -> None:
    nav = make_nav(make_config(surface="minimap"), keymap=make_keymap())
    nav.start(0.0)
    nav.stop()
    assert nav.tick(make_observation((0.5, 0.8)), 0.0) == []
    assert nav.active is False


def test_reset_clears_state() -> None:
    nav = make_nav(make_config(), keymap=make_keymap())
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    nav.tick(obs, 0.0)
    nav.tick(obs, 0.1)
    nav.reset()
    assert nav.active is False
    assert nav.phase == "idle"
    assert nav.own_position_norm is None
    assert nav.last_target is None
    assert nav.clicks == 0
    assert nav.blocks_behavior is False
    assert nav.surface == "full_map"


# ---------------------------------------------------------------------------
# 旧 minimap 降级路径与无配置时不误点
# ---------------------------------------------------------------------------
def test_minimap_surface_keeps_legacy_direct_click() -> None:
    nav = make_nav(make_config(surface="minimap"), keymap=make_keymap())
    nav.start(0.0)
    obs = make_observation((0.8, 0.9))  # 目标 (0.5, 0.1)
    assert nav.tick(obs, 0.0) == []
    assert nav.phase == "click"
    assert nav.blocks_behavior is False
    actions = nav.tick(obs, 0.1)
    assert len(actions) == 1
    # 小地图内容区 (1000,600,800,480)：local (1400,648) -> screen (1500,698)
    assert (actions[0].x, actions[0].y) == (1500, 698)
    target = nav.last_target
    assert target is not None
    assert target.surface == "minimap"
    assert target.normalized == pytest.approx((0.5, 0.1))
    assert nav.clicks == 1
    # 周期重发
    assert nav.tick(obs, 30.0) == []
    assert nav.tick(obs, nav.next_click_at) == []  # begin cycle -> click 相位
    actions = nav.tick(obs, nav.next_click_at + 0.1)
    assert len(actions) == 1
    assert nav.clicks == 2


def test_center_target_when_configured() -> None:
    nav = make_nav(make_config(surface="minimap", target="center"), keymap=make_keymap())
    nav.start(0.0)
    obs = make_observation((0.2, 0.2))
    nav.tick(obs, 0.0)
    nav.tick(obs, 0.1)
    target = nav.last_target
    assert target is not None
    assert target.normalized == (0.5, 0.5)
    assert target.from_own_marker is False
    assert target.screen_px == (WINDOW.x + MINIMAP_PX[0] + 400, WINDOW.y + MINIMAP_PX[1] + 240)


def test_missing_minimap_config_returns_no_blind_click() -> None:
    profile = replace(make_profile(), minimap=None)
    events = _Events()
    nav = make_nav(
        make_config(surface="minimap", target="center"),
        profile=profile,
        keymap=make_keymap(),
        events=events,
    )
    nav.start(0.0)
    obs = make_observation((0.5, 0.8))
    assert nav.tick(obs, 0.0) == []
    assert nav.tick(obs, 0.1) == []
    assert nav.clicks == 0
    assert "autopilot_click_skipped" in events.names()


def test_user_target_always_uses_m_map_even_if_default_surface_is_minimap():
    nav = make_nav(make_config(surface="minimap"), keymap=make_keymap())
    nav.start(0)
    nav.replace_target((0.3, 0.6), 1)
    nav.stop()
    nav.start(2)
    obs = make_observation((0.5, 0.8), timestamp=2)
    nav.tick(obs, 2)
    assert key_actions(nav.tick(obs, 2.1)) == [("key_down", "m"), ("key_up", "m")]
    assert nav.surface == "full_map"


def test_user_target_without_full_map_calibration_is_rejected():
    nav = make_nav(make_config(full_map_rect=None), keymap=make_keymap())
    nav.start(0)
    with pytest.raises(ValueError, match="全屏地图"):
        nav.replace_target((0.3, 0.6), 1)
    assert nav.target_override is None
