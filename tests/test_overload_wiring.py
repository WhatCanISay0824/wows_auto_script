"""过载保护与运行服务/CLI 的接线测试（Lead 集成层，2026-10-31）。

覆盖 M0 + 过载监控的接缝，防止“策略单测通过但实际没接上”：
- 安全事件回调对 OVERLOAD_PAUSE/STOP 也立即关门禁并请求释放（与 F12 同一路径）；
- 过载监控能挂到 SafetyWatchdog（runner 的 attach 适配）；
- SessionRunner 每轮把“脚本是否控制中/前台/状态”交给监控，且性能视图补充
  主循环节拍与截屏耗时；
- CLI 轮换遇到 overload_* 终止整个轮换，而不是继续切下一账号。
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest
from src.execution.safety import SafetyEvent, SafetyEventKind
from src.runtime import runner as runner_mod
from src.runtime.messages import PerformanceView
from src.runtime.runner import SessionRunner, _make_safety_event_handler, attach_overload_monitor


class _FakeGateExecutor:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def close_gate_and_release(self, reason: str = "") -> int:
        self.closed.append(reason)
        return 1


class _FakeLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.debugs: list[str] = []

    def warning(self, fmt: str, *args) -> None:  # noqa: ANN002 - 测试替身
        self.warnings.append(fmt % args if args else fmt)

    def info(self, fmt: str, *args) -> None:  # noqa: ANN002
        self.infos.append(fmt % args if args else fmt)

    def debug(self, fmt: str, *args) -> None:  # noqa: ANN002
        self.debugs.append(fmt % args if args else fmt)


@pytest.mark.parametrize(
    "kind",
    [SafetyEventKind.OVERLOAD_PAUSE_REQUESTED, SafetyEventKind.OVERLOAD_STOP_REQUESTED],
)
def test_overload_events_close_gate(kind: SafetyEventKind) -> None:
    executor = _FakeGateExecutor()
    handler = _make_safety_event_handler(executor, _FakeLogger())  # type: ignore[arg-type]
    handler(SafetyEvent(kind, 1.0))
    assert executor.closed == [f"safety:{kind.value}"]


@pytest.mark.parametrize("kind", [SafetyEventKind.OVERLOAD_WARNING, SafetyEventKind.OVERLOAD_RESUME_REQUESTED])
def test_non_stop_overload_events_do_not_close_gate(kind: SafetyEventKind) -> None:
    executor = _FakeGateExecutor()
    handler = _make_safety_event_handler(executor, _FakeLogger())  # type: ignore[arg-type]
    handler(SafetyEvent(kind, 1.0))
    assert executor.closed == []


def test_attach_overload_monitor_prefers_attach_method() -> None:
    class _Watchdog:
        def __init__(self) -> None:
            self.attached: list[object] = []

        def attach_overload_monitor(self, monitor: object) -> bool:
            self.attached.append(monitor)
            return True

    watchdog = _Watchdog()
    monitor = object()
    assert attach_overload_monitor(watchdog, monitor) is True
    assert watchdog.attached == [monitor]


def _fake_self(*, state: str, foreground: bool, gate_open: bool) -> SimpleNamespace:
    runner_mod._LAST_WINDOW[0] = SimpleNamespace(foreground=foreground)
    contexts: list[dict[str, object]] = []
    monitor = SimpleNamespace(set_context=lambda **kwargs: contexts.append(kwargs))

    class _Machine:
        value = None

        def __init__(self) -> None:
            self.state = SimpleNamespace(value=state)

    fake = SimpleNamespace(
        runtime=SimpleNamespace(
            overload_monitor=monitor,
            machine=_Machine(),
            executor=SimpleNamespace(gate_open=gate_open),
        ),
        _logger=_FakeLogger(),
        _overload_state_since_at=12.5,
        _contexts=contexts,
    )
    return fake


def test_runner_passes_control_context_to_overload_monitor() -> None:
    fake = _fake_self(state="BATTLE_ACTIVE", foreground=True, gate_open=True)
    SessionRunner._update_overload_context(fake, 20.0)  # type: ignore[arg-type]
    assert fake._contexts == [
        {"state": "BATTLE_ACTIVE", "controlling": True, "transition_at": 12.5, "foreground": True}
    ]


def test_runner_does_not_authorize_overload_stop_when_not_controlling() -> None:
    for state, foreground, gate_open in (
        ("PAUSED", True, False),
        ("BATTLE_ACTIVE", False, True),
        ("STOPPING", True, False),
    ):
        fake = _fake_self(state=state, foreground=foreground, gate_open=gate_open)
        SessionRunner._update_overload_context(fake, 20.0)  # type: ignore[arg-type]
        assert fake._contexts[0]["controlling"] is False


def test_performance_view_enriches_capture_and_loop_tick() -> None:
    class _Engine:
        def stats(self) -> SimpleNamespace:
            return SimpleNamespace(capture_ms_avg=18.5)

    class _Monitor:
        def performance_view(self) -> PerformanceView:
            return PerformanceView(enabled=True, mode="monitor")

    fake = SimpleNamespace(
        runtime=SimpleNamespace(overload_monitor=_Monitor(), engine=_Engine()),
        _perf_error_logged=False,
        _logger=_FakeLogger(),
        _tick_samples=deque([0.10, 0.11, 0.12, 0.30, 0.10]),
    )
    fake._loop_tick_p95_ms = lambda: SessionRunner._loop_tick_p95_ms(fake)  # type: ignore[attr-defined]
    view = SessionRunner.performance_view(fake)  # type: ignore[arg-type]
    assert view is not None
    assert view.capture_ms_avg == 18.5
    assert view.loop_tick_p95_ms == pytest.approx(300.0)
    assert view.mode == "monitor"


def test_cli_rotation_terminates_on_overload_reason(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    import src.main as main_mod

    app = SimpleNamespace(account=None, global_config=SimpleNamespace(paths=SimpleNamespace(data_dir="data")))
    runtime = SimpleNamespace()

    class _Rotator:
        def __init__(self) -> None:
            self.current = SimpleNamespace(alias="alt1")
            self.advanced = 0
            self.recorded: list[str | None] = []

        def current_limit(self) -> int:
            return 3

        def record_battle(self, alias: str | None) -> None:
            self.recorded.append(alias)

        def advance(self) -> None:
            self.advanced += 1

    rotator = _Rotator()
    monkeypatch.setattr(main_mod, "_load_app", lambda *a, **k: app)
    monkeypatch.setattr(main_mod, "validate_app", lambda app_: [])
    monkeypatch.setattr(main_mod, "_build_runtime", lambda app_: runtime)
    monkeypatch.setattr(main_mod, "_teardown", lambda rt: None)
    monkeypatch.setattr(main_mod, "_run_session", lambda runtime_, *, max_battles=None: ("overload_commit", 0))
    args = SimpleNamespace(profile="cn360", ship="test", log_level=None, max_battles=None)
    code = main_mod._run_accounts(args, tmp_path, ["alt1", "alt2"], rotator)  # type: ignore[arg-type]
    assert code == main_mod.EXIT_OK
    assert rotator.advanced == 0  # 不切下一账号
    assert rotator.recorded == ["alt1"]


def test_python_version_supports_ctypes_pointer_arithmetic() -> None:
    """过载采样依赖 64 位 ctypes 结构体；32 位解释器必须降级（自检在 sysmetrics）。"""
    assert sys.maxsize > 2**32
    assert Path("src/common/sysmetrics.py").is_file()
