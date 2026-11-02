"""死亡退出与自然结算交接回归；不发送真实键鼠。"""

from dataclasses import replace
from pathlib import Path

import pytest
from src.common.types import State

from tests.test_state_machine import Harness, _death_signals, _match, drive_to_battle_active


def stalled_exit(tmp_path: Path, *, numeric: bool = True) -> Harness:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features = {}
    h.machine._battles_completed = 10
    h.perception.death = replace(_death_signals(h.clock.now()), hp_current=0 if numeric else None)
    h.step()
    h.clock.advance(2.5)
    h.perception.death = replace(_death_signals(h.clock.now()), hp_current=0 if numeric else None)
    h.step()
    assert h.machine.state is State.EXIT_MENU_OPEN
    assert [action.label for action in h.step()] == ["exit_menu", "exit_menu"]
    h.clock.advance(9)
    h.perception.death = _death_signals(h.clock.now(), authorized=False, hp_evidence="unknown")
    assert h.step() == []
    return h


@pytest.mark.parametrize("fresh_zero", [False, True])
def test_eleventh_battle_waits_without_input_then_continues_result(tmp_path: Path, fresh_zero: bool) -> None:
    h = stalled_exit(tmp_path)
    assert h.machine.state is State.DEAD_PENDING_EXIT
    starts = (h.autopilot.started, h.behavior.started)
    for _ in range(4):
        h.clock.advance(3)
        h.perception.death = replace(_death_signals(h.clock.now(), authorized=fresh_zero), hp_current=0)
        assert h.step() == []
        assert h.machine.state is State.DEAD_PENDING_EXIT
    assert (h.autopilot.started, h.behavior.started) == starts
    assert not h.machine._recovery_times
    assert h.machine.battles_completed == 10
    h.perception.features = {"battle_results": _match("battle_results")}
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    h.perception.features["result_continue_battle_button"] = _match("result_continue_battle_button")
    actions = h.step()
    assert any(action.label == "result_continue" for action in actions)
    h.perception.features = {"queue_indicator": _match("queue_indicator")}
    h.step()
    assert h.machine.battles_completed == 11
    assert h.machine.state is State.START_COOP
    assert h.machine._dead_result_wait_until is None


def test_passive_death_wait_accepts_port(tmp_path: Path) -> None:
    h = stalled_exit(tmp_path)
    h.perception.features = {"port_indicator": _match("port_indicator")}
    assert h.step() == []
    assert h.machine.state is State.POST_EXIT_INTERFACE
    h.step()
    assert h.machine.battles_completed == 11


def test_passive_death_wait_has_bounded_timeout(tmp_path: Path) -> None:
    h = stalled_exit(tmp_path)
    h.clock.advance(h.app.global_config.timeouts.exit_s + 0.1)
    assert h.step() == []
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "death_result_timeout"
    assert h.machine.battles_completed == 10


def test_passive_dead_wait_survives_manual_pause_without_resetting_deadline(tmp_path: Path) -> None:
    h = stalled_exit(tmp_path)
    h.clock.advance(h.app.global_config.timeouts.exit_s - 1)
    h.machine.request_pause("manual")
    h.perception.detected = State.DEAD_PENDING_EXIT
    h.controller.resume()
    h.machine.request_resume()
    h.step()
    h.clock.advance(2)
    assert h.step() == []
    assert h.machine.state is State.PAUSED
    assert h.machine.pause_reason == "death_result_timeout"


def test_exit_timeout_without_authorized_context_keeps_recovery(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine._transition(State.EXIT_MENU_OPEN, "test_no_context")
    h.step()
    h.clock.advance(9)
    h.step()
    assert h.machine.state is State.ERROR_RECOVERY


def test_legacy_death_evidence_does_not_restart_behavior_on_resume(tmp_path: Path) -> None:
    h = stalled_exit(tmp_path, numeric=False)
    h.machine.request_pause("manual")
    h.perception.detected = State.BATTLE_ACTIVE
    h.controller.resume()
    h.machine.request_resume()
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    assert h.step() == []


def test_wait_keeps_result_limit_and_explicit_stop(tmp_path: Path) -> None:
    h = stalled_exit(tmp_path)
    h.machine.set_rotation_limit(11)
    h.perception.features = {
        feature: _match(feature) for feature in
        ("battle_results", "result_continue_battle_button", "result_return_port_button")
    }
    h.step()
    actions = h.step()
    assert any(action.label == "result_return_port" for action in actions)
    assert not any(action.label == "result_continue" for action in actions)
    h.machine.request_stop("gui_stop")
    assert h.step() == []
    assert h.machine.state is State.STOPPING
    assert h.machine._dead_result_wait_until is None
