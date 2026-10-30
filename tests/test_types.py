"""公共类型单元测试（详细设计 5.1 / 9.1）。"""

from __future__ import annotations

import pytest
from src.common.types import (
    Action,
    ActionKind,
    DeathSignals,
    MapMatch,
    MatchResult,
    NormalizedRect,
    Observation,
    Rect,
    State,
)


def test_normalized_rect_to_pixels() -> None:
    rect = NormalizedRect(0.25, 0.5, 0.75, 1.0)
    pixels = rect.to_pixels(2560, 1600)
    assert pixels == Rect(640, 800, 1280, 800)


def test_normalized_rect_roundtrip() -> None:
    rect = NormalizedRect.from_list([0.1, 0.2, 0.3, 0.4])
    assert rect.to_list() == [0.1, 0.2, 0.3, 0.4]


@pytest.mark.parametrize(
    "values",
    [
        (-0.1, 0.0, 0.5, 0.5),
        (0.5, 0.0, 0.5, 1.0),
        (0.0, 0.6, 0.5, 0.4),
        (0.0, 0.0, 1.1, 1.0),
    ],
)
def test_normalized_rect_rejects_invalid(values: tuple[float, ...]) -> None:
    with pytest.raises(ValueError):
        NormalizedRect(*values)


def test_rect_basics() -> None:
    rect = Rect(10, 20, 100, 50)
    assert rect.right == 110
    assert rect.bottom == 70
    assert rect.center == (60, 45)
    assert rect.contains(10, 20)
    assert not rect.contains(110, 20)


def test_rect_rejects_negative_size() -> None:
    with pytest.raises(ValueError):
        Rect(0, 0, -1, 10)


def test_action_helpers() -> None:
    click = Action.click_abs(100, 200, "nav_click")
    assert click.kind is ActionKind.CLICK_ABS
    assert (click.x, click.y) == (100, 200)
    assert click.to_dict() == {"kind": "click_abs", "x": 100, "y": 200, "priority": 10, "label": "nav_click"}

    down, up = Action.key_press("escape")
    assert down.kind is ActionKind.KEY_DOWN and up.kind is ActionKind.KEY_UP
    assert down.key == "escape"
    assert down.priority == 20

    move = Action.move_rel(-5, 7)
    assert (move.dx, move.dy) == (-5, 7)

    wait = Action.wait(250)
    assert wait.duration_ms == 250

    wheel = Action.wheel(-1, "ship_list_scroll")
    assert wheel.kind is ActionKind.MOUSE_WHEEL
    assert wheel.wheel_delta == -1

    expired = click.with_expiry(12.5)
    assert expired.expires_at == 12.5
    assert expired.priority == click.priority


def test_observation_frame_fields() -> None:
    obs = Observation(timestamp=3.0, window_rect=(0, 0, 10, 10), frame_id=7, frame_timestamp=2.5)
    assert obs.frame_id == 7
    assert obs.frame_timestamp == 2.5
    assert not obs.window_missing
    assert obs.capture_error is None


def test_match_result_and_observation_helpers() -> None:
    result = MatchResult("port_indicator", True, 0.93, center=(5, 6), rect=(1, 2, 8, 8), timestamp=1.0)
    assert result.to_dict()["found"] is True

    obs = Observation(timestamp=1.0, window_rect=(0, 0, 100, 100), features={"port_indicator": result})
    assert obs.found("port_indicator")
    assert obs.score("port_indicator") == 0.93
    assert obs.feature("missing") is None
    assert not obs.found("missing")


def test_map_match_serialization() -> None:
    match = MapMatch(
        map_id="north_sea",
        inliers=42,
        score=0.8,
        affine=(0.1, 0.0, 5.0, 0.0, 0.1, 6.0),
        reprojection_error=0.7,
        timestamp=2.0,
        mirrored=False,
        rotation_deg=0.4,
        scale=0.1,
    )
    data = match.to_dict()
    assert data["map_id"] == "north_sea"
    assert data["inliers"] == 42


def test_death_signals_vote_property() -> None:
    dead = DeathSignals(True, False, True, False, False, score=0.8, stable_ms=1200)
    alive = DeathSignals(False, False, False, False, False, score=0.0, stable_ms=0)
    assert dead.dead
    assert not alive.dead


def test_state_helpers() -> None:
    assert State.BATTLE_ACTIVE.is_battle
    assert State.DEAD_PENDING_EXIT.is_battle
    assert not State.PORT.is_battle
    assert State.PAUSED.is_stopped
    assert State.STOPPING.is_stopped
    assert not State.ERROR_RECOVERY.is_stopped
