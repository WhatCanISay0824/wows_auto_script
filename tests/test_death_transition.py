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


def test_late_confirmation_resumes_exit_and_counts_once(tmp_path: Path) -> None:
    h = stalled_exit(tmp_path)
    starts = (h.autopilot.started, h.behavior.started)
    h.perception.features = {"exit_confirm": _match("exit_confirm")}
    h.clock.advance(12)

    assert h.step() == []
    assert h.machine.state is State.EXIT_CONFIRMING
    actions = h.step()
    assert [action.label for action in actions] == ["exit_confirm"]
    assert h.machine.state is State.EXITING
    assert h.step() == []  # 复用同一帧也不能再点一次。
    assert (h.autopilot.started, h.behavior.started) == starts
    h.perception.features = {"port_indicator": _match("port_indicator")}
    h.step()
    h.step()
    assert h.machine.battles_completed == 11
    assert h.session.battle_ends == 1
    assert h.machine._dead_result_wait_until is None


def test_late_menu_resumes_without_escape(tmp_path: Path) -> None:
    h = stalled_exit(tmp_path)
    h.perception.features = {key: _match(key) for key in ("exit_menu_panel", "return_port")}
    actions = h.step()
    assert [action.label for action in actions] == ["exit_return_port"]
    assert h.machine.state is State.EXIT_CONFIRMING


@pytest.mark.parametrize("age", [-1.0, 5.0])
def test_late_dialog_requires_fresh_frame(tmp_path: Path, age: float) -> None:
    h = stalled_exit(tmp_path)
    h.perception.features = {"exit_confirm": _match("exit_confirm")}
    observation = replace(h.obs(), frame_timestamp=h.clock.now() - age)
    assert h.machine.step(observation, h.clock.now()) == []
    assert h.machine.state is State.DEAD_PENDING_EXIT
    h.clock.advance(0.6)
    assert h.step() == []
    assert h.machine.state is State.EXIT_CONFIRMING


@pytest.mark.parametrize("invalid_context", ["no_battle_id", "no_battle", "alive"])
def test_late_dialog_requires_authorized_battle_context(tmp_path: Path, invalid_context: str) -> None:
    h = stalled_exit(tmp_path)
    if invalid_context == "no_battle_id":
        h.machine._battle_id = None
    elif invalid_context == "no_battle":
        h.machine._battle = None
    else:
        h.machine._battle_result = ""
    h.perception.features = {"exit_confirm": _match("exit_confirm")}
    assert h.step() == []
    assert h.machine.state is State.DEAD_PENDING_EXIT


def test_reentered_menu_timeout_preserves_first_deadline(tmp_path: Path) -> None:
    h = stalled_exit(tmp_path)
    deadline = h.machine._dead_result_wait_until
    h.clock.advance(20)
    h.machine._transition(State.EXIT_MENU_OPEN, "test_resume_menu")
    h.step()
    h.clock.advance(9)
    h.step()
    assert h.machine.state is State.DEAD_PENDING_EXIT
    assert h.machine._dead_result_wait_until == deadline


@pytest.mark.parametrize("priority", ["timeout", "result", "port", "stop", "pause"])
def test_late_dialog_keeps_guards_and_terminal_interfaces_first(tmp_path: Path, priority: str) -> None:
    h = stalled_exit(tmp_path)
    h.perception.features = {"exit_confirm": _match("exit_confirm")}
    expected = State.PAUSED
    if priority == "timeout":
        h.clock.advance(h.app.global_config.timeouts.exit_s + 0.1)
    elif priority == "result":
        h.perception.features["battle_results"] = _match("battle_results")
        expected = State.BATTLE_FINISHED
    elif priority == "port":
        h.perception.features["port_indicator"] = _match("port_indicator")
        expected = State.POST_EXIT_INTERFACE
    elif priority == "stop":
        h.machine.request_stop("gui_stop")
        expected = State.STOPPING
    else:
        h.machine.request_pause("manual")
    assert h.step() == []
    assert h.machine.state is expected
