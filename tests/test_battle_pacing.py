"""回港及结算继续战斗共用配置化的随机等待。"""

from pathlib import Path

from src.common.types import State

from tests.test_state_machine import Harness, _match, drive_to_battle_active


def result_harness(tmp_path: Path) -> Harness:
    h = Harness(tmp_path)
    drive_to_battle_active(h)
    h.perception.features = {k: _match(k) for k in ("battle_results", "result_continue_battle_button")}
    h.session.sample_between_battle_s = lambda: 70.0
    h.step()
    assert h.machine.state is State.BATTLE_FINISHED
    return h


def test_continue_battle_waits_once_and_does_not_consume_button_timeout(tmp_path: Path) -> None:
    h = result_harness(tmp_path)
    calls = []
    h.session.sample_between_battle_s = lambda: calls.append(1) or 70.0
    assert not any(a.label == "result_continue" for a in h.step())
    h.clock.advance(61)
    assert not any(a.label == "result_continue" for a in h.step())
    assert h.machine.state is State.BATTLE_FINISHED
    h.clock.advance(10)
    assert any(a.label == "result_continue" for a in h.step())
    assert calls == [1]


def test_long_port_interval_is_not_clamped(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.machine._battles_completed = 1
    h.session.sample_between_battle_s = lambda: 70.0
    h.machine._transition(State.PORT, "test")
    assert h.machine._port_wait_until == h.clock.now() + 70
    h.clock.advance(65)
    assert h.step() == []
    assert h.machine.state is State.PORT


def test_late_continue_button_gets_full_wait_without_false_timeout(tmp_path: Path) -> None:
    h = result_harness(tmp_path)
    h.perception.features = {"battle_results": _match("battle_results")}
    h.clock.advance(45)
    assert not any(a.label == "result_continue" for a in h.step())
    h.perception.features["result_continue_battle_button"] = _match("result_continue_battle_button")
    assert not any(a.label == "result_continue" for a in h.step())
    h.clock.advance(65)
    assert not any(a.label == "result_continue" for a in h.step())
    assert h.machine.state is State.BATTLE_FINISHED
    h.clock.advance(6)
    assert any(a.label == "result_continue" for a in h.step())


def test_stop_at_result_does_not_wait_before_return(tmp_path: Path) -> None:
    h = result_harness(tmp_path)
    h.machine.set_rotation_limit(1)
    h.perception.features['result_return_port_button'] = _match('result_return_port_button')
    assert any(a.label == 'result_return_port' for a in h.step())


def test_result_wait_survives_pause_resume_without_resampling(tmp_path: Path) -> None:
    h = result_harness(tmp_path)
    calls = []
    h.session.sample_between_battle_s = lambda: calls.append(1) or 70.0
    h.step()
    h.clock.advance(50)
    h.machine.request_pause('manual')
    h.perception.detected = State.BATTLE_FINISHED
    h.controller.resume()
    h.machine.request_resume()
    h.step()
    h.clock.advance(21)
    assert any(a.label == 'result_continue' for a in h.step())
    assert calls == [1]


def test_return_to_port_during_result_wait_uses_remaining_time(tmp_path: Path) -> None:
    h = result_harness(tmp_path)
    calls = []
    h.session.sample_between_battle_s = lambda: calls.append(1) or 70.0
    h.step()
    deadline = h.machine._port_wait_until
    h.clock.advance(25)
    h.perception.features = {k: _match(k) for k in ('battle_results', 'result_return_port_button')}
    assert any(a.label == 'result_return_port' for a in h.step())
    h.perception.features = {'port_indicator': _match('port_indicator')}
    h.step()
    h.step()
    assert h.machine.state is State.PORT
    assert h.machine._port_wait_until == deadline
    assert calls == [1]


def test_each_result_draws_new_interval_and_first_launch_has_no_wait(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    calls = []
    values = iter((30., 65.))
    h.session.sample_between_battle_s = lambda: calls.append(1) or next(values)
    h.machine._transition(State.PORT, 'first')
    assert calls == []
    h.machine._ensure_between_battle_wait(10, 1)
    assert h.machine._port_wait_until == 40
    h.machine._ensure_between_battle_wait(20, 1)
    assert h.machine._port_wait_until == 40
    h.machine._ensure_between_battle_wait(100, 2)
    assert h.machine._port_wait_until == 165
    assert calls == [1, 1]
