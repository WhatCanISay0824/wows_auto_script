"""Win32 窗口定位、客户区坐标与 DPI 感知（详细设计 5.2 / 9.3 / 12.5）。

只在 Windows 上可用；导入本模块本身是安全的，非 Windows 平台调用相关函数会抛
``WindowUnavailable``。纯函数（窗口匹配规则、标题正则）可在任何平台测试。
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes

from .errors import WindowUnavailable
from .types import Rect, WindowInfo

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - 项目只支持 Windows
    user32 = None  # type: ignore[assignment]
    kernel32 = None  # type: ignore[assignment]

# --- 常量 ---------------------------------------------------------------
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
MIN_CLIENT_WIDTH = 320
MIN_CLIENT_HEIGHT = 200

# --- Win32 类型 ---------------------------------------------------------
if _IS_WINDOWS:
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.restype = wintypes.BOOL
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.restype = wintypes.BOOL
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.ClientToScreen.restype = wintypes.BOOL
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL


# ---------------------------------------------------------------------------
# DPI
# ---------------------------------------------------------------------------


def set_dpi_awareness() -> str:
    """声明 DPI 感知，必须在创建窗口句柄/截屏之前调用。

    详细设计 5.2：程序启动时声明 DPI 感知，避免系统缩放把截屏尺寸和点击坐标搞混。
    返回实际生效的模式名，便于 check_env 记录。
    """
    if not _IS_WINDOWS:
        return "not_windows"
    # Windows 10 1703+：PER_MONITOR_AWARE_V2 = -4
    try:
        func = user32.SetProcessDpiAwarenessContext
        func.argtypes = [ctypes.c_void_p]
        func.restype = wintypes.BOOL
        if func(ctypes.c_void_p(-4)):
            return "per_monitor_v2"
    except (AttributeError, OSError):
        pass
    try:  # Windows 8.1+：PROCESS_PER_MONITOR_DPI_AWARE = 2
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        if shcore.SetProcessDpiAwareness(2) == 0:
            return "per_monitor"
    except (OSError, AttributeError):
        pass
    try:  # 旧系统
        user32.SetProcessDPIAware.argtypes = []
        user32.SetProcessDPIAware.restype = wintypes.BOOL
        if user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    return "none"


def is_dpi_aware() -> bool:
    if not _IS_WINDOWS:
        return False
    try:
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.AreDpiAwarenessContextsEqual.restype = wintypes.BOOL
        current = ctypes.c_void_p(user32.GetThreadDpiAwarenessContext())
        unaware = ctypes.c_void_p(-1)  # DPI_AWARENESS_CONTEXT_UNAWARE
        return not user32.AreDpiAwarenessContextsEqual(current, unaware)
    except (AttributeError, OSError):
        return False


# ---------------------------------------------------------------------------
# 窗口匹配规则（纯函数，便于单元测试）
# ---------------------------------------------------------------------------


def window_matches(process_name: str, title: str, process_names: list[str] | tuple[str, ...],
                   title_patterns: list[str] | tuple[str, ...]) -> bool:
    """窗口是否匹配客户端配置（详细设计 3.2 / 9.3）：进程名优先，标题正则兜底。"""
    process = (process_name or "").strip().lower()
    for name in process_names:
        if name and process == name.strip().lower():
            return True
        if name and process and process.endswith("\\" + name.strip().lower()):
            return True
    for pattern in title_patterns:
        if not pattern:
            continue
        try:
            if re.search(pattern, title or "", re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


# ---------------------------------------------------------------------------
# 窗口操作
# ---------------------------------------------------------------------------


def _process_name_of(hwnd: int) -> str:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return os.path.basename(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _title_of(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


def client_rect_screen(hwnd: int) -> Rect | None:
    """返回窗口客户区的屏幕矩形（x/y 为屏幕物理像素）。"""
    if not _IS_WINDOWS or not hwnd:
        return None
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    if width <= 0 or height <= 0:
        return None
    point = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(point)):
        return None
    return Rect(int(point.x), int(point.y), int(width), int(height))


def virtual_screen_rect() -> Rect:
    """整个虚拟桌面（多显示器合并）的屏幕矩形，SendInput 归一化使用。"""
    if not _IS_WINDOWS:
        raise WindowUnavailable("当前平台不支持 Win32 虚拟屏幕")
    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN) or user32.GetSystemMetrics(0)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN) or user32.GetSystemMetrics(1)
    if width <= 0 or height <= 0:
        raise WindowUnavailable("无法获取虚拟屏幕尺寸")
    return Rect(int(left), int(top), int(width), int(height))


class WindowLocator:
    """按客户端配置查找游戏窗口。"""

    def __init__(self, process_names: list[str] | tuple[str, ...],
                 title_patterns: list[str] | tuple[str, ...]) -> None:
        self.process_names = tuple(process_names)
        self.title_patterns = tuple(title_patterns)

    def _build_info(self, hwnd: int, title: str, process_name: str) -> WindowInfo | None:
        client = client_rect_screen(hwnd)
        if client is None:
            return None
        return WindowInfo(
            hwnd=int(hwnd),
            title=title,
            process_name=process_name,
            client_rect=client,
            foreground=bool(user32.GetForegroundWindow() == hwnd),
            minimized=bool(user32.IsIconic(hwnd)),
            valid=True,
        )

    def find(self) -> WindowInfo | None:
        """查找第一个匹配且客户区足够大的可见窗口。"""
        if not _IS_WINDOWS:
            raise WindowUnavailable("项目只支持 Windows 平台")
        matches: list[WindowInfo] = []

        def _callback(hwnd: int, _lparam: int) -> bool:
            if not user32.IsWindowVisible(hwnd):
                return True
            title = _title_of(hwnd)
            process = _process_name_of(hwnd)
            if not window_matches(process, title, self.process_names, self.title_patterns):
                return True
            info = self._build_info(hwnd, title, process)
            if info is not None and info.client_rect.w >= MIN_CLIENT_WIDTH and info.client_rect.h >= MIN_CLIENT_HEIGHT:
                matches.append(info)
            return True

        user32.EnumWindows(WNDENUMPROC(_callback), 0)
        if not matches:
            return None
        # 优先进程名精确命中，其次客户区面积最大（避免启动器小窗）
        def _score(info: WindowInfo) -> tuple[int, int]:
            exact = any(
                info.process_name.lower() == name.strip().lower()
                for name in self.process_names if name
            )
            return (1 if exact else 0, info.client_rect.w * info.client_rect.h)

        matches.sort(key=_score, reverse=True)
        return matches[0]

    def find_or_raise(self) -> WindowInfo:
        info = self.find()
        if info is None:
            raise WindowUnavailable(
                f"找不到游戏窗口（进程名 {list(self.process_names)}，标题 {list(self.title_patterns)}）"
            )
        return info

    def refresh(self, hwnd: int) -> WindowInfo | None:
        if not _IS_WINDOWS or not hwnd or not user32.IsWindow(hwnd):
            return None
        return self._build_info(hwnd, _title_of(hwnd), _process_name_of(hwnd))

    def is_foreground(self, hwnd: int) -> bool:
        if not _IS_WINDOWS or not hwnd:
            return False
        return bool(user32.GetForegroundWindow() == hwnd)

    def bring_to_foreground(self, hwnd: int) -> bool:
        """尝试把游戏窗口切到前台（详细设计 9.3）。失败返回 False，不抛异常。"""
        if not _IS_WINDOWS or not hwnd:
            return False
        if user32.GetForegroundWindow() == hwnd:
            return True
        foreground = user32.GetForegroundWindow()
        current_thread = kernel32.GetCurrentThreadId()
        target_thread = user32.GetWindowThreadProcessId(hwnd, None)
        foreground_thread = user32.GetWindowThreadProcessId(foreground, None) if foreground else 0
        attached = False
        try:
            if foreground_thread and foreground_thread != current_thread:
                attached = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
            if target_thread:
                user32.AttachThreadInput(current_thread, target_thread, True)
            user32.BringWindowToTop(hwnd)
            result = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached:
                user32.AttachThreadInput(current_thread, foreground_thread, False)
        return result

    def set_topmost(self, hwnd: int, enabled: bool) -> bool:
        if not _IS_WINDOWS or not hwnd:
            return False
        insert_after = HWND_TOPMOST if enabled else HWND_NOTOPMOST
        return bool(
            user32.SetWindowPos(hwnd, insert_after, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        )
