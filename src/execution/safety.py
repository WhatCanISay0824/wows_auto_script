"""安全控制：紧急停止/恢复热键、心跳看门狗与共享停止标志（详细设计 2.4 / 9.5 / 13.5）。

职责边界：

- 本模块**只读**键盘状态（``GetAsyncKeyState``），绝不发送键鼠事件；
  真正的输入释放由 :class:`src.execution.queue.InputExecutor` 的输入线程执行；
- F12 紧急停止 → ``STOPPING``（终止会话）；F11 恢复 → 清除 PAUSED，由状态机从 INIT
  重新识别界面（评审补充 P1-1）；
- 主循环心跳超时 → PAUSED：看门狗只设置停止标志并产生事件，由主线程负责停止子流程、
  请求输入线程释放键鼠；
- **ISS-005**：``SafetyWatchdog(on_event=...)`` 的事件回调允许在监控线程内**直接**调用
  :meth:`InputExecutor.close_gate_and_release`（线程安全、非阻塞）来关闭业务输入门禁并
  请求释放，不再依赖主循环下一次同步；状态机转移与日志收尾仍由运行线程完成，
  监控线程不直接发送游戏操作；
- ``GetAsyncKeyState`` 通过 :class:`KeyStateSource` 协议抽象，单元测试注入
  :class:`FakeKeyStateSource`，不读取真实键盘（详细设计 14.1）。

看门狗线程以 20 Hz 轮询（详细设计 2.4）：:class:`SafetyWatchdog` 只是线程外壳，
全部状态机逻辑在 :class:`SafetyMonitor` 中，可离线单步测试。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from ctypes import wintypes
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ..common.clock import Clock, SystemClock
from ..common.errors import InputUnavailable, SafetyStop, StopRequest
from ..config.models import SafetyConfig
from .winput import resolve_key

if TYPE_CHECKING:  # 只读类型提示；运行时由 main/服务注入，避免 safety <-> overload 循环导入
    from .overload import OverloadMonitor

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    _user32.GetAsyncKeyState.restype = wintypes.SHORT
else:  # pragma: no cover - 项目只支持 Windows
    _user32 = None

#: 看门狗默认轮询频率 20 Hz（详细设计 2.4）
WATCHDOG_HZ = 20.0
WATCHDOG_INTERVAL_S = 1.0 / WATCHDOG_HZ

#: GetAsyncKeyState 返回值的高位表示“当前按下”
_KEY_DOWN_MASK = 0x8000


# ---------------------------------------------------------------------------
# 键盘状态源
# ---------------------------------------------------------------------------


@runtime_checkable
class KeyStateSource(Protocol):
    """按键状态源：只读查询某个可读按键当前是否按下。"""

    def is_down(self, key: str) -> bool:
        """返回按键当前是否处于按下状态。"""


class Win32KeyStateSource:
    """真实键盘状态源：轮询 ``user32.GetAsyncKeyState``（详细设计 9.5）。"""

    def __init__(self) -> None:
        if not _IS_WINDOWS:
            raise InputUnavailable("当前平台不支持 GetAsyncKeyState（项目只支持 Windows）")

    def is_down(self, key: str) -> bool:
        binding = resolve_key(key)
        if binding.is_mouse:
            vk = 0x01 if binding.mouse_button == "left" else 0x02 if binding.mouse_button == "right" else 0x04
        else:
            vk = binding.vk
        try:
            state = int(_user32.GetAsyncKeyState(vk))  # type: ignore[union-attr]
        except (OSError, TypeError) as exc:  # pragma: no cover - 极罕见
            logger.error("GetAsyncKeyState 调用失败: %s", exc)
            return False
        return bool(state & _KEY_DOWN_MASK)


class FakeKeyStateSource:
    """测试辅助按键状态源（与 :class:`~src.common.clock.FakeClock` 同口径）。"""

    def __init__(self, down: Iterable[str] = ()) -> None:
        self._down: set[str] = set()
        for key in down:
            self.press(key)

    def press(self, key: str) -> None:
        self._down.add(resolve_key(key).name)

    def release(self, key: str) -> None:
        self._down.discard(resolve_key(key).name)

    def release_all(self) -> None:
        self._down.clear()

    @property
    def down_keys(self) -> frozenset[str]:
        return frozenset(self._down)

    def is_down(self, key: str) -> bool:
        return resolve_key(key).name in self._down


# ---------------------------------------------------------------------------
# 共享停止标志
# ---------------------------------------------------------------------------


class StopController:
    """主线程、输入线程、看门狗线程共享的停止/暂停标志（详细设计 13.5）。

    - :meth:`request_stop`：终止运行（F12 紧急停止、会话上限、连续异常等）。
      一旦进入停止状态不可恢复，重复请求保留第一次的原因，便于定位根因；
    - :meth:`request_pause`：暂停（失焦、心跳超时等），可由 :meth:`resume` 恢复；
      停止状态下的暂停请求被忽略；
    - :meth:`resume`：清除暂停状态（F11 恢复）；若已进入停止状态则返回 False。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop_request: StopRequest | None = None
        self._pause_request: StopRequest | None = None

    # --- 状态查询 ---------------------------------------------------
    @property
    def stop_requested(self) -> bool:
        with self._lock:
            return self._stop_request is not None

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._pause_request is not None

    @property
    def running(self) -> bool:
        with self._lock:
            return self._stop_request is None and self._pause_request is None

    @property
    def stop_request(self) -> StopRequest | None:
        with self._lock:
            return self._stop_request

    @property
    def pause_request(self) -> StopRequest | None:
        with self._lock:
            return self._pause_request

    # --- 请求 -------------------------------------------------------
    def request_stop(self, reason: str, detail: str = "", hook: str = "") -> StopRequest:
        """请求终止运行（幂等，保留第一次的原因）。"""
        with self._lock:
            if self._stop_request is None:
                self._stop_request = StopRequest(reason=reason, detail=detail, hook=hook)
                logger.warning("请求停止运行: %s", self._stop_request.format())
            return self._stop_request

    def request_pause(self, reason: str, detail: str = "", hook: str = "") -> StopRequest | None:
        """请求暂停运行；已进入停止状态时返回 None。幂等，保留第一次暂停原因。"""
        with self._lock:
            if self._stop_request is not None:
                return None
            if self._pause_request is None:
                self._pause_request = StopRequest(reason=reason, detail=detail, hook=hook)
                logger.warning("请求暂停运行: %s", self._pause_request.format())
            return self._pause_request

    def resume(self, *, expected_pause: StopRequest | None = None) -> bool:
        """清除暂停状态；返回此前是否处于暂停状态（停止状态不可恢复）。"""
        with self._lock:
            if self._stop_request is not None:
                return False
            if expected_pause is not None and self._pause_request is not expected_pause:
                return False
            was_paused = self._pause_request is not None
            self._pause_request = None
            if was_paused:
                logger.info("恢复运行（清除暂停状态）")
            return was_paused

    def reset(self) -> None:
        """清空停止/暂停标志（新一轮会话或测试场景使用）。"""
        with self._lock:
            self._stop_request = None
            self._pause_request = None

    def raise_if_stopped(self) -> None:
        """已请求停止时抛 :class:`~src.common.errors.SafetyStop`，供主循环快速退出。"""
        with self._lock:
            request = self._stop_request
        if request is not None:
            raise SafetyStop(request)


# ---------------------------------------------------------------------------
# 安全事件
# ---------------------------------------------------------------------------


class SafetyEventKind(StrEnum):
    """看门狗产生的事件类型。"""

    EMERGENCY_STOP = "emergency_stop"
    RESUME = "resume"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    # 过载保护（可选 OverloadMonitor 注入；不改变 F12/心跳/恢复路径）
    OVERLOAD_WARNING = "overload_warning"
    OVERLOAD_PAUSE_REQUESTED = "overload_pause_requested"
    OVERLOAD_STOP_REQUESTED = "overload_stop_requested"
    OVERLOAD_RESUME_REQUESTED = "overload_resume_requested"


@dataclass(frozen=True, slots=True)
class SafetyEvent:
    """一次安全事件（供主循环写日志、切换状态）。"""

    kind: SafetyEventKind
    timestamp: float
    request: StopRequest | None = None
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.detail:
            object.__setattr__(self, "detail", dict(self.detail))

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {"kind": self.kind.value, "timestamp": round(self.timestamp, 3)}
        if self.request is not None:
            data["reason"] = self.request.reason
            data["detail"] = self.request.detail
        if self.detail:
            data["extra"] = dict(self.detail)
        return data


@dataclass(frozen=True, slots=True)
class _KeyEdge:
    """热键边沿检测状态。"""

    key: str
    was_down: bool = False


class SafetyMonitor:
    """安全监控状态机：热键边沿检测 + 主循环心跳超时（详细设计 9.5）。

    由 :class:`SafetyWatchdog` 周期调用 :meth:`poll`；测试可以直接传入假时钟单步驱动。
    """

    def __init__(
        self,
        key_source: KeyStateSource,
        *,
        stop_key: str = "f12",
        resume_key: str = "f11",
        heartbeat_s: float = 10.0,
        controller: StopController | None = None,
        clock: Clock | None = None,
    ) -> None:
        if heartbeat_s <= 0:
            raise ValueError(f"心跳超时时间必须为正数: {heartbeat_s!r}")
        self._key_source = key_source
        self._stop_key = resolve_key(stop_key).name
        self._resume_key = resolve_key(resume_key).name
        self._heartbeat_s = float(heartbeat_s)
        self._controller = controller or StopController()
        self._clock = clock or SystemClock()
        now = self._clock.now()
        self._last_heartbeat = now
        self._stop_edge = _KeyEdge(self._stop_key, was_down=self._key_source.is_down(self._stop_key))
        self._resume_edge = _KeyEdge(self._resume_key, was_down=self._key_source.is_down(self._resume_key))
        self._heartbeat_timed_out = False

    @classmethod
    def from_config(
        cls,
        config: SafetyConfig,
        key_source: KeyStateSource,
        *,
        controller: StopController | None = None,
        clock: Clock | None = None,
    ) -> SafetyMonitor:
        """按全局 ``safety`` 配置创建监控器（热键与心跳超时均来自配置）。"""
        return cls(
            key_source,
            stop_key=config.emergency_stop_key,
            resume_key=config.resume_key,
            heartbeat_s=config.heartbeat_s,
            controller=controller,
            clock=clock,
        )

    @property
    def controller(self) -> StopController:
        return self._controller

    @property
    def clock(self) -> Clock:
        """监控器使用的时钟（看门狗驱动过载监控时复用同一假时钟/系统时钟）。"""
        return self._clock

    @property
    def heartbeat_s(self) -> float:
        return self._heartbeat_s

    @property
    def last_heartbeat(self) -> float:
        return self._last_heartbeat

    def heartbeat(self, now: float | None = None) -> None:
        """主循环每轮调用，刷新心跳时间戳并解除本监控器的心跳超时锁存。"""
        self._last_heartbeat = self._clock.now() if now is None else float(now)
        self._heartbeat_timed_out = False

    def poll(self, now: float | None = None) -> list[SafetyEvent]:
        """单步轮询：返回本次轮询产生的全部安全事件（可能为空列表）。"""
        moment = self._clock.now() if now is None else float(now)
        events: list[SafetyEvent] = []

        # 1) 紧急停止热键：只在按下的上升沿触发一次；已停止时不重复产生事件
        stop_down = bool(self._key_source.is_down(self._stop_key))
        if stop_down and not self._stop_edge.was_down and not self._controller.stop_requested:
            request = self._controller.request_stop(
                "emergency_stop",
                f"检测到紧急停止热键 {self._stop_key}",
                hook="safety_hotkey",
            )
            events.append(SafetyEvent(SafetyEventKind.EMERGENCY_STOP, moment, request))
        self._stop_edge = _KeyEdge(self._stop_key, was_down=stop_down)

        # 2) 恢复热键：仅在暂停状态下生效（停止状态不可恢复）
        resume_down = bool(self._key_source.is_down(self._resume_key))
        if resume_down and not self._resume_edge.was_down and self._controller.resume():
            events.append(SafetyEvent(SafetyEventKind.RESUME, moment))
        self._resume_edge = _KeyEdge(self._resume_key, was_down=resume_down)

        # 3) 心跳看门狗：运行中且超过 heartbeat_s 没有心跳 -> 暂停（只锁存一次）
        if (
            self._controller.running
            and not self._heartbeat_timed_out
            and moment - self._last_heartbeat > self._heartbeat_s
        ):
            elapsed = moment - self._last_heartbeat
            request = self._controller.request_pause(
                "heartbeat_timeout",
                f"主循环心跳超时 {elapsed:.1f}s > {self._heartbeat_s:.1f}s",
                hook="watchdog",
            )
            if request is not None:
                self._heartbeat_timed_out = True
                events.append(SafetyEvent(SafetyEventKind.HEARTBEAT_TIMEOUT, moment, request))
                logger.error("主循环心跳超时 %.1fs，请求暂停并停止输入", elapsed)

        return events


class SafetyWatchdog:
    """看门狗线程外壳：以 20 Hz 周期调用 :meth:`SafetyMonitor.poll`（详细设计 2.4）。

    只负责“周期轮询 + 分发事件”；收到事件后：
    - 主线程负责状态机转移与日志收尾；
    - ``on_event`` 回调（ISS-005）在监控线程内执行，可安全地调用
      :meth:`InputExecutor.close_gate_and_release` 立即关闭输入门禁并请求释放；
    - 监控线程不直接发送游戏操作（SendInput 只允许输入线程调用）；
    - 可选 ``overload``（:class:`~src.execution.overload.OverloadMonitor`）：在同一个
      20 Hz 循环里按 1 Hz 调用 ``overload.poll(now)`` 并分发其安全事件。F12/心跳/恢复
      的判定频率与顺序保持不变（先原有监控、后过载监控）。
    """

    def __init__(
        self,
        monitor: SafetyMonitor,
        *,
        on_event: Callable[[SafetyEvent], None] | None = None,
        interval_s: float = WATCHDOG_INTERVAL_S,
        overload: OverloadMonitor | None = None,
    ) -> None:
        if interval_s <= 0:
            raise ValueError(f"看门狗轮询间隔必须为正数: {interval_s!r}")
        self._monitor = monitor
        self._on_event = on_event
        self._interval_s = float(interval_s)
        self._overload = overload
        self._last_overload_at: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def monitor(self) -> SafetyMonitor:
        return self._monitor

    @property
    def overload(self) -> OverloadMonitor | None:
        return self._overload

    def attach_overload_monitor(self, overload: OverloadMonitor | None) -> bool:
        """挂接/替换过载监控（``src.runtime.runner.attach_overload_monitor`` 的适配点）。

        允许在启动前后调用；挂接后下一次 ``run_once`` 即按 1 Hz 触达。返回是否挂接成功。
        """
        self._overload = overload
        self._last_overload_at = None
        if overload is not None:
            logger.info("看门狗已挂接过载监控: %s", type(overload).__name__)
        return True

    def set_overload_monitor(self, overload: OverloadMonitor | None) -> bool:
        """``attach_overload_monitor`` 的别名（兼容 runner 的接口探测顺序）。"""
        return self.attach_overload_monitor(overload)

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def run_once(self) -> list[SafetyEvent]:
        """执行一次轮询并分发事件（测试/主循环内联驱动用）。

        先执行原有安全监控（F12/F11/心跳），再按 1 Hz 触达过载监控；事件按产生顺序 dispatch。
        """
        events = self._monitor.poll()
        events.extend(self._poll_overload())
        for event in events:
            self._dispatch(event)
        return events

    def _poll_overload(self) -> list[SafetyEvent]:
        overload = self._overload
        if overload is None:
            return []
        now = self._monitor.clock.now()
        interval = float(getattr(overload, "poll_interval_s", 1.0) or 1.0)
        if self._last_overload_at is not None and now - self._last_overload_at < interval - 1e-9:
            return []
        self._last_overload_at = now
        try:
            return list(overload.poll(now))
        except Exception:  # 过载监控异常不能拖垮看门狗线程
            logger.exception("过载监控轮询异常（已忽略本轮）")
            return []

    def _dispatch(self, event: SafetyEvent) -> None:
        logger.warning("安全事件: %s", event.to_dict())
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:  # 回调异常不能拖垮看门狗线程
            logger.exception("安全事件回调异常: %s", event.kind.value)

    def start(self) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        thread = threading.Thread(target=self._run, name="safety-watchdog", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def join(self, timeout: float | None = None) -> bool:
        thread = self._thread
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(self._interval_s)


__all__ = [
    "FakeKeyStateSource",
    "KeyStateSource",
    "SafetyEvent",
    "SafetyEventKind",
    "SafetyMonitor",
    "SafetyWatchdog",
    "StopController",
    "WATCHDOG_HZ",
    "WATCHDOG_INTERVAL_S",
    "Win32KeyStateSource",
]
