"""状态 → 界面特征表（详细设计 5.4）。

内置默认表按详细设计 5.4 的表格给出“主判定特征 + 辅助特征”：第一个是主判定，
其余是辅助证据。``profile.state_features`` 可以按状态整体覆盖默认值；未覆盖的状态
沿用内置表。状态键使用 ``State`` 的字符串值（配置文件里就是 ``PORT`` 这样的文本）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..common.types import State

#: 内置默认表（详细设计 5.4）。每项第一个是主判定特征，其余为辅助特征。
DEFAULT_STATE_FEATURES: Mapping[State, tuple[str, ...]] = {
    State.PORT: (
        "port_indicator",
        "port_battle_button",
        "port_mode_selector",
        "coop_mode_selected",
    ),
    State.QUEUEING: ("queue_indicator", "cancel_queue_button"),
    State.LOADING: ("loading_indicator", "loading_tip_area"),
    State.BATTLE_ACTIVE: ("battle_hud_indicator", "hp_bar_present", "minimap_frame"),
    # 详细设计 5.4 的 DEAD 行对应本项目的 DEAD_PENDING_EXIT 状态；
    # 死亡的综合判定由 DeathDetector（T4）负责，这里只提供界面特征。
    State.DEAD_PENDING_EXIT: ("death_banner", "hp_bar_absent", "spectator_indicator"),
    State.BATTLE_FINISHED: ("battle_results", "result_panel"),
    State.EXIT_MENU_OPEN: ("exit_menu_panel", "exit_menu_return_port"),
    State.EXIT_CONFIRMING: ("exit_confirm_dialog", "exit_confirm_yes"),
    State.POST_EXIT_INTERFACE: ("popup_close_button", "reward_panel", "mission_panel"),
}

#: ``detect_state`` 的检查优先级（详细设计 5.4）：弹窗 → 退出菜单 → 死亡 → 战斗
#: 结果 → 战斗 → 加载 → 排队 → 港口；模态界面优先，避免被下层界面掩盖。
DEFAULT_STATE_PRIORITY: tuple[State, ...] = (
    State.POST_EXIT_INTERFACE,
    State.EXIT_CONFIRMING,
    State.EXIT_MENU_OPEN,
    State.DEAD_PENDING_EXIT,
    State.BATTLE_FINISHED,
    State.BATTLE_ACTIVE,
    State.LOADING,
    State.QUEUEING,
    State.PORT,
)


def _state_key(state: State | str) -> str:
    return state.value if isinstance(state, State) else str(state)


def default_state_features() -> dict[str, tuple[str, ...]]:
    """返回内置默认表的可变副本（键为状态字符串值）。"""
    return {state.value: tuple(feature_ids) for state, feature_ids in DEFAULT_STATE_FEATURES.items()}


def merge_state_features(
    overrides: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """把 ``profile.state_features`` 覆盖到默认表上，返回新的映射。

    覆盖语义是“按状态整体替换”（不是追加）：配置里某个状态给出新的特征列表时，
    该状态的默认列表被完全替换；配置里多出来的状态键直接加入。
    """
    merged = default_state_features()
    if not overrides:
        return merged
    for key, feature_ids in overrides.items():
        merged[_state_key(key)] = tuple(str(feature_id) for feature_id in feature_ids)
    return merged
