"""SendInput 输入后端（详细设计 9.1 / 9.2 / 9.6，评审补充第 3 节第 4 项）。

本模块把公共动作类型 :class:`src.common.types.Action` 展开为 Win32 键鼠事件：

- 绝对点击 ``CLICK_ABS``：屏幕坐标按虚拟桌面归一化到 0～65535，
  标志位 ``MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK``；
  内部固定顺序为“移动光标 → 左键按下 → 保持 30～80ms → 左键释放”；
- 绝对移动 ``MOVE_ABS``：同样按虚拟桌面归一化，只移动光标，不按下/不释放、
  不等待保持时间（船只列表滚动前把光标移到列表区域，再发 ``MOUSE_WHEEL``）；
- 相对移动 ``MOVE_REL``：``dx/dy`` 是 SendInput 的鼠标计数（mickey），不是屏幕像素，
  不做 DPI 或虚拟屏幕归一化，只设置 ``MOUSEEVENTF_MOVE``；
- 滚轮 ``MOUSE_WHEEL``：``mouseData = wheel_delta × WHEEL_DELTA(120)``，
  标志位 ``MOUSEEVENTF_WHEEL``（正数向上）；
- 按键：优先使用扫描码（``KEYEVENTF_SCANCODE``），扫描码由
  ``user32.MapVirtualKeyW(MAPVK_VK_TO_VSC)`` 取得，失败时回退到内置扫描码表；
  方向键等扩展键补 ``KEYEVENTF_EXTENDEDKEY``；
- 两套后端：默认 ``SendInputBackend``，备选 ``SetCursorPosBackend``
  （详细设计 9.6，配置 ``input.backend=setcursor``，用 SetCursorPos 定位光标）；
- ``make_backend(InputConfig)`` 按配置创建后端。

x64 下结构体布局（评审补充第 3 节第 4 项）::

    sizeof(MOUSEINPUT) == 32，sizeof(KEYBDINPUT) == 24，sizeof(INPUT) == 40
    dwExtraInfo 使用 ULONG_PTR（ctypes.c_size_t），不能是 c_uint。

单元测试只验证结构体布局、归一化公式、键表和展开后的事件参数：
``send_input`` / ``set_cursor_pos`` / ``mouse_event`` 等 Win32 调用全部通过构造参数注入，
测试中绝不真正发送键鼠事件（详细设计 14.1）。``release_all`` 在正常运行中只由输入线程
调用（见 :mod:`src.execution.queue`），其它线程不要直接调用本模块的后端。
"""

from __future__ import annotations

import ctypes
import logging
import random
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from ..common.clock import Sleeper, SystemSleeper
from ..common.errors import InputUnavailable
from ..common.types import Action, ActionKind, Rect
from ..common.window import virtual_screen_rect
from ..config.models import InputConfig

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# SendInput 常量（winuser.h）
# ---------------------------------------------------------------------------

INPUT_MOUSE = 0x0000
INPUT_KEYBOARD = 0x0001

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008

MAPVK_VK_TO_VSC = 0
WHEEL_DELTA = 120

#: SendInput 绝对坐标归一化上限（0～65535）
NORMALIZE_MAX = 65535

#: 鼠标按键名 -> (按下标志, 释放标志)
MOUSE_BUTTON_FLAGS: Mapping[str, tuple[int, int]] = MappingProxyType(
    {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }
)

# ---------------------------------------------------------------------------
# Win32 结构体（x64 布局见模块 docstring）
# ---------------------------------------------------------------------------

#: ULONG_PTR：x64 为 8 字节；不能用 c_uint，否则 INPUT 会短 4 字节
ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    """SendInput 鼠标事件结构体（winuser.h ``MOUSEINPUT``）。"""

    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class KEYBDINPUT(ctypes.Structure):
    """SendInput 键盘事件结构体（winuser.h ``KEYBDINPUT``）。"""

    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    """SendInput 硬件事件结构体（winuser.h ``HARDWAREINPUT``），仅用于补齐联合体。"""

    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]


class INPUT(ctypes.Structure):
    """SendInput 事件结构体（winuser.h ``INPUT``）；``u`` 为匿名联合体。"""

    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUTUNION),
    ]


# 类型别名放在结构体定义之后，避免前向引用问题
SendInputFn = Callable[[Sequence[INPUT]], int]
VirtualScreenProvider = Callable[[], Rect]
ScanCodeResolver = Callable[[int], int]
SetCursorPosFn = Callable[[int, int], bool]
GetCursorPosFn = Callable[[], tuple[int, int]]
MouseEventFn = Callable[[int, int, int, int], None]

# ---------------------------------------------------------------------------
# Win32 函数原型：SendInput 必须声明 argtypes/restype（评审补充第 3 节第 4 项）
# ---------------------------------------------------------------------------

if _IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    _user32.SendInput.restype = wintypes.UINT
    _user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
    _user32.MapVirtualKeyW.restype = wintypes.UINT
    _user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _user32.SetCursorPos.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.mouse_event.argtypes = [
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.DWORD,
        ULONG_PTR,
    ]
    _user32.mouse_event.restype = None
else:  # pragma: no cover - 项目只支持 Windows
    _user32 = None


def _require_user32() -> Any:
    if _user32 is None:
        raise InputUnavailable("当前平台不支持 Win32 输入接口（项目只支持 Windows）")
    return _user32


def _default_send_input(events: Sequence[INPUT]) -> int:
    """调用 ``user32.SendInput``，返回被系统接受的事件数。"""
    user32 = _require_user32()
    if not events:
        return 0
    array = (INPUT * len(events))(*events)
    accepted = user32.SendInput(len(events), array, ctypes.sizeof(INPUT))
    return int(accepted)


def map_vk_to_scan(vk: int) -> int:
    """用 ``MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)`` 把虚拟键码换成扫描码。

    不可用或查询失败时返回 0，调用方回退到内置扫描码表。
    """
    if not _IS_WINDOWS or _user32 is None:  # pragma: no cover - 测试环境是 Windows
        return 0
    try:
        return int(_user32.MapVirtualKeyW(int(vk), MAPVK_VK_TO_VSC))
    except (OSError, ValueError):  # pragma: no cover - Win32 异常非常罕见
        return 0


def _default_set_cursor_pos(x: int, y: int) -> bool:
    user32 = _require_user32()
    return bool(user32.SetCursorPos(int(x), int(y)))


def _default_get_cursor_pos() -> tuple[int, int]:
    user32 = _require_user32()
    point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(point)):
        raise InputUnavailable("GetCursorPos 调用失败")
    return int(point.x), int(point.y)


def _default_mouse_event(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
    user32 = _require_user32()
    user32.mouse_event(int(flags), int(dx), int(dy), int(data) & 0xFFFFFFFF, 0)


# ---------------------------------------------------------------------------
# 绝对坐标归一化（详细设计 9.2 公式）
# ---------------------------------------------------------------------------


def _normalize_axis(value: int, origin: int, size: int) -> int:
    normalized = int(round((value - origin) * NORMALIZE_MAX / (size - 1)))
    return max(0, min(NORMALIZE_MAX, normalized))


def normalize_absolute(x: int, y: int, virtual_rect: Rect) -> tuple[int, int]:
    """把虚拟桌面物理像素坐标归一化到 0～65535。

    公式（详细设计 9.2）::

        dx = round((x - vs_left) * 65535 / (vs_width - 1))
        dy = round((y - vs_top) * 65535 / (vs_height - 1))

    多显示器时从虚拟屏幕左上角（可能为负）开始换算；越界坐标钳位到 0～65535。
    """
    if virtual_rect.w <= 1 or virtual_rect.h <= 1:
        raise ValueError(f"虚拟屏幕矩形过小，无法归一化: {virtual_rect!r}")
    return (
        _normalize_axis(int(x), virtual_rect.x, virtual_rect.w),
        _normalize_axis(int(y), virtual_rect.y, virtual_rect.h),
    )


# ---------------------------------------------------------------------------
# 按键表：可读名称 -> 虚拟键码 / 内置扫描码 / 扩展键 / 鼠标按键
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KeyBinding:
    """一个可读按键名的解析结果。"""

    name: str
    vk: int
    scan: int
    extended: bool = False
    mouse_button: str | None = None

    @property
    def is_mouse(self) -> bool:
        return self.mouse_button is not None


def _build_key_bindings() -> dict[str, KeyBinding]:
    bindings: dict[str, KeyBinding] = {}

    def _add(
        name: str,
        vk: int,
        scan: int,
        *,
        extended: bool = False,
        mouse_button: str | None = None,
        aliases: tuple[str, ...] = (),
    ) -> None:
        binding = KeyBinding(name, vk, scan, extended, mouse_button)
        bindings[name] = binding
        for alias in aliases:
            bindings[alias] = binding

    # 字母：虚拟键码 = ASCII 大写；扫描码为 Set 1 标准值
    letters = {
        "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14, "y": 0x15,
        "u": 0x16, "i": 0x17, "o": 0x18, "p": 0x19, "a": 0x1E, "s": 0x1F,
        "d": 0x20, "f": 0x21, "g": 0x22, "h": 0x23, "j": 0x24, "k": 0x25,
        "l": 0x26, "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F, "b": 0x30,
        "n": 0x31, "m": 0x32,
    }
    for letter, scan in letters.items():
        _add(letter, ord(letter.upper()), scan)

    # 数字主键区
    digits = {"1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06,
              "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A, "0": 0x0B}
    for digit, scan in digits.items():
        _add(digit, ord(digit), scan)

    # 功能键
    function_keys = {
        "f1": (0x70, 0x3B), "f2": (0x71, 0x3C), "f3": (0x72, 0x3D),
        "f4": (0x73, 0x3E), "f5": (0x74, 0x3F), "f6": (0x75, 0x40),
        "f7": (0x76, 0x41), "f8": (0x77, 0x42), "f9": (0x78, 0x43),
        "f10": (0x79, 0x44), "f11": (0x7A, 0x57), "f12": (0x7B, 0x58),
    }
    for name, (vk, scan) in function_keys.items():
        _add(name, vk, scan)

    # 常用控制键 / 编辑键 / 标点键
    special = [
        ("escape", 0x1B, 0x01, False, ("esc",)),
        ("space", 0x20, 0x39, False, ("spacebar",)),
        ("enter", 0x0D, 0x1C, False, ("return", "numpad_enter")),
        ("tab", 0x09, 0x0F, False, ()),
        ("backspace", 0x08, 0x0E, False, ("back",)),
        ("capslock", 0x14, 0x3A, False, ("caps_lock",)),
        ("shift", 0x10, 0x2A, False, ()),
        ("ctrl", 0x11, 0x1D, False, ("control",)),
        ("alt", 0x12, 0x38, False, ()),
        ("insert", 0x2D, 0x52, True, ("ins",)),
        ("delete", 0x2E, 0x53, True, ("del",)),
        ("home", 0x24, 0x47, True, ()),
        ("end", 0x23, 0x4F, True, ()),
        ("pageup", 0x21, 0x49, True, ("page_up", "prior")),
        ("pagedown", 0x22, 0x51, True, ("page_down", "next")),
        ("left", 0x25, 0x4B, True, ("arrow_left", "left_arrow")),
        ("up", 0x26, 0x48, True, ("arrow_up", "up_arrow")),
        ("right", 0x27, 0x4D, True, ("arrow_right", "right_arrow")),
        ("down", 0x28, 0x50, True, ("arrow_down", "down_arrow")),
        ("numlock", 0x90, 0x45, True, ("num_lock",)),
        ("scrolllock", 0x91, 0x46, False, ("scroll_lock",)),
        ("printscreen", 0x2C, 0x37, True, ("print_screen", "prtsc")),
        ("minus", 0xBD, 0x0C, False, ("-", "hyphen")),
        ("equal", 0xBB, 0x0D, False, ("=", "plus")),
        ("comma", 0xBC, 0x33, False, (",",)),
        ("period", 0xBE, 0x34, False, (".", "dot")),
        ("slash", 0xBF, 0x35, False, ("/",)),
        ("semicolon", 0xBA, 0x27, False, (";",)),
        ("apostrophe", 0xDE, 0x28, False, ("'", "quote")),
        ("leftbracket", 0xDB, 0x1A, False, ("[",)),
        ("rightbracket", 0xDD, 0x1B, False, ("]",)),
        ("backslash", 0xDC, 0x2B, False, ("\\",)),
        ("grave", 0xC0, 0x29, False, ("`", "tilde")),
    ]
    for name, vk, scan, extended, aliases in special:
        _add(name, vk, scan, extended=extended, aliases=aliases)

    # 鼠标按键：可写进 keys.yaml（例如 fire: mouse_left）
    _add("mouse_left", 0x01, 0, mouse_button="left", aliases=("lbutton", "mouse1"))
    _add("mouse_right", 0x02, 0, mouse_button="right", aliases=("rbutton", "mouse2"))
    _add("mouse_middle", 0x04, 0, mouse_button="middle", aliases=("mbutton", "mouse3"))
    return bindings


#: 可读按键名（含别名）-> 解析结果；只读映射
KEY_BINDINGS: Mapping[str, KeyBinding] = MappingProxyType(_build_key_bindings())

#: 支持的全部按键名（含别名），供配置校验使用
SUPPORTED_KEY_NAMES: frozenset[str] = frozenset(KEY_BINDINGS)


def resolve_key(name: str) -> KeyBinding:
    """把可读按键名解析为 :class:`KeyBinding`；不支持时抛 ``KeyError``。"""
    text = str(name or "").strip().lower()
    binding = KEY_BINDINGS.get(text)
    if binding is None:
        alt = text.replace(" ", "_").replace("-", "_")
        binding = KEY_BINDINGS.get(alt)
    if binding is None:
        raise KeyError(f"不支持的按键名: {name!r}（见 SUPPORTED_KEY_NAMES）")
    return binding


def is_supported_key(name: str) -> bool:
    """按键名是否在支持列表中（配置校验用，不抛异常）。"""
    try:
        resolve_key(name)
    except KeyError:
        return False
    return True


# ---------------------------------------------------------------------------
# 事件构造纯函数（单元测试直接验证参数）
# ---------------------------------------------------------------------------


def build_key_event(binding: KeyBinding, *, down: bool, scan_code: int | None = None) -> INPUT:
    """构造键盘事件：优先扫描码，扩展键补 ``KEYEVENTF_EXTENDEDKEY``，释放补 ``KEYUP``。"""
    scan = binding.scan if scan_code is None else int(scan_code)
    if not 0 < scan < 0x100:
        raise ValueError(f"扫描码超出范围: {scan!r}（按键 {binding.name}）")
    flags = KEYEVENTF_SCANCODE
    if binding.extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    if not down:
        flags |= KEYEVENTF_KEYUP
    event = INPUT(type=INPUT_KEYBOARD)
    event.ki = KEYBDINPUT(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
    return event


def build_mouse_button_event(button: str, *, down: bool) -> INPUT:
    """构造鼠标按键事件（左/右/中键）。"""
    flags = MOUSE_BUTTON_FLAGS.get(button)
    if flags is None:
        raise ValueError(f"不支持的鼠标按键: {button!r}")
    down_flag, up_flag = flags
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(
        dx=0,
        dy=0,
        mouseData=0,
        dwFlags=down_flag if down else up_flag,
        time=0,
        dwExtraInfo=0,
    )
    return event


def build_mouse_absolute_event(dx: int, dy: int) -> INPUT:
    """构造绝对鼠标移动事件；``dx/dy`` 必须是已归一化的 0～65535。"""
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(
        dx=int(dx),
        dy=int(dy),
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
        time=0,
        dwExtraInfo=0,
    )
    return event


def build_mouse_relative_event(dx: int, dy: int) -> INPUT:
    """构造相对鼠标移动事件；``dx/dy`` 为鼠标计数（mickey），不做归一化。"""
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(
        dx=int(dx),
        dy=int(dy),
        mouseData=0,
        dwFlags=MOUSEEVENTF_MOVE,
        time=0,
        dwExtraInfo=0,
    )
    return event


def wheel_mouse_data(delta: int) -> int:
    """滚轮格数换算成 ``mouseData``（每格 ``WHEEL_DELTA``，负数按 DWORD 回绕）。"""
    return (int(delta) * WHEEL_DELTA) & 0xFFFFFFFF


def build_mouse_wheel_event(delta: int) -> INPUT:
    """构造滚轮事件：``mouseData = delta × 120``，正数向上（详细设计 9.1）。"""
    event = INPUT(type=INPUT_MOUSE)
    event.mi = MOUSEINPUT(
        dx=0,
        dy=0,
        mouseData=wheel_mouse_data(delta),
        dwFlags=MOUSEEVENTF_WHEEL,
        time=0,
        dwExtraInfo=0,
    )
    return event


# ---------------------------------------------------------------------------
# 后端协议
# ---------------------------------------------------------------------------


@runtime_checkable
class InputBackend(Protocol):
    """输入后端协议（详细设计 9.6）：上层只依赖这两个方法。"""

    def send(self, actions: Sequence[Action]) -> None:
        """按顺序执行一组动作；实现需保证动作序列不被其它调用打断。"""

    def release_all(self) -> None:
        """释放所有按下状态的键和鼠标按钮；幂等。"""


# ---------------------------------------------------------------------------
# 后端公共骨架
# ---------------------------------------------------------------------------


class _TrackedInputBackend:
    """输入后端骨架：动作展开、按下状态跟踪、``release_all`` 与保持时间。

    子类只实现底层事件发送方法：

    - :meth:`_emit`：把一组 ``INPUT`` 交给 SendInput（键盘事件与默认后端共用）；
    - :meth:`_raw_move_absolute` / :meth:`_raw_move_relative` / :meth:`_raw_button` /
      :meth:`_raw_wheel`：鼠标事件的具体发送方式。

    ``send`` / ``release_all`` 内部加锁，保证“按下状态跟踪”与事件发送的顺序一致；
    ``release_all`` 只发送当前确实处于按下状态的键鼠事件，重复调用不会重复释放。
    """

    def __init__(
        self,
        config: InputConfig | None = None,
        *,
        sleeper: Sleeper,
        rng: random.Random,
        scan_code_resolver: ScanCodeResolver | None,
        send_input: SendInputFn,
    ) -> None:
        self._config = config or InputConfig()
        self._sleeper = sleeper
        self._rng = rng
        self._scan_code_resolver = scan_code_resolver or map_vk_to_scan
        self._send_input = send_input
        self._lock = threading.RLock()
        #: 已按下、尚未释放的键盘按键（规范名 -> 绑定）
        self._pressed_keys: dict[str, KeyBinding] = {}
        #: 已按下、尚未释放的鼠标按键
        self._pressed_buttons: set[str] = set()
        self._consecutive_failures = 0

    # --- 供上层/测试读取的状态 -------------------------------------
    @property
    def config(self) -> InputConfig:
        return self._config

    @property
    def consecutive_failures(self) -> int:
        """连续 SendInput 失败次数（详细设计 9.2：连续三次失败应进入暂停）。"""
        return self._consecutive_failures

    @property
    def pressed_keys(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._pressed_keys)

    @property
    def pressed_buttons(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._pressed_buttons)

    # --- 子类实现：底层事件发送 -------------------------------------
    def _emit(self, events: Sequence[INPUT]) -> None:
        raise NotImplementedError

    def _raw_move_absolute(self, x: int, y: int) -> None:
        raise NotImplementedError

    def _raw_move_relative(self, dx: int, dy: int) -> None:
        raise NotImplementedError

    def _raw_button(self, button: str, *, down: bool) -> None:
        raise NotImplementedError

    def _raw_wheel(self, delta: int) -> None:
        raise NotImplementedError

    # --- 公共实现 ---------------------------------------------------
    def send(self, actions: Sequence[Action]) -> None:
        """按顺序展开并执行一组动作；同一动作内部的按下/保持/释放不会被插入。"""
        with self._lock:
            for action in actions:
                if not isinstance(action, Action):
                    raise TypeError(f"输入后端只接受 Action，收到 {type(action)!r}")
                self._dispatch(action)

    def release_all(self) -> None:
        """释放所有按下状态的键鼠；幂等，重复调用不会重复发送。"""
        with self._lock:
            errors: list[InputUnavailable] = []
            for name in sorted(self._pressed_keys):
                binding = self._pressed_keys.pop(name)
                try:
                    self._send_key_event(binding, down=False)
                except InputUnavailable as exc:
                    errors.append(exc)
            for button in sorted(self._pressed_buttons):
                self._pressed_buttons.discard(button)
                try:
                    self._raw_button(button, down=False)
                except InputUnavailable as exc:
                    errors.append(exc)
            if errors:
                raise InputUnavailable(
                    "释放按键失败: " + "; ".join(str(exc) for exc in errors)
                )

    def _send_events(self, events: Sequence[INPUT]) -> None:
        """发送一批事件并校验 SendInput 返回值等于事件数量。"""
        if not events:
            return
        try:
            accepted = int(self._send_input(list(events)))
        except InputUnavailable:
            self._note_failure()
            raise
        except Exception as exc:  # ctypes/Win32 异常
            self._note_failure()
            raise InputUnavailable(f"SendInput 调用失败: {exc}") from exc
        if accepted != len(events):
            self._note_failure()
            raise InputUnavailable(
                f"SendInput 只接受 {accepted}/{len(events)} 个事件"
                f"（连续失败 {self._consecutive_failures} 次）"
            )
        self._consecutive_failures = 0

    def _note_failure(self) -> None:
        self._consecutive_failures += 1

    def _hold_seconds(self, hold_range: tuple[int, int]) -> float:
        """按配置区间与注入 RNG 采样保持时间（秒）。"""
        low, high = sorted(int(v) for v in hold_range)
        if high <= 0:
            return 0.0
        return max(0.0, self._rng.uniform(max(0, low), high) / 1000.0)

    def _scan_code_for(self, binding: KeyBinding) -> int:
        """优先 MapVirtualKeyW 查询扫描码，失败回退内置表。"""
        scan = int(self._scan_code_resolver(binding.vk))
        if not 0 < scan < 0x100:
            scan = binding.scan
        return scan

    def _send_key_event(self, binding: KeyBinding, *, down: bool) -> None:
        scan = self._scan_code_for(binding)
        self._send_events([build_key_event(binding, down=down, scan_code=scan)])

    def _key_event(self, key: str | None, *, down: bool) -> None:
        if not key:
            raise ValueError("按键动作缺少 key")
        binding = resolve_key(key)
        if binding.is_mouse:
            self._button_event(binding.mouse_button or "left", down=down)
            return
        self._send_key_event(binding, down=down)
        if down:
            self._pressed_keys[binding.name] = binding
        else:
            self._pressed_keys.pop(binding.name, None)

    def _button_event(self, button: str, *, down: bool) -> None:
        # 先发送再记录状态：发送失败时不应把按键当成已按下
        self._raw_button(button, down=down)
        if down:
            self._pressed_buttons.add(button)
        else:
            self._pressed_buttons.discard(button)

    def _click_abs(self, action: Action) -> None:
        if action.x is None or action.y is None:
            raise ValueError("click_abs 动作缺少 x/y 坐标")
        # 内部顺序固定：移动光标 -> 按下 -> 保持 -> 释放（详细设计 9.2）
        self._raw_move_absolute(int(action.x), int(action.y))
        self._button_event("left", down=True)
        self._sleeper.sleep(self._hold_seconds(self._config.click_hold_ms))
        self._button_event("left", down=False)

    def _move_abs(self, action: Action) -> None:
        """绝对移动只移动光标：不按下/不释放，也不等待 click_hold_ms。

        船只列表滚动（ShipChecker）需要先把光标移到列表区域再发 ``MOUSE_WHEEL``，
        若用 ``CLICK_ABS`` 会误点击列表里的船只。
        """
        if action.x is None or action.y is None:
            raise ValueError("move_abs 动作缺少 x/y 坐标")
        self._raw_move_absolute(int(action.x), int(action.y))

    def _dispatch(self, action: Action) -> None:
        kind = action.kind
        if kind is ActionKind.CLICK_ABS:
            self._click_abs(action)
        elif kind is ActionKind.MOVE_ABS:
            self._move_abs(action)
        elif kind is ActionKind.MOVE_REL:
            dx = 0 if action.dx is None else int(action.dx)
            dy = 0 if action.dy is None else int(action.dy)
            self._raw_move_relative(dx, dy)
        elif kind is ActionKind.KEY_DOWN:
            self._key_event(action.key, down=True)
        elif kind is ActionKind.KEY_UP:
            self._key_event(action.key, down=False)
        elif kind is ActionKind.MOUSE_DOWN:
            self._button_event("left", down=True)
        elif kind is ActionKind.MOUSE_UP:
            self._button_event("left", down=False)
        elif kind is ActionKind.MOUSE_WHEEL:
            if int(action.wheel_delta) == 0:
                logger.debug("滚轮动作 wheel_delta=0，跳过: %s", action.label)
                return
            self._raw_wheel(int(action.wheel_delta))
        elif kind is ActionKind.WAIT:
            self._sleeper.sleep(max(0, int(action.duration_ms)) / 1000.0)
        else:  # pragma: no cover - ActionKind 为封闭枚举
            raise ValueError(f"不支持的动作类型: {kind!r}")


# ---------------------------------------------------------------------------
# 默认后端：SendInput
# ---------------------------------------------------------------------------


class SendInputBackend(_TrackedInputBackend):
    """基于 ``user32.SendInput`` 的默认后端（详细设计 9.2）。

    测试通过 ``send_input`` / ``virtual_screen_provider`` / ``sleeper`` / ``rng`` /
    ``scan_code_resolver`` 注入假实现，不会真正发送键鼠事件。
    """

    def __init__(
        self,
        config: InputConfig | None = None,
        *,
        send_input: SendInputFn | None = None,
        virtual_screen_provider: VirtualScreenProvider | None = None,
        sleeper: Sleeper | None = None,
        rng: random.Random | None = None,
        scan_code_resolver: ScanCodeResolver | None = None,
    ) -> None:
        self._virtual_screen = virtual_screen_provider or virtual_screen_rect
        super().__init__(
            config,
            sleeper=sleeper or SystemSleeper(),
            rng=rng or random.Random(),
            scan_code_resolver=scan_code_resolver,
            send_input=send_input or _default_send_input,
        )

    def _emit(self, events: Sequence[INPUT]) -> None:
        self._send_events(events)

    def _raw_move_absolute(self, x: int, y: int) -> None:
        rect = self._virtual_screen()
        dx, dy = normalize_absolute(x, y, rect)
        self._emit([build_mouse_absolute_event(dx, dy)])

    def _raw_move_relative(self, dx: int, dy: int) -> None:
        self._emit([build_mouse_relative_event(dx, dy)])

    def _raw_button(self, button: str, *, down: bool) -> None:
        self._emit([build_mouse_button_event(button, down=down)])

    def _raw_wheel(self, delta: int) -> None:
        self._emit([build_mouse_wheel_event(delta)])


# ---------------------------------------------------------------------------
# 备选后端：SetCursorPos + mouse_event（详细设计 9.6）
# ---------------------------------------------------------------------------


class SetCursorPosBackend(_TrackedInputBackend):
    """备选后端（详细设计 9.6）：用 ``SetCursorPos`` 定位光标 + ``mouse_event`` 按键。

    SendInput 绝对点击被游戏忽略时，通过配置 ``input.backend=setcursor`` 切换。
    键盘事件仍使用 SendInput 扫描码；测试同样全部注入假实现。
    """

    def __init__(
        self,
        config: InputConfig | None = None,
        *,
        send_input: SendInputFn | None = None,
        set_cursor_pos: SetCursorPosFn | None = None,
        get_cursor_pos: GetCursorPosFn | None = None,
        mouse_event: MouseEventFn | None = None,
        sleeper: Sleeper | None = None,
        rng: random.Random | None = None,
        scan_code_resolver: ScanCodeResolver | None = None,
    ) -> None:
        self._set_cursor_pos_fn = set_cursor_pos or _default_set_cursor_pos
        self._get_cursor_pos_fn = get_cursor_pos or _default_get_cursor_pos
        self._mouse_event_fn = mouse_event or _default_mouse_event
        super().__init__(
            config,
            sleeper=sleeper or SystemSleeper(),
            rng=rng or random.Random(),
            scan_code_resolver=scan_code_resolver,
            send_input=send_input or _default_send_input,
        )

    def _emit(self, events: Sequence[INPUT]) -> None:
        self._send_events(events)

    def _set_position(self, x: int, y: int) -> None:
        try:
            ok = bool(self._set_cursor_pos_fn(int(x), int(y)))
        except Exception as exc:
            raise InputUnavailable(f"SetCursorPos 调用失败: {exc}") from exc
        if not ok:
            raise InputUnavailable(f"SetCursorPos 失败: ({x}, {y})")

    def _mouse_event(self, flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> None:
        try:
            self._mouse_event_fn(int(flags), int(dx), int(dy), int(data))
        except InputUnavailable:
            raise
        except Exception as exc:
            raise InputUnavailable(f"mouse_event 调用失败: {exc}") from exc

    def _raw_move_absolute(self, x: int, y: int) -> None:
        self._set_position(x, y)

    def _raw_move_relative(self, dx: int, dy: int) -> None:
        self._mouse_event(MOUSEEVENTF_MOVE, dx, dy, 0)

    def _raw_button(self, button: str, *, down: bool) -> None:
        flags = MOUSE_BUTTON_FLAGS.get(button)
        if flags is None:
            raise ValueError(f"不支持的鼠标按键: {button!r}")
        down_flag, up_flag = flags
        self._mouse_event(down_flag if down else up_flag, 0, 0, 0)

    def _raw_wheel(self, delta: int) -> None:
        self._mouse_event(MOUSEEVENTF_WHEEL, 0, 0, wheel_mouse_data(delta))


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------

_SENDINPUT_ALIASES = frozenset({"sendinput", "send_input", "default", "input"})
_SETCURSOR_ALIASES = frozenset({"setcursor", "set_cursor", "set_cursor_pos", "setcursorpos"})


def make_backend(
    config: InputConfig | None = None,
    *,
    backend: str | None = None,
    **kwargs: Any,
) -> InputBackend:
    """按配置创建输入后端（详细设计 9.6 的可切换实现）。

    ``backend`` 参数优先，其次取 ``config.backend``；未知名称抛
    :class:`~src.common.errors.InputUnavailable`，不静默回退。
    ``kwargs`` 透传给具体后端构造函数（测试用于注入 fake Win32 调用）。
    """
    cfg = config or InputConfig()
    name = (backend if backend is not None else cfg.backend or "sendinput").strip().lower()
    if name in _SENDINPUT_ALIASES:
        return SendInputBackend(cfg, **kwargs)
    if name in _SETCURSOR_ALIASES:
        return SetCursorPosBackend(cfg, **kwargs)
    raise InputUnavailable(f"未知输入后端: {name!r}（支持 sendinput / setcursor）")


__all__ = [
    "INPUT",
    "KEYBDINPUT",
    "MOUSEINPUT",
    "SUPPORTED_KEY_NAMES",
    "KEY_BINDINGS",
    "InputBackend",
    "KeyBinding",
    "NORMALIZE_MAX",
    "SendInputBackend",
    "SetCursorPosBackend",
    "build_key_event",
    "build_mouse_absolute_event",
    "build_mouse_button_event",
    "build_mouse_relative_event",
    "build_mouse_wheel_event",
    "is_supported_key",
    "make_backend",
    "map_vk_to_scan",
    "normalize_absolute",
    "resolve_key",
    "wheel_mouse_data",
]
