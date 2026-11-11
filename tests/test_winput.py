"""输入后端单元测试（详细设计 9.2 / 9.6，评审补充第 3 节第 4 项）。

测试**绝不真正发送键鼠事件**：``SendInput`` / ``SetCursorPos`` / ``mouse_event``
全部替换为记录参数的假实现，只验证结构体布局、归一化公式、键表与展开后的事件参数。
"""

from __future__ import annotations

import ctypes
import random
import sys

import pytest
import yaml
from src.common.clock import FakeClock, FakeSleeper
from src.common.errors import InputUnavailable
from src.common.types import Action, ActionKind, Rect
from src.execution import winput

FAKE_VIRTUAL_RECT = Rect(0, 0, 1920, 1080)


@pytest.mark.parametrize('key', ['mouse_left', 'r'])
def test_atomic_tap_sends_press_release_without_cursor_movement(key: str) -> None:
    fake = FakeSendInput()
    clock = FakeClock()
    backend = make_sendinput_backend(fake, clock)
    backend.send([Action.tap_key(key, 'test_fire')])
    assert len(fake.events) == 2
    assert not backend.pressed_keys
    assert not backend.pressed_buttons
    if key == 'mouse_left':
        assert [event.dwFlags for event in fake.mouse_events()] == [winput.MOUSEEVENTF_LEFTDOWN, winput.MOUSEEVENTF_LEFTUP]
    else:
        assert len(fake.key_events()) == 2


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["mouse_left", "r"])
def test_atomic_tap_releases_even_when_hold_is_interrupted(key: str) -> None:
    fake = FakeSendInput()
    backend = make_sendinput_backend(fake, FakeClock())

    class InterruptedHold:
        def sleep(self, seconds):
            raise RuntimeError("interrupted hold")

    backend._sleeper = InterruptedHold()
    with pytest.raises(RuntimeError, match="interrupted hold"):
        backend.send([Action.tap_key(key)])
    assert len(fake.events) == 2
    assert not backend.pressed_keys
    assert not backend.pressed_buttons


class FakeSendInput:
    """记录 SendInput 事件的假实现（不会触达 Win32）。"""

    def __init__(self) -> None:
        self.batches: list[list[winput.INPUT]] = []
        self.return_override: int | None = None
        self.raise_exc: Exception | None = None

    def __call__(self, events):  # noqa: ANN001 - 与 SendInputFn 签名一致
        batch = list(events)
        self.batches.append(batch)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.return_override is not None:
            return self.return_override
        return len(batch)

    @property
    def events(self) -> list[winput.INPUT]:
        return [event for batch in self.batches for event in batch]

    def clear(self) -> None:
        self.batches.clear()

    def key_events(self) -> list[winput.KEYBDINPUT]:
        return [event.ki for event in self.events if event.type == winput.INPUT_KEYBOARD]

    def mouse_events(self) -> list[winput.MOUSEINPUT]:
        return [event.mi for event in self.events if event.type == winput.INPUT_MOUSE]


def make_sendinput_backend(
    fake: FakeSendInput,
    clock: FakeClock,
    *,
    virtual_rect: Rect = FAKE_VIRTUAL_RECT,
    seed: int = 0,
) -> winput.SendInputBackend:
    """构造注入假实现的 SendInput 后端；扫描码解析返回 0 以走内置表。"""
    return winput.SendInputBackend(
        virtual_screen_provider=lambda: virtual_rect,
        send_input=fake,
        sleeper=FakeSleeper(clock),
        rng=random.Random(seed),
        scan_code_resolver=lambda _vk: 0,
    )


@pytest.fixture
def fake_send_input() -> FakeSendInput:
    return FakeSendInput()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


# ---------------------------------------------------------------------------
# 结构体布局（x64：INPUT=40 / MOUSEINPUT=32 / KEYBDINPUT=24）
# ---------------------------------------------------------------------------


def test_struct_layout_x64() -> None:
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "项目目标平台为 x64"
    assert ctypes.sizeof(winput.ULONG_PTR) == 8
    assert ctypes.sizeof(winput.MOUSEINPUT) == 32
    assert ctypes.sizeof(winput.KEYBDINPUT) == 24
    assert ctypes.sizeof(winput.HARDWAREINPUT) == 8
    assert ctypes.sizeof(winput.INPUT) == 40


def test_struct_field_offsets() -> None:
    assert winput.INPUT.type.offset == 0
    assert winput.MOUSEINPUT.dx.offset == 0
    assert winput.MOUSEINPUT.dwExtraInfo.offset == 24
    assert winput.KEYBDINPUT.dwExtraInfo.offset == 16
    event = winput.INPUT(type=winput.INPUT_KEYBOARD)
    # INPUT 的 type 之后对齐到 8 字节，联合体从偏移 8 开始
    assert ctypes.addressof(event.ki) - ctypes.addressof(event) == 8
    assert ctypes.addressof(event.mi) - ctypes.addressof(event) == 8
    # dwExtraInfo 必须是 8 字节 ULONG_PTR，否则 INPUT 会只有 36/32 字节
    assert ctypes.sizeof(winput.ULONG_PTR) == 8
    assert winput.ULONG_PTR is ctypes.c_size_t
    for struct in (winput.MOUSEINPUT, winput.KEYBDINPUT):
        field_types = dict(struct._fields_)
        assert field_types["dwExtraInfo"] is winput.ULONG_PTR


# ---------------------------------------------------------------------------
# 绝对坐标归一化
# ---------------------------------------------------------------------------


def test_normalize_absolute_formula() -> None:
    rect = Rect(0, 0, 2560, 1600)
    assert winput.normalize_absolute(0, 0, rect) == (0, 0)
    assert winput.normalize_absolute(2559, 1599, rect) == (65535, 65535)
    # round(1280*65535/2559)=32780，round(800*65535/1599)=32788
    assert winput.normalize_absolute(1280, 800, rect) == (32780, 32788)


def test_normalize_absolute_negative_virtual_screen() -> None:
    """多显示器：虚拟屏幕左上角为负，归一化从虚拟屏幕左上角开始（Lead 评审第 3 条）。"""
    rect = Rect(-1920, 0, 3840, 1080)
    assert winput.normalize_absolute(-1920, 0, rect) == (0, 0)
    assert winput.normalize_absolute(1919, 1079, rect) == (65535, 65535)
    assert winput.normalize_absolute(0, 0, rect) == (32776, 0)
    # 越界坐标钳位
    assert winput.normalize_absolute(-5000, -5000, rect) == (0, 0)
    assert winput.normalize_absolute(9999, 9999, rect) == (65535, 65535)


def test_normalize_absolute_small_screen_rounding() -> None:
    rect = Rect(0, 0, 3, 3)
    assert winput.normalize_absolute(0, 0, rect) == (0, 0)
    assert winput.normalize_absolute(1, 1, rect) == (32768, 32768)
    assert winput.normalize_absolute(2, 2, rect) == (65535, 65535)


def test_normalize_absolute_rejects_degenerate_rect() -> None:
    with pytest.raises(ValueError, match="虚拟屏幕矩形过小"):
        winput.normalize_absolute(0, 0, Rect(0, 0, 1, 1))


# ---------------------------------------------------------------------------
# 按键表
# ---------------------------------------------------------------------------


def test_key_table_covers_profile_keys(project_root) -> None:
    """keys.yaml 里出现的所有按键名都必须能解析（详细设计 3.4）。"""
    data = yaml.safe_load((project_root / "profiles" / "cn360" / "keys.yaml").read_text(encoding="utf-8"))
    names: set[str] = set(data["actions"].values())
    names.update(str(slot["key"]) for slot in data["consumables"])
    for name in names:
        assert winput.is_supported_key(name), name
        assert winput.resolve_key(name).name


def test_key_table_known_values() -> None:
    escape = winput.resolve_key("escape")
    assert (escape.vk, escape.scan, escape.extended, escape.is_mouse) == (0x1B, 0x01, False, False)

    f11 = winput.resolve_key("f11")
    assert (f11.vk, f11.scan) == (0x7A, 0x57)
    f12 = winput.resolve_key("f12")
    assert (f12.vk, f12.scan) == (0x7B, 0x58)

    letter_m = winput.resolve_key("m")
    assert (letter_m.vk, letter_m.scan) == (0x4D, 0x32)

    digit_1 = winput.resolve_key("1")
    assert (digit_1.vk, digit_1.scan) == (0x31, 0x02)
    digit_0 = winput.resolve_key("0")
    assert (digit_0.vk, digit_0.scan) == (0x30, 0x0B)

    left = winput.resolve_key("left")
    assert left.extended is True and left.scan == 0x4B

    mouse_left = winput.resolve_key("mouse_left")
    assert mouse_left.is_mouse is True
    assert mouse_left.mouse_button == "left"


def test_key_table_aliases_and_case() -> None:
    assert winput.resolve_key("ESC") is winput.resolve_key("escape")
    assert winput.resolve_key("mouse-left") is winput.resolve_key("mouse_left")
    assert winput.resolve_key(" Return ") is winput.resolve_key("enter")
    assert winput.resolve_key("page_up") is winput.resolve_key("pageup")


def test_key_table_unknown_key_raises() -> None:
    with pytest.raises(KeyError, match="不支持的按键名"):
        winput.resolve_key("definitely_not_a_key")
    assert winput.is_supported_key("definitely_not_a_key") is False


def test_key_table_canonical_names_are_self_consistent() -> None:
    for name, binding in winput.KEY_BINDINGS.items():
        assert binding.name in winput.KEY_BINDINGS
        assert winput.KEY_BINDINGS[binding.name] is binding
        assert name  # 表非空键
    for required in ("f11", "f12", "escape", "mouse_left", "m", "1", "5"):
        assert required in winput.SUPPORTED_KEY_NAMES


@pytest.mark.skipif(sys.platform != "win32", reason="MapVirtualKeyW 只在 Windows 可用")
def test_map_vk_to_scan_uses_mapvirtualkey() -> None:
    assert winput.map_vk_to_scan(0x4D) == 0x32  # m
    assert winput.map_vk_to_scan(0x7B) == 0x58  # f12
    assert winput.map_vk_to_scan(0x1B) == 0x01  # escape


# ---------------------------------------------------------------------------
# 事件构造纯函数
# ---------------------------------------------------------------------------


def test_build_key_event_prefers_scan_code() -> None:
    event = winput.build_key_event(winput.resolve_key("m"), down=True, scan_code=0x32)
    assert event.type == winput.INPUT_KEYBOARD
    assert event.ki.wVk == 0
    assert event.ki.wScan == 0x32
    assert event.ki.dwFlags == winput.KEYEVENTF_SCANCODE

    event_up = winput.build_key_event(winput.resolve_key("m"), down=False, scan_code=0x32)
    assert event_up.ki.dwFlags == winput.KEYEVENTF_SCANCODE | winput.KEYEVENTF_KEYUP


def test_build_key_event_extended_flag() -> None:
    event = winput.build_key_event(winput.resolve_key("left"), down=True, scan_code=0x4B)
    assert event.ki.dwFlags == winput.KEYEVENTF_SCANCODE | winput.KEYEVENTF_EXTENDEDKEY


def test_build_key_event_uses_table_scan_fallback() -> None:
    event = winput.build_key_event(winput.resolve_key("f12"), down=True)
    assert event.ki.wScan == 0x58


def test_build_key_event_rejects_invalid_scan() -> None:
    with pytest.raises(ValueError, match="扫描码超出范围"):
        winput.build_key_event(winput.resolve_key("m"), down=True, scan_code=0)


def test_build_mouse_button_event() -> None:
    left_down = winput.build_mouse_button_event("left", down=True)
    assert left_down.type == winput.INPUT_MOUSE
    assert left_down.mi.dwFlags == winput.MOUSEEVENTF_LEFTDOWN
    right_up = winput.build_mouse_button_event("right", down=False)
    assert right_up.mi.dwFlags == winput.MOUSEEVENTF_RIGHTUP
    with pytest.raises(ValueError, match="不支持的鼠标按键"):
        winput.build_mouse_button_event("thumb", down=True)


def test_build_mouse_absolute_event() -> None:
    event = winput.build_mouse_absolute_event(1234, 5678)
    assert event.type == winput.INPUT_MOUSE
    assert event.mi.dx == 1234
    assert event.mi.dy == 5678
    assert event.mi.dwFlags == (
        winput.MOUSEEVENTF_MOVE | winput.MOUSEEVENTF_ABSOLUTE | winput.MOUSEEVENTF_VIRTUALDESK
    )


def test_build_mouse_relative_event_keeps_mickey() -> None:
    event = winput.build_mouse_relative_event(1200, -300)
    assert event.mi.dx == 1200
    assert event.mi.dy == -300
    assert event.mi.dwFlags == winput.MOUSEEVENTF_MOVE


def test_build_mouse_wheel_event() -> None:
    up = winput.build_mouse_wheel_event(3)
    assert up.mi.mouseData == 3 * winput.WHEEL_DELTA
    assert up.mi.dwFlags == winput.MOUSEEVENTF_WHEEL

    down = winput.build_mouse_wheel_event(-2)
    assert down.mi.mouseData == 0xFFFFFF10
    assert winput.wheel_mouse_data(-1) == 0xFFFFFF88
    assert winput.wheel_mouse_data(1) == 120


# ---------------------------------------------------------------------------
# SendInputBackend：键盘 / 鼠标 / 等待
# ---------------------------------------------------------------------------


def test_backend_key_down_up_uses_scan_code(fake_send_input, clock) -> None:
    resolved_vks: list[int] = []

    def resolver(vk: int) -> int:
        resolved_vks.append(vk)
        return 0  # 回退内置扫描码表

    backend = winput.SendInputBackend(
        send_input=fake_send_input,
        virtual_screen_provider=lambda: FAKE_VIRTUAL_RECT,
        sleeper=FakeSleeper(clock),
        rng=random.Random(0),
        scan_code_resolver=resolver,
    )
    backend.send([Action.key_down("m")])
    backend.send([Action.key_up("m")])

    assert resolved_vks == [0x4D, 0x4D]
    keys = fake_send_input.key_events()
    assert len(keys) == 2
    assert keys[0].wScan == 0x32
    assert keys[0].dwFlags == winput.KEYEVENTF_SCANCODE
    assert keys[1].dwFlags == winput.KEYEVENTF_SCANCODE | winput.KEYEVENTF_KEYUP
    assert backend.pressed_keys == frozenset()


def test_backend_mouse_keys_from_keys_yaml(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    backend.send([Action.key_down("mouse_left"), Action.key_up("mouse_left")])
    mouse = fake_send_input.mouse_events()
    assert [m.dwFlags for m in mouse] == [winput.MOUSEEVENTF_LEFTDOWN, winput.MOUSEEVENTF_LEFTUP]
    assert backend.pressed_buttons == frozenset()


def test_backend_click_abs_moves_then_clicks(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    backend.send([Action.click_abs(960, 540, label="nav_click")])

    mouse = fake_send_input.mouse_events()
    assert len(mouse) == 3
    assert mouse[0].dwFlags == (
        winput.MOUSEEVENTF_MOVE | winput.MOUSEEVENTF_ABSOLUTE | winput.MOUSEEVENTF_VIRTUALDESK
    )
    # Rect(0,0,1920,1080)：round(960*65535/1919)=32785，round(540*65535/1079)=32798
    assert (mouse[0].dx, mouse[0].dy) == (32785, 32798)
    assert mouse[1].dwFlags == winput.MOUSEEVENTF_LEFTDOWN
    assert mouse[2].dwFlags == winput.MOUSEEVENTF_LEFTUP
    # 按下到释放 30～80ms，使用注入的 Sleeper
    assert 0.030 <= clock.now() <= 0.080
    assert backend.pressed_buttons == frozenset()


def test_backend_move_abs_only_moves_without_click(fake_send_input, clock) -> None:
    """MOVE_ABS 只做绝对移动：不按下/不释放、不等待 click_hold_ms。"""
    backend = make_sendinput_backend(fake_send_input, clock)
    action = Action.move_abs(960, 540, label="ship_list_area")
    assert action.priority == 10
    backend.send([action])

    mouse = fake_send_input.mouse_events()
    assert len(mouse) == 1, "MOVE_ABS 只能产生一个绝对移动事件，不能有 down/up"
    assert mouse[0].dwFlags == (
        winput.MOUSEEVENTF_MOVE | winput.MOUSEEVENTF_ABSOLUTE | winput.MOUSEEVENTF_VIRTUALDESK
    )
    # Rect(0,0,1920,1080)：round(960*65535/1919)=32785，round(540*65535/1079)=32798
    assert (mouse[0].dx, mouse[0].dy) == (32785, 32798)
    assert fake_send_input.key_events() == []
    assert backend.pressed_buttons == frozenset()
    assert clock.now() == 0.0, "MOVE_ABS 不等待 click_hold_ms"


def test_backend_move_abs_then_wheel_for_ship_list_scroll(fake_send_input, clock) -> None:
    """ShipChecker 口径：先把光标移到列表区域，再发滚轮（不点击列表）。"""
    backend = make_sendinput_backend(fake_send_input, clock)
    backend.send(
        [
            Action.move_abs(960, 540, label="ship_list_area"),
            Action.wheel(-3, label="ship_list_scroll"),
        ]
    )

    mouse = fake_send_input.mouse_events()
    assert [m.dwFlags for m in mouse] == [
        winput.MOUSEEVENTF_MOVE | winput.MOUSEEVENTF_ABSOLUTE | winput.MOUSEEVENTF_VIRTUALDESK,
        winput.MOUSEEVENTF_WHEEL,
    ]
    assert mouse[1].mouseData == winput.wheel_mouse_data(-3)
    assert clock.now() == 0.0


def test_backend_move_abs_requires_coordinates(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    with pytest.raises(ValueError, match="缺少 x/y"):
        backend.send([Action(ActionKind.MOVE_ABS, x=10)])


def test_backend_move_rel_is_mickey_not_normalized(fake_send_input, clock) -> None:
    """MOVE_REL 的 dx/dy 是鼠标计数，不能按虚拟屏幕归一化（Lead 评审第 3 条）。"""
    backend = make_sendinput_backend(fake_send_input, clock, virtual_rect=Rect(0, 0, 1920, 1080))
    backend.send([Action.move_rel(1200, -300)])
    mouse = fake_send_input.mouse_events()
    assert len(mouse) == 1
    assert (mouse[0].dx, mouse[0].dy) == (1200, -300)
    assert mouse[0].dwFlags == winput.MOUSEEVENTF_MOVE


def test_backend_wheel_action(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    backend.send([Action.wheel(2)])
    backend.send([Action.wheel(-1)])
    backend.send([Action.wheel(0)])  # 0 格不发事件
    mouse = fake_send_input.mouse_events()
    assert [m.dwFlags for m in mouse] == [winput.MOUSEEVENTF_WHEEL, winput.MOUSEEVENTF_WHEEL]
    assert [m.mouseData for m in mouse] == [240, 0xFFFFFF88]


def test_backend_wait_uses_injected_sleeper(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    backend.send([Action.wait(250)])
    assert fake_send_input.events == []
    assert clock.now() == pytest.approx(0.25)


def test_backend_release_all_is_idempotent(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    backend.send([Action.key_down("m"), Action.mouse_down()])
    assert backend.pressed_keys == frozenset({"m"})
    assert backend.pressed_buttons == frozenset({"left"})

    fake_send_input.clear()
    backend.release_all()
    events = fake_send_input.events
    assert len(events) == 2
    assert events[0].type == winput.INPUT_KEYBOARD
    assert events[0].ki.dwFlags & winput.KEYEVENTF_KEYUP
    assert events[1].type == winput.INPUT_MOUSE
    assert events[1].mi.dwFlags == winput.MOUSEEVENTF_LEFTUP
    assert backend.pressed_keys == frozenset()
    assert backend.pressed_buttons == frozenset()

    fake_send_input.clear()
    backend.release_all()
    backend.release_all()
    assert fake_send_input.events == [], "release_all 必须幂等，未按下的键不再发送事件"


def test_backend_release_all_with_nothing_pressed(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    backend.release_all()
    assert fake_send_input.events == []


def test_backend_partial_send_failure_raises(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    fake_send_input.return_override = 0
    with pytest.raises(InputUnavailable, match="只接受"):
        backend.send([Action.key_down("m")])
    assert backend.consecutive_failures == 1
    # 发送失败的动作不能记为已按下
    assert backend.pressed_keys == frozenset()

    fake_send_input.return_override = None
    backend.send([Action.key_up("m")])
    assert backend.consecutive_failures == 0


def test_backend_win32_exception_wrapped(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    fake_send_input.raise_exc = OSError("SendInput 不可用")
    with pytest.raises(InputUnavailable, match="SendInput 调用失败"):
        backend.send([Action.move_rel(1, 1)])
    assert backend.consecutive_failures == 1


def test_backend_validates_action_payload(fake_send_input, clock) -> None:
    backend = make_sendinput_backend(fake_send_input, clock)
    with pytest.raises(ValueError, match="缺少 x/y"):
        backend.send([Action(ActionKind.CLICK_ABS, x=10)])
    with pytest.raises(KeyError, match="不支持的按键名"):
        backend.send([Action(ActionKind.KEY_DOWN, key="no_such_key")])
    with pytest.raises(ValueError, match="缺少 key"):
        backend.send([Action(ActionKind.KEY_DOWN)])


# ---------------------------------------------------------------------------
# SetCursorPos 备选后端
# ---------------------------------------------------------------------------


class FakeCursor:
    """SetCursorPos / GetCursorPos / mouse_event 的假实现。"""

    def __init__(self, start: tuple[int, int] = (0, 0)) -> None:
        self.position = start
        self.calls: list[tuple] = []
        self.set_result = True

    def set(self, x: int, y: int) -> bool:
        self.calls.append(("set", x, y))
        if self.set_result:
            self.position = (x, y)
        return self.set_result

    def get(self) -> tuple[int, int]:
        return self.position

    def mouse(self, flags: int, dx: int, dy: int, data: int) -> None:
        self.calls.append(("mouse", flags, dx, dy, data))


def make_setcursor_backend(fake_send_input, clock, cursor: FakeCursor) -> winput.SetCursorPosBackend:
    return winput.SetCursorPosBackend(
        send_input=fake_send_input,
        set_cursor_pos=cursor.set,
        get_cursor_pos=cursor.get,
        mouse_event=cursor.mouse,
        sleeper=FakeSleeper(clock),
        rng=random.Random(1),
        scan_code_resolver=lambda _vk: 0,
    )


def test_setcursor_backend_absolute_click(fake_send_input, clock) -> None:
    cursor = FakeCursor(start=(5, 5))
    backend = make_setcursor_backend(fake_send_input, clock, cursor)
    backend.send([Action.click_abs(300, 400)])

    assert cursor.calls[0] == ("set", 300, 400)
    assert cursor.calls[1] == ("mouse", winput.MOUSEEVENTF_LEFTDOWN, 0, 0, 0)
    assert cursor.calls[2] == ("mouse", winput.MOUSEEVENTF_LEFTUP, 0, 0, 0)
    assert fake_send_input.events == [], "绝对点击走 SetCursorPos，不经过 SendInput 鼠标事件"
    assert 0.030 <= clock.now() <= 0.080
    assert backend.pressed_buttons == frozenset()


def test_setcursor_backend_move_abs_uses_set_cursor_pos(fake_send_input, clock) -> None:
    """SetCursorPos 后端的 MOVE_ABS 用 SetCursorPos 定位，不发鼠标按键/滚轮事件。"""
    cursor = FakeCursor(start=(5, 5))
    backend = make_setcursor_backend(fake_send_input, clock, cursor)
    backend.send([Action.move_abs(300, 400)])

    assert cursor.calls == [("set", 300, 400)]
    assert fake_send_input.events == []
    assert backend.pressed_buttons == frozenset()
    assert clock.now() == 0.0


def test_setcursor_backend_relative_move_and_wheel(fake_send_input, clock) -> None:
    cursor = FakeCursor(start=(10, 20))
    backend = make_setcursor_backend(fake_send_input, clock, cursor)
    backend.send([Action.move_rel(5, -3)])
    backend.send([Action.wheel(1)])
    assert cursor.calls[0] == ("mouse", winput.MOUSEEVENTF_MOVE, 5, -3, 0)
    assert cursor.calls[1] == ("mouse", winput.MOUSEEVENTF_WHEEL, 0, 0, 120)


def test_setcursor_backend_keyboard_uses_sendinput(fake_send_input, clock) -> None:
    cursor = FakeCursor()
    backend = make_setcursor_backend(fake_send_input, clock, cursor)
    backend.send([Action.key_down("m"), Action.key_up("m")])
    keys = fake_send_input.key_events()
    assert [k.wScan for k in keys] == [0x32, 0x32]
    assert keys[1].dwFlags & winput.KEYEVENTF_KEYUP


def test_setcursor_backend_set_cursor_failure(fake_send_input, clock) -> None:
    cursor = FakeCursor()
    cursor.set_result = False
    backend = make_setcursor_backend(fake_send_input, clock, cursor)
    with pytest.raises(InputUnavailable, match="SetCursorPos 失败"):
        backend.send([Action.click_abs(10, 20)])


def test_setcursor_backend_release_all_idempotent(fake_send_input, clock) -> None:
    cursor = FakeCursor()
    backend = make_setcursor_backend(fake_send_input, clock, cursor)
    backend.send([Action.key_down("m"), Action.mouse_down()])
    cursor.calls.clear()
    fake_send_input.clear()

    backend.release_all()
    assert fake_send_input.key_events()[0].dwFlags & winput.KEYEVENTF_KEYUP
    assert cursor.calls == [("mouse", winput.MOUSEEVENTF_LEFTUP, 0, 0, 0)]

    cursor.calls.clear()
    fake_send_input.clear()
    backend.release_all()
    assert cursor.calls == []
    assert fake_send_input.events == []


# ---------------------------------------------------------------------------
# 后端工厂
# ---------------------------------------------------------------------------


def test_make_backend_selects_sendinput(fake_send_input) -> None:
    backend = winput.make_backend(
        send_input=fake_send_input,
        virtual_screen_provider=lambda: FAKE_VIRTUAL_RECT,
        sleeper=FakeSleeper(FakeClock()),
        rng=random.Random(0),
    )
    assert isinstance(backend, winput.SendInputBackend)
    assert isinstance(backend, winput.InputBackend)


def test_make_backend_selects_setcursor(fake_send_input) -> None:
    backend = winput.make_backend(
        backend="setcursor",
        send_input=fake_send_input,
        set_cursor_pos=lambda x, y: True,
        get_cursor_pos=lambda: (0, 0),
        mouse_event=lambda *args: None,
        sleeper=FakeSleeper(FakeClock()),
        rng=random.Random(0),
    )
    assert isinstance(backend, winput.SetCursorPosBackend)


def test_make_backend_uses_config_backend() -> None:
    from src.config.models import InputConfig

    backend = winput.make_backend(
        InputConfig(backend="setcursor"),
        set_cursor_pos=lambda x, y: True,
        mouse_event=lambda *args: None,
    )
    assert isinstance(backend, winput.SetCursorPosBackend)


def test_make_backend_rejects_unknown() -> None:
    with pytest.raises(InputUnavailable, match="未知输入后端"):
        winput.make_backend(backend="magic")
