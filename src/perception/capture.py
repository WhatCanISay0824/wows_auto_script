"""GDI 截屏与帧几何工具（详细设计 5.2 / 5.11）。

实现口径（评审 P0-8）：

- 用 ``CreateDIBSection`` 创建 top-down 32bpp DIB（``biHeight = -height``、
  ``biBitCount = 32``、``BI_RGB``），``BitBlt`` 直接画进 DIB 内存，再从内存读出
  BGR。不使用 ``GetDIBits``，避免“位图已选入 DC 时 GetDIBits 失败/黑图”的顺序问题。
- 32bpp 的 stride 固定为 ``width * 4``，转换函数显式接收 stride，不假设缓冲区连续。
- 声明 DPI 感知后才创建窗口/DC：main 会先声明，这里兜底声明一次并记录实际模式。
- 截屏后断言 ``image.shape == (client_rect.h, client_rect.w, 3)``，不一致直接视为
  ``CaptureUnavailable``；黑屏（灰度均值/方差接近 0）先尝试 ``PrintWindow`` 兜底，
  仍不可用则抛 ``CaptureUnavailable``，绝不把黑帧当有效画面返回。
- 内存 DC / DIB 在窗口句柄和客户区尺寸不变时复用，尺寸变化时重建；每次截屏记录
  耗时，超过预算（默认 300 ms）写警告。

本模块只在 Windows 上执行真实 GDI 调用；纯函数（缓冲区转换、矩形裁剪校验、帧有效
性判断）不依赖窗口，可离线测试。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from collections import deque
from collections.abc import Buffer, Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Protocol

import cv2
import numpy as np

from ..common.clock import Clock, SystemClock
from ..common.errors import CaptureUnavailable, WindowUnavailable
from ..common.types import Rect, WindowInfo
from ..common.window import is_dpi_aware, set_dpi_awareness

_IS_WINDOWS = sys.platform == "win32"
_log = logging.getLogger(__name__)

#: 有效帧的最小尺寸（像素），小于该值视为无效帧
MIN_FRAME_WIDTH = 8
MIN_FRAME_HEIGHT = 8
#: 黑屏判定阈值：灰度均值与标准差同时低于阈值时视为黑屏/纯色
DEFAULT_BLACK_MEAN_THRESHOLD = 2.0
DEFAULT_BLACK_STD_THRESHOLD = 2.0
#: 单帧截屏耗时预算（毫秒），超过时记录警告（详细设计 5.2 / 5.11）
DEFAULT_CAPTURE_BUDGET_MS = 300.0

SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
DIB_RGB_COLORS = 0
BI_RGB = 0
PW_RENDERFULLCONTENT = 0x00000002


# ---------------------------------------------------------------------------
# 帧数据
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class Frame:
    """一帧整窗截图（详细设计 5.1）。

    ``image`` 为 BGR、``HxWx3``、uint8；``window_rect`` 为窗口客户区屏幕矩形；
    ``frame_id`` 从 1 开始逐帧递增；``timestamp`` 为单调时钟秒数。

    相等性按对象标识判断（``eq=False``）：帧内含 NumPy 数组，逐元素比较会得到
    布尔数组；需要判断“是否同一帧”时只应比较 ``frame_id``/``timestamp``。
    """

    image: np.ndarray
    timestamp: float
    window_rect: Rect
    frame_id: int = 0

    @property
    def width(self) -> int:
        return int(self.image.shape[1])

    @property
    def height(self) -> int:
        return int(self.image.shape[0])

    @property
    def shape(self) -> tuple[int, ...]:
        return tuple(int(v) for v in self.image.shape)

    def crop(self, rect: Rect) -> np.ndarray:
        """按窗口内像素矩形裁剪并返回副本（越界会裁剪到图像边界）。"""
        return crop_bgr(self.image, rect, copy=True)


# ---------------------------------------------------------------------------
# 协议：窗口来源与图像来源
# ---------------------------------------------------------------------------


class WindowSource(Protocol):
    """窗口信息来源，``WindowLocator`` 与该协议的假实现均可注入。"""

    def find(self) -> WindowInfo | None:
        """重新查找游戏窗口。"""

    def refresh(self, hwnd: int) -> WindowInfo | None:
        """按句柄刷新窗口信息；窗口失效返回 None。"""


class FrameSource(Protocol):
    """图像来源：真实实现是 ``GdiFrameSource``，测试可注入假实现。"""

    def grab(self, hwnd: int, rect: Rect) -> np.ndarray:
        """抓取客户区画面，返回 BGR、HxWx3、uint8。"""

    def close(self) -> None:
        """释放资源。"""


# ---------------------------------------------------------------------------
# 纯函数：缓冲区转换 / 几何校验 / 帧有效性
# ---------------------------------------------------------------------------


def bgra_top_down_to_bgr(
    buffer: Buffer,
    width: int,
    height: int,
    *,
    stride: int | None = None,
) -> np.ndarray:
    """把 top-down 32bpp BGRA 缓冲区转成 BGR ndarray（``HxWx3``、uint8）。

    ``stride`` 为每行字节数，默认 ``width * 4``；DIB 的 stride 需要按行显式处理，
    不能假设整个缓冲区连续。缓冲区长度不足或尺寸非法时抛 ``ValueError``。
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"图像尺寸必须为正: {width}x{height}")
    row_bytes = width * 4
    row_stride = row_bytes if stride is None else int(stride)
    if row_stride < row_bytes:
        raise ValueError(f"stride 不能小于每行字节数 {row_bytes}: {row_stride}")
    required = row_stride * height
    try:
        available = memoryview(buffer).nbytes
    except TypeError as exc:  # pragma: no cover - 正常调用不会走到
        raise ValueError(f"不支持的数据缓冲区: {type(buffer)!r}") from exc
    if available < required:
        raise ValueError(f"缓冲区长度不足: 需要 {required} 字节，实际 {available} 字节")
    raw = np.frombuffer(buffer, dtype=np.uint8, count=required)
    rows = raw.reshape(height, row_stride)[:, :row_bytes].reshape(height, width, 4)
    return cv2.cvtColor(rows, cv2.COLOR_BGRA2BGR)


def dib_section_to_bgr(bits: int, width: int, height: int, *, stride: int | None = None) -> np.ndarray:
    """把 ``CreateDIBSection`` 返回的位图内存（top-down 32bpp）读成 BGR ndarray。

    该函数是真实 GDI 路径与测试的接缝：测试可以 monkeypatch 它，直接返回合成图像。
    """
    if not bits:
        raise ValueError("DIB 内存指针为空")
    row_stride = width * 4 if stride is None else int(stride)
    total = row_stride * height
    buffer = (ctypes.c_ubyte * total).from_address(int(bits))
    return bgra_top_down_to_bgr(buffer, width, height, stride=row_stride)


def rect_within(rect: Rect, width: int, height: int) -> bool:
    """判断矩形是否完整落在 ``width x height`` 图像内。"""
    return (
        rect.w > 0
        and rect.h > 0
        and rect.x >= 0
        and rect.y >= 0
        and rect.right <= width
        and rect.bottom <= height
    )


def clip_rect(rect: Rect, width: int, height: int) -> Rect:
    """把矩形裁剪到图像范围内；完全越界或尺寸为零时抛 ``ValueError``。"""
    left = max(0, rect.x)
    top = max(0, rect.y)
    right = min(int(width), rect.right)
    bottom = min(int(height), rect.bottom)
    if right <= left or bottom <= top:
        raise ValueError(f"矩形完全位于图像之外: {rect!r}，图像 {width}x{height}")
    return Rect(left, top, right - left, bottom - top)


def crop_bgr(image: np.ndarray, rect: Rect, *, copy: bool = False) -> np.ndarray:
    """按像素矩形裁剪 BGR 图像；矩形越界时裁剪到图像边界，完全越界抛 ``ValueError``。"""
    if not isinstance(image, np.ndarray) or image.ndim != 3 or image.shape[2] != 3:
        shape = getattr(image, "shape", None)
        raise ValueError(f"图像必须是 HxWx3 的 ndarray: {shape!r}")
    height, width = image.shape[:2]
    clipped = clip_rect(rect, width, height)
    view = image[clipped.y : clipped.bottom, clipped.x : clipped.right]
    return view.copy() if copy else view


def frame_invalid_reason(
    image: np.ndarray | None,
    *,
    mean_threshold: float = DEFAULT_BLACK_MEAN_THRESHOLD,
    std_threshold: float = DEFAULT_BLACK_STD_THRESHOLD,
) -> str | None:
    """判断一帧图像是否可用；可用返回 None，否则返回中文原因。

    无用帧包括：空图像、类型/通道数异常、尺寸过小、黑屏或纯色（灰度均值与标准差
    同时接近 0）。黑屏判定阈值可由截屏器配置。
    """
    if image is None:
        return "截屏结果为空"
    if not isinstance(image, np.ndarray):
        return f"截屏结果类型异常: {type(image)!r}"
    if image.size == 0:
        return "截屏结果尺寸为零"
    if image.dtype != np.uint8:
        return f"截屏结果类型不是 uint8: {image.dtype}"
    if image.ndim != 3 or image.shape[2] != 3:
        return f"截屏结果不是 HxWx3 BGR: {image.shape}"
    height, width = image.shape[:2]
    if width < MIN_FRAME_WIDTH or height < MIN_FRAME_HEIGHT:
        return f"截屏尺寸过小: {width}x{height}"
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    mean = float(gray.mean())
    std = float(gray.std())
    if mean <= mean_threshold and std <= std_threshold:
        return f"黑屏/纯色画面 mean={mean:.2f} std={std:.2f}"
    return None


def frame_is_valid(
    image: np.ndarray | None,
    *,
    mean_threshold: float = DEFAULT_BLACK_MEAN_THRESHOLD,
    std_threshold: float = DEFAULT_BLACK_STD_THRESHOLD,
) -> bool:
    """帧可用返回 True（见 :func:`frame_invalid_reason`）。"""
    return frame_invalid_reason(image, mean_threshold=mean_threshold, std_threshold=std_threshold) is None


# ---------------------------------------------------------------------------
# Win32 GDI 底层封装（测试通过 monkeypatch 这些函数验证生命周期）
# ---------------------------------------------------------------------------


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 1)]


_gdi32: object | None = None
_user32: object | None = None


def _load_win32() -> tuple[object, object]:
    """延迟加载并声明 Win32 函数签名；非 Windows 平台抛 ``CaptureUnavailable``。"""
    global _gdi32, _user32
    if not _IS_WINDOWS:
        raise CaptureUnavailable("GDI 截屏仅支持 Windows 平台")
    if _gdi32 is not None and _user32 is not None:
        return _gdi32, _user32
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateDIBSection.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_uint32,
    ]
    gdi32.CreateDIBSection.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_int
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.c_int
    gdi32.BitBlt.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint32,
    ]
    gdi32.BitBlt.restype = ctypes.c_int
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    _gdi32, _user32 = gdi32, user32
    return gdi32, user32


def _gdi_get_dc(hwnd: int) -> int:
    _, user32 = _load_win32()
    return int(user32.GetDC(wintypes.HWND(int(hwnd))) or 0)


def _gdi_release_dc(hwnd: int, dc: int) -> None:
    _, user32 = _load_win32()
    user32.ReleaseDC(wintypes.HWND(int(hwnd)), wintypes.HDC(int(dc)))


def _gdi_create_compatible_dc(source_dc: int) -> int:
    gdi32, _ = _load_win32()
    return int(gdi32.CreateCompatibleDC(ctypes.c_void_p(int(source_dc))) or 0)


def _gdi_create_dib_section(source_dc: int, width: int, height: int) -> tuple[int, int]:
    """创建 top-down 32bpp DIB，返回 ``(位图句柄, 内存指针)``。"""
    gdi32, _ = _load_win32()
    info = _BITMAPINFO()
    header = info.bmiHeader
    header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
    header.biWidth = int(width)
    header.biHeight = -int(height)  # 负高度 = top-down，行序与 NumPy 一致
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = BI_RGB
    header.biSizeImage = int(width) * int(height) * 4
    bits = ctypes.c_void_p(0)
    bitmap = gdi32.CreateDIBSection(
        ctypes.c_void_p(int(source_dc)),
        ctypes.byref(info),
        DIB_RGB_COLORS,
        ctypes.byref(bits),
        None,
        0,
    )
    return int(bitmap or 0), int(bits.value or 0)


def _gdi_select_object(dc: int, obj: int) -> int:
    gdi32, _ = _load_win32()
    return int(gdi32.SelectObject(ctypes.c_void_p(int(dc)), ctypes.c_void_p(int(obj))) or 0)


def _gdi_delete_object(obj: int) -> None:
    gdi32, _ = _load_win32()
    if obj:
        gdi32.DeleteObject(ctypes.c_void_p(int(obj)))


def _gdi_delete_dc(dc: int) -> None:
    gdi32, _ = _load_win32()
    if dc:
        gdi32.DeleteDC(ctypes.c_void_p(int(dc)))


def _gdi_bitblt(dst_dc: int, width: int, height: int, src_dc: int) -> bool:
    gdi32, _ = _load_win32()
    ok = gdi32.BitBlt(
        ctypes.c_void_p(int(dst_dc)),
        0,
        0,
        int(width),
        int(height),
        ctypes.c_void_p(int(src_dc)),
        0,
        0,
        SRCCOPY | CAPTUREBLT,
    )
    return bool(ok)


def _gdi_print_window(hwnd: int, hdc: int) -> bool:
    _, user32 = _load_win32()
    return bool(user32.PrintWindow(wintypes.HWND(int(hwnd)), wintypes.HDC(int(hdc)), PW_RENDERFULLCONTENT))


# ---------------------------------------------------------------------------
# GDI 图像来源
# ---------------------------------------------------------------------------


class GdiFrameSource:
    """真实 GDI 截屏：``CreateDIBSection`` + ``BitBlt``，内存 DC/位图按尺寸复用。"""

    supports_print_window = True

    def __init__(self, *, dpi_aware: bool = True) -> None:
        # 兜底声明 DPI 感知：main 一般已经声明，此处只记录实际生效模式
        if not dpi_aware:
            self.dpi_mode = "skipped"
        elif is_dpi_aware():
            self.dpi_mode = "already_aware"
        else:
            self.dpi_mode = set_dpi_awareness()
        self._hwnd = 0
        self._size: tuple[int, int] = (0, 0)
        self._mem_dc = 0
        self._bitmap = 0
        self._old_bitmap = 0
        self._bits = 0

    @property
    def dpi_awareness(self) -> str:
        return self.dpi_mode

    def _ensure_resources(self, hwnd: int, rect: Rect) -> None:
        size = (rect.w, rect.h)
        if (
            self._mem_dc
            and self._bitmap
            and self._bits
            and self._hwnd == hwnd
            and self._size == size
        ):
            return
        self._release()
        source_dc = _gdi_get_dc(hwnd)
        if not source_dc:
            raise CaptureUnavailable(f"GetDC 失败（hwnd={hwnd}）")
        try:
            mem_dc = _gdi_create_compatible_dc(source_dc)
        finally:
            _gdi_release_dc(hwnd, source_dc)
        if not mem_dc:
            raise CaptureUnavailable("CreateCompatibleDC 失败")
        try:
            bitmap, bits = _gdi_create_dib_section(mem_dc, rect.w, rect.h)
            if not bitmap or not bits:
                raise CaptureUnavailable("CreateDIBSection 失败")
            old_bitmap = _gdi_select_object(mem_dc, bitmap)
        except CaptureUnavailable:
            _gdi_delete_dc(mem_dc)
            raise
        self._mem_dc = mem_dc
        self._bitmap = bitmap
        self._bits = bits
        self._old_bitmap = old_bitmap
        self._hwnd = hwnd
        self._size = size

    def grab(self, hwnd: int, rect: Rect) -> np.ndarray:
        if rect.w <= 0 or rect.h <= 0:
            raise CaptureUnavailable(f"客户区尺寸无效: {rect.w}x{rect.h}")
        self._ensure_resources(hwnd, rect)
        source_dc = _gdi_get_dc(hwnd)
        if not source_dc:
            raise CaptureUnavailable(f"GetDC 失败（hwnd={hwnd}）")
        try:
            ok = _gdi_bitblt(self._mem_dc, rect.w, rect.h, source_dc)
        finally:
            _gdi_release_dc(hwnd, source_dc)
        if not ok:
            raise CaptureUnavailable("BitBlt 失败，无法复制窗口画面")
        return dib_section_to_bgr(self._bits, rect.w, rect.h)

    def grab_print_window(self, hwnd: int, rect: Rect) -> np.ndarray:
        """``PrintWindow(PW_RENDERFULLCONTENT)`` 兜底路径，用于 BitBlt 得到黑屏时。"""
        if rect.w <= 0 or rect.h <= 0:
            raise CaptureUnavailable(f"客户区尺寸无效: {rect.w}x{rect.h}")
        self._ensure_resources(hwnd, rect)
        if not _gdi_print_window(hwnd, self._mem_dc):
            raise CaptureUnavailable("PrintWindow 失败")
        return dib_section_to_bgr(self._bits, rect.w, rect.h)

    def _release(self) -> None:
        if self._mem_dc and self._old_bitmap:
            _gdi_select_object(self._mem_dc, self._old_bitmap)
        if self._bitmap:
            _gdi_delete_object(self._bitmap)
        if self._mem_dc:
            _gdi_delete_dc(self._mem_dc)
        self._mem_dc = 0
        self._bitmap = 0
        self._old_bitmap = 0
        self._bits = 0
        self._hwnd = 0
        self._size = (0, 0)

    def close(self) -> None:
        self._release()


# ---------------------------------------------------------------------------
# 截屏统计
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureStats:
    """截屏耗时与成功率统计（详细设计 5.11 要求记录平均/95 分位耗时）。"""

    attempts: int = 0
    successes: int = 0
    failures: int = 0
    last_ms: float = 0.0
    avg_ms: float = 0.0
    max_ms: float = 0.0
    p95_ms: float = 0.0
    slow_frames: int = 0


# ---------------------------------------------------------------------------
# 截屏器
# ---------------------------------------------------------------------------


class GdiCapture:
    """GDI 整窗截屏器（详细设计 5.2）。

    典型用法::

        locator = WindowLocator(profile.window.process_names, profile.window.title_patterns)
        capture = GdiCapture(locator, require_foreground=profile.window.require_foreground)
        frame = capture.capture()      # 失败抛 CaptureUnavailable
        last = capture.last_frame      # 最近一帧缓存

    内存 DC 与 DIB 由 :class:`GdiFrameSource` 持有并按窗口尺寸复用；本类负责窗口定位、
    前台/尺寸校验、黑屏兜底、帧序号与耗时统计。
    """

    def __init__(
        self,
        locator: WindowSource,
        *,
        clock: Clock | None = None,
        source: FrameSource | None = None,
        require_foreground: bool = False,
        capture_budget_ms: float = DEFAULT_CAPTURE_BUDGET_MS,
        black_mean_threshold: float = DEFAULT_BLACK_MEAN_THRESHOLD,
        black_std_threshold: float = DEFAULT_BLACK_STD_THRESHOLD,
        enable_print_window: bool = True,
        history_size: int = 240,
        perf_counter: Callable[[], float] | None = None,
    ) -> None:
        self._locator = locator
        self._clock = clock or SystemClock()
        self._source: FrameSource = source if source is not None else GdiFrameSource()
        self._require_foreground = require_foreground
        self._capture_budget_ms = float(capture_budget_ms)
        self._black_mean_threshold = float(black_mean_threshold)
        self._black_std_threshold = float(black_std_threshold)
        self._enable_print_window = bool(enable_print_window)
        self._perf_counter = perf_counter or time.perf_counter
        self._durations: deque[float] = deque(maxlen=max(1, int(history_size)))

        self._hwnd = 0
        self._frame_id = 0
        self._last_frame: Frame | None = None
        self._attempts = 0
        self._successes = 0
        self._failures = 0
        self._slow_frames = 0

    # --- 查询 ---------------------------------------------------------
    @property
    def last_frame(self) -> Frame | None:
        """最近一次成功截屏的帧（供主循环在没有新帧时复用）。"""
        return self._last_frame

    @property
    def frame_id(self) -> int:
        return self._frame_id

    @property
    def source(self) -> FrameSource:
        return self._source

    @property
    def dpi_awareness(self) -> str:
        return str(getattr(self._source, "dpi_awareness", "unknown"))

    @property
    def stats(self) -> CaptureStats:
        durations = list(self._durations)
        if not durations:
            return CaptureStats(
                attempts=self._attempts,
                successes=self._successes,
                failures=self._failures,
                slow_frames=self._slow_frames,
            )
        ordered = sorted(durations)
        p95_index = max(0, min(len(ordered) - 1, (95 * len(ordered) + 99) // 100 - 1))
        return CaptureStats(
            attempts=self._attempts,
            successes=self._successes,
            failures=self._failures,
            last_ms=durations[-1],
            avg_ms=sum(durations) / len(durations),
            max_ms=ordered[-1],
            p95_ms=ordered[p95_index],
            slow_frames=self._slow_frames,
        )

    # --- 截屏 ---------------------------------------------------------
    def capture(self) -> Frame:
        """截取一帧；窗口缺失、尺寸异常、黑屏等失败都抛 ``CaptureUnavailable``。"""
        self._attempts += 1
        started = self._perf_counter()
        try:
            frame = self._capture_once()
        except BaseException:
            self._failures += 1
            raise
        finally:
            self._record_duration((self._perf_counter() - started) * 1000.0)
        self._successes += 1
        return frame

    def _capture_once(self) -> Frame:
        hwnd, rect = self._resolve_window()
        image = self._source.grab(hwnd, rect)
        image = self._validate_image(image, hwnd, rect)
        self._frame_id += 1
        frame = Frame(
            image=image,
            timestamp=float(self._clock.now()),
            window_rect=rect,
            frame_id=self._frame_id,
        )
        self._last_frame = frame
        return frame

    def _resolve_window(self) -> tuple[int, Rect]:
        info = self._locate()
        if info is None:
            raise CaptureUnavailable("找不到游戏窗口")
        if not info.valid:
            raise CaptureUnavailable("游戏窗口已失效")
        if info.minimized:
            raise CaptureUnavailable("游戏窗口已最小化")
        rect = info.client_rect
        if rect.w < MIN_FRAME_WIDTH or rect.h < MIN_FRAME_HEIGHT:
            raise CaptureUnavailable(f"客户区尺寸无效: {rect.w}x{rect.h}")
        if self._require_foreground and not info.foreground:
            raise CaptureUnavailable("游戏窗口不在前台，暂停截屏")
        return info.hwnd, rect

    def _locate(self) -> WindowInfo | None:
        """优先刷新已知句柄，失效后再整窗查找，避免每帧 EnumWindows。"""
        hwnd = self._hwnd
        if hwnd:
            try:
                info = self._locator.refresh(hwnd)
            except WindowUnavailable as exc:
                raise CaptureUnavailable(f"窗口查找失败: {exc}") from exc
            if info is not None:
                self._hwnd = info.hwnd
                return info
            self._hwnd = 0
        try:
            info = self._locator.find()
        except WindowUnavailable as exc:
            raise CaptureUnavailable(f"窗口查找失败: {exc}") from exc
        if info is not None:
            self._hwnd = info.hwnd
        return info

    def _validate_image(self, image: np.ndarray, hwnd: int, rect: Rect) -> np.ndarray:
        """校验尺寸与画面内容；黑屏时尝试 PrintWindow 兜底。"""
        expected = (rect.h, rect.w, 3)
        if not isinstance(image, np.ndarray):
            raise CaptureUnavailable(f"截屏结果类型异常: {type(image)!r}")
        if image.shape != expected:
            raise CaptureUnavailable(f"截屏尺寸不一致: {image.shape} != {expected}")
        reason = frame_invalid_reason(
            image,
            mean_threshold=self._black_mean_threshold,
            std_threshold=self._black_std_threshold,
        )
        if reason is None:
            return image
        _log.warning("截屏画面不可用（%s），尝试 PrintWindow 兜底", reason)
        fallback = getattr(self._source, "grab_print_window", None) if self._enable_print_window else None
        if not callable(fallback):
            raise CaptureUnavailable(f"截屏画面不可用: {reason}")
        try:
            recovered = fallback(hwnd, rect)
        except CaptureUnavailable as exc:
            raise CaptureUnavailable(f"截屏画面不可用（{reason}），PrintWindow 兜底失败: {exc}") from exc
        if not isinstance(recovered, np.ndarray):
            raise CaptureUnavailable(f"截屏画面不可用（{reason}），PrintWindow 返回类型异常")
        if recovered.shape != expected:
            raise CaptureUnavailable(
                f"截屏画面不可用（{reason}），PrintWindow 尺寸不一致: {recovered.shape} != {expected}"
            )
        reason2 = frame_invalid_reason(
            recovered,
            mean_threshold=self._black_mean_threshold,
            std_threshold=self._black_std_threshold,
        )
        if reason2 is not None:
            raise CaptureUnavailable(f"截屏画面不可用（{reason}），PrintWindow 兜底仍不可用（{reason2}）")
        _log.warning("PrintWindow 兜底成功，当前帧改由 PrintWindow 获取")
        return recovered

    def _record_duration(self, duration_ms: float) -> None:
        self._durations.append(float(duration_ms))
        if duration_ms > self._capture_budget_ms:
            self._slow_frames += 1
            _log.warning(
                "截屏耗时 %.1f ms 超过预算 %.1f ms",
                duration_ms,
                self._capture_budget_ms,
            )

    # --- 生命周期 -----------------------------------------------------
    def close(self) -> None:
        close = getattr(self._source, "close", None)
        if callable(close):
            close()
        self._last_frame = None

    def __enter__(self) -> GdiCapture:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
