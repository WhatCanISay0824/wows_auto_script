"""GDI 截屏的纯函数与假句柄测试。

测试不截任何真实窗口：GDI 底层调用全部 monkeypatch 成假句柄/假返回值，
图像内容用 ``numpy`` 噪点模拟；这样能验证尺寸校验、DC/位图复用、黑屏兜底、
帧序号与耗时统计，而不依赖游戏窗口。
"""

from __future__ import annotations

import ctypes
from typing import Any

import numpy as np
import pytest
from src.common.clock import FakeClock
from src.common.errors import CaptureUnavailable, WindowUnavailable
from src.common.types import Rect, WindowInfo
from src.perception import capture as capture_mod
from src.perception.capture import (
    Frame,
    GdiCapture,
    GdiFrameSource,
    bgra_top_down_to_bgr,
    clip_rect,
    crop_bgr,
    dib_section_to_bgr,
    frame_invalid_reason,
    frame_is_valid,
    is_fullscreen_client_rect,
    rect_within,
)

# 测试客户端尺寸（远小于真实 2560x1600，但不能小于最小帧尺寸）
CLIENT_RECT = Rect(10, 20, 64, 48)
#: 无边框全屏测试用客户区（与虚拟屏幕一致）
FULL_RECT = Rect(0, 0, 2560, 1600)
HWND = 0x1234


def _noise(width: int, height: int, seed: int = 3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, size=(height, width, 3), dtype=np.uint8)


def _window_info(
    rect: Rect = CLIENT_RECT,
    *,
    hwnd: int = HWND,
    foreground: bool = True,
    minimized: bool = False,
    valid: bool = True,
) -> WindowInfo:
    return WindowInfo(
        hwnd=hwnd,
        title="战舰世界",
        process_name="WorldOfWarships.exe",
        client_rect=rect,
        foreground=foreground,
        minimized=minimized,
        valid=valid,
    )


class FakeLocator:
    """可注入的假窗口定位器。"""

    def __init__(self, info: WindowInfo | None) -> None:
        self._info = info
        self.find_calls = 0
        self.refresh_calls = 0
        self.error: Exception | None = None

    def find(self) -> WindowInfo | None:
        self.find_calls += 1
        if self.error is not None:
            raise self.error
        return self._info

    def refresh(self, hwnd: int) -> WindowInfo | None:
        self.refresh_calls += 1
        if self.error is not None:
            raise self.error
        if self._info is None or self._info.hwnd != hwnd:
            return None
        return self._info


class GrabOnlySource:
    """只有 grab 的假图像来源（用于验证“无 PrintWindow 兜底”路径）。"""

    def __init__(self, image: np.ndarray) -> None:
        self.image = image
        self.grab_calls: list[tuple[int, Rect]] = []
        self.closed = 0

    def grab(self, hwnd: int, rect: Rect) -> np.ndarray:
        self.grab_calls.append((hwnd, rect))
        return self.image

    def close(self) -> None:
        self.closed += 1


class FakeSource:
    """带 PrintWindow / 桌面截屏兜底能力的假图像来源。"""

    def __init__(
        self,
        image: np.ndarray,
        *,
        print_image: np.ndarray | None = None,
        screen_image: np.ndarray | None = None,
    ) -> None:
        self.image = image
        self.print_image = print_image
        self.screen_image = screen_image
        self.grab_calls: list[tuple[int, Rect]] = []
        self.print_calls: list[tuple[int, Rect]] = []
        self.screen_calls: list[Rect] = []
        self.closed = 0
        self.print_error: Exception | None = None
        self.screen_error: Exception | None = None

    def grab(self, hwnd: int, rect: Rect) -> np.ndarray:
        self.grab_calls.append((hwnd, rect))
        return self.image

    def grab_print_window(self, hwnd: int, rect: Rect) -> np.ndarray:
        self.print_calls.append((hwnd, rect))
        if self.print_error is not None:
            raise self.print_error
        assert self.print_image is not None
        return self.print_image

    def grab_screen(self, rect: Rect) -> np.ndarray:
        self.screen_calls.append(rect)
        if self.screen_error is not None:
            raise self.screen_error
        assert self.screen_image is not None
        return self.screen_image

    def close(self) -> None:
        self.closed += 1


def _make_capture(
    locator: Any,
    source: Any,
    *,
    clock: FakeClock | None = None,
    require_foreground: bool = False,
    budget_ms: float = 300.0,
    perf: Any = None,
) -> GdiCapture:
    return GdiCapture(
        locator,
        clock=clock or FakeClock(start=5.0),
        source=source,
        require_foreground=require_foreground,
        capture_budget_ms=budget_ms,
        perf_counter=perf,
    )


# ---------------------------------------------------------------------------
# 纯函数：DIB 缓冲区 → BGR
# ---------------------------------------------------------------------------


def test_bgra_top_down_to_bgr_converts_and_drops_alpha() -> None:
    pixels = [
        (10, 20, 30, 255),
        (40, 50, 60, 255),
        (70, 80, 90, 255),
        (100, 110, 120, 255),
    ]
    buffer = bytearray()
    for blue, green, red, alpha in pixels:
        buffer += bytes((blue, green, red, alpha))
    result = bgra_top_down_to_bgr(buffer, 2, 2)
    expected = np.array(
        [
            [(10, 20, 30), (40, 50, 60)],
            [(70, 80, 90), (100, 110, 120)],
        ],
        dtype=np.uint8,
    )
    assert result.dtype == np.uint8
    np.testing.assert_array_equal(result, expected)


def test_bgra_top_down_to_bgr_honours_stride() -> None:
    # 每行 2 像素（8 字节）＋4 字节对齐填充
    row0 = bytes((1, 2, 3, 255, 4, 5, 6, 255)) + b"\xee" * 4
    row1 = bytes((7, 8, 9, 255, 10, 11, 12, 255)) + b"\xee" * 4
    result = bgra_top_down_to_bgr(row0 + row1, 2, 2, stride=12)
    np.testing.assert_array_equal(
        result,
        np.array(
            [
                [(1, 2, 3), (4, 5, 6)],
                [(7, 8, 9), (10, 11, 12)],
            ],
            dtype=np.uint8,
        ),
    )


@pytest.mark.parametrize(
    ("buffer", "width", "height", "stride"),
    [
        (b"\x00" * 8, 2, 2, None),  # 长度不足
        (b"\x00" * 8, 2, 2, 4),  # stride 小于每行字节数
        (b"", 0, 2, None),  # 尺寸非法
        (b"\x00" * 16, 2, 0, None),
    ],
)
def test_bgra_top_down_to_bgr_rejects_invalid_input(
    buffer: bytes, width: int, height: int, stride: int | None
) -> None:
    with pytest.raises(ValueError):
        bgra_top_down_to_bgr(buffer, width, height, stride=stride)


def test_dib_section_to_bgr_reads_memory_pointer() -> None:
    width, height = 3, 2
    data = _noise(width, height, seed=8)
    # 按 BGRA 组装内存
    pixels = np.empty((height, width, 4), dtype=np.uint8)
    pixels[:, :, :3] = data
    pixels[:, :, 3] = 255
    raw = pixels.tobytes()
    ctypes_buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    result = dib_section_to_bgr(ctypes.addressof(ctypes_buffer), width, height)
    np.testing.assert_array_equal(result, data)


def test_dib_section_to_bgr_rejects_null_pointer() -> None:
    with pytest.raises(ValueError):
        dib_section_to_bgr(0, 4, 4)


# ---------------------------------------------------------------------------
# 纯函数：矩形裁剪校验
# ---------------------------------------------------------------------------


def test_rect_within() -> None:
    assert rect_within(Rect(1, 2, 3, 4), 10, 10)
    assert not rect_within(Rect(-1, 2, 3, 4), 10, 10)
    assert not rect_within(Rect(8, 8, 3, 3), 10, 10)
    assert not rect_within(Rect(0, 0, 0, 0), 10, 10)


def test_clip_rect_clamps_and_rejects_outside() -> None:
    assert clip_rect(Rect(1, 2, 3, 4), 10, 10) == Rect(1, 2, 3, 4)
    assert clip_rect(Rect(-2, 3, 10, 5), 8, 8) == Rect(0, 3, 8, 5)
    with pytest.raises(ValueError):
        clip_rect(Rect(20, 20, 5, 5), 8, 8)
    with pytest.raises(ValueError):
        clip_rect(Rect(3, 3, 0, 0), 8, 8)


def test_crop_bgr() -> None:
    image = np.arange(4 * 6 * 3, dtype=np.uint8).reshape(4, 6, 3)
    cropped = crop_bgr(image, Rect(2, 1, 3, 2))
    np.testing.assert_array_equal(cropped, image[1:3, 2:5])
    copied = crop_bgr(image, Rect(2, 1, 3, 2), copy=True)
    np.testing.assert_array_equal(copied, cropped)
    assert not np.shares_memory(copied, image)
    # 部分越界时裁剪到图像边界
    np.testing.assert_array_equal(crop_bgr(image, Rect(4, 2, 10, 10)), image[2:, 4:])
    with pytest.raises(ValueError):
        crop_bgr(image, Rect(10, 10, 3, 3))
    with pytest.raises(ValueError):
        crop_bgr(np.zeros((4, 6), dtype=np.uint8), Rect(0, 0, 3, 3))


# ---------------------------------------------------------------------------
# 纯函数：帧有效性
# ---------------------------------------------------------------------------


def test_frame_is_valid_accepts_textured_frame() -> None:
    image = _noise(80, 60)
    assert frame_is_valid(image)
    assert frame_invalid_reason(image) is None


def test_frame_invalid_reason_detects_black_and_empty() -> None:
    black = np.zeros((60, 80, 3), dtype=np.uint8)
    assert not frame_is_valid(black)
    assert "黑屏" in (frame_invalid_reason(black) or "")

    assert frame_invalid_reason(None) is not None
    assert frame_invalid_reason(np.zeros((0, 0, 3), dtype=np.uint8)) is not None
    assert frame_invalid_reason(np.zeros((60, 80), dtype=np.uint8)) is not None
    assert frame_invalid_reason(np.zeros((60, 80, 4), dtype=np.uint8)) is not None
    assert frame_invalid_reason(np.zeros((4, 4, 3), dtype=np.uint8)) is not None
    assert frame_invalid_reason(np.zeros((60, 80, 3), dtype=np.float32)) is not None
    assert frame_invalid_reason([1, 2, 3]) is not None  # type: ignore[arg-type]


def test_frame_invalid_reason_uses_black_thresholds() -> None:
    dark = np.full((60, 80, 3), 5, dtype=np.uint8)
    assert frame_invalid_reason(dark) is None  # 均值 5 > 默认阈值 2
    assert frame_invalid_reason(dark, mean_threshold=10.0) is not None


def test_frame_crop_helper() -> None:
    image = _noise(64, 48)
    frame = Frame(image=image, timestamp=1.5, window_rect=Rect(0, 0, 64, 48), frame_id=3)
    assert frame.width == 64
    assert frame.height == 48
    assert frame.shape == (48, 64, 3)
    np.testing.assert_array_equal(frame.crop(Rect(8, 6, 16, 12)), image[6:18, 8:24])


# ---------------------------------------------------------------------------
# 截屏器：窗口校验 / 帧序号 / 缓存
# ---------------------------------------------------------------------------


def test_capture_returns_frame_and_reuses_window_handle() -> None:
    locator = FakeLocator(_window_info())
    source = FakeSource(_noise(CLIENT_RECT.w, CLIENT_RECT.h))
    clock = FakeClock(start=5.0)
    capture = _make_capture(locator, source, clock=clock)

    first = capture.capture()
    assert first.image is source.image
    assert first.window_rect == CLIENT_RECT
    assert first.timestamp == 5.0
    assert first.frame_id == 1
    assert capture.last_frame is first
    assert capture.frame_id == 1
    assert source.grab_calls == [(HWND, CLIENT_RECT)]

    clock.advance(0.5)
    second = capture.capture()
    assert second.frame_id == 2
    assert second.timestamp == 5.5
    assert capture.last_frame is second
    # 第二次走 refresh 刷新句柄，不再整窗查找
    assert locator.find_calls == 1
    assert locator.refresh_calls == 1

    stats = capture.stats
    assert (stats.attempts, stats.successes, stats.failures) == (2, 2, 0)
    assert stats.last_ms >= 0.0


def test_capture_raises_when_window_missing() -> None:
    locator = FakeLocator(None)
    source = GrabOnlySource(_noise(CLIENT_RECT.w, CLIENT_RECT.h))
    capture = _make_capture(locator, source)
    with pytest.raises(CaptureUnavailable, match="找不到游戏窗口"):
        capture.capture()
    assert source.grab_calls == []
    assert capture.last_frame is None
    assert capture.stats.failures == 1


def test_capture_wraps_window_locator_error() -> None:
    locator = FakeLocator(None)
    locator.error = WindowUnavailable("非 Windows 平台")
    capture = _make_capture(locator, GrabOnlySource(_noise(8, 8)))
    with pytest.raises(CaptureUnavailable, match="窗口查找失败"):
        capture.capture()


@pytest.mark.parametrize(
    ("info", "match_text"),
    [
        (_window_info(valid=False), "失效"),
        (_window_info(minimized=True), "最小化"),
        (_window_info(Rect(0, 0, 4, 4)), "尺寸无效"),
        (_window_info(foreground=False), "前台"),
    ],
)
def test_capture_rejects_bad_window(info: WindowInfo, match_text: str) -> None:
    source = GrabOnlySource(_noise(CLIENT_RECT.w, CLIENT_RECT.h))
    capture = _make_capture(FakeLocator(info), source, require_foreground=True)
    with pytest.raises(CaptureUnavailable, match=match_text):
        capture.capture()
    assert source.grab_calls == []


def test_capture_allows_unfocused_when_not_required() -> None:
    source = GrabOnlySource(_noise(CLIENT_RECT.w, CLIENT_RECT.h))
    capture = _make_capture(FakeLocator(_window_info(foreground=False)), source, require_foreground=False)
    assert capture.capture().frame_id == 1


def test_capture_rejects_shape_mismatch() -> None:
    source = GrabOnlySource(_noise(32, 32))
    capture = _make_capture(FakeLocator(_window_info()), source)
    with pytest.raises(CaptureUnavailable, match="尺寸不一致"):
        capture.capture()
    assert capture.stats.failures == 1


def test_capture_rejects_black_frame_without_fallback() -> None:
    black = np.zeros((CLIENT_RECT.h, CLIENT_RECT.w, 3), dtype=np.uint8)
    capture = _make_capture(FakeLocator(_window_info()), GrabOnlySource(black))
    with pytest.raises(CaptureUnavailable, match="黑屏"):
        capture.capture()
    assert capture.stats.failures == 1


def test_capture_uses_print_window_fallback() -> None:
    black = np.zeros((CLIENT_RECT.h, CLIENT_RECT.w, 3), dtype=np.uint8)
    recovered = _noise(CLIENT_RECT.w, CLIENT_RECT.h, seed=9)
    source = FakeSource(black, print_image=recovered)
    capture = _make_capture(FakeLocator(_window_info()), source)
    frame = capture.capture()
    assert frame.image is recovered
    assert source.print_calls == [(HWND, CLIENT_RECT)]
    assert capture.stats.successes == 1


def test_capture_remembers_print_window_mode_after_fallback() -> None:
    black = np.zeros((CLIENT_RECT.h, CLIENT_RECT.w, 3), dtype=np.uint8)
    recovered = _noise(CLIENT_RECT.w, CLIENT_RECT.h, seed=11)
    source = FakeSource(black, print_image=recovered)
    capture = _make_capture(FakeLocator(_window_info()), source)
    first = capture.capture()
    second = capture.capture()
    assert first.image is recovered
    assert second.image is recovered
    # 第二次不再先尝试黑屏的 GDI，直接走 PrintWindow
    assert source.grab_calls == [(HWND, CLIENT_RECT)]
    assert len(source.print_calls) == 2


def test_capture_rejects_black_fallback_result() -> None:
    black = np.zeros((CLIENT_RECT.h, CLIENT_RECT.w, 3), dtype=np.uint8)
    source = FakeSource(black, print_image=black.copy())
    capture = _make_capture(FakeLocator(_window_info()), source)
    with pytest.raises(CaptureUnavailable, match="PrintWindow 兜底仍不可用"):
        capture.capture()


def test_capture_wraps_print_window_error() -> None:
    black = np.zeros((CLIENT_RECT.h, CLIENT_RECT.w, 3), dtype=np.uint8)
    source = FakeSource(black, print_image=_noise(CLIENT_RECT.w, CLIENT_RECT.h))
    source.print_error = CaptureUnavailable("PrintWindow 失败")
    capture = _make_capture(FakeLocator(_window_info()), source)
    with pytest.raises(CaptureUnavailable, match="PrintWindow 兜底失败"):
        capture.capture()


def test_capture_can_disable_print_window_fallback() -> None:
    black = np.zeros((CLIENT_RECT.h, CLIENT_RECT.w, 3), dtype=np.uint8)
    source = FakeSource(black, print_image=_noise(CLIENT_RECT.w, CLIENT_RECT.h))
    capture = GdiCapture(
        FakeLocator(_window_info()),
        clock=FakeClock(),
        source=source,
        enable_print_window=False,
    )
    with pytest.raises(CaptureUnavailable, match="黑屏"):
        capture.capture()
    assert source.print_calls == []


# ---------------------------------------------------------------------------
# 桌面 DC 截屏（无边框全屏，2026-10-31 实机修复：PrintWindow 冻结帧）
# ---------------------------------------------------------------------------


def _patch_virtual_screen(monkeypatch: pytest.MonkeyPatch, rect: Rect = FULL_RECT) -> None:
    monkeypatch.setattr(capture_mod, "virtual_screen_rect", lambda: rect)


def test_is_fullscreen_client_rect() -> None:
    virtual = Rect(0, 0, 2560, 1600)
    assert is_fullscreen_client_rect(Rect(0, 0, 2560, 1600), virtual) is True
    # 原点有 2px 以内的取整误差仍算全屏
    assert is_fullscreen_client_rect(Rect(1, -1, 2560, 1600), virtual) is True
    assert is_fullscreen_client_rect(Rect(0, 0, 1920, 1080), virtual) is False
    assert is_fullscreen_client_rect(Rect(10, 20, 64, 48), virtual) is False
    # 多显示器：虚拟屏幕更宽时，只占主屏的窗口不算全屏
    assert is_fullscreen_client_rect(Rect(0, 0, 2560, 1600), Rect(0, 0, 4480, 1600)) is False


def test_capture_auto_uses_screen_dc_for_fullscreen(monkeypatch: pytest.MonkeyPatch) -> None:
    """无边框全屏 + 前台：优先桌面 DC 截屏，避免窗口 BitBlt 冻结帧。"""
    _patch_virtual_screen(monkeypatch)
    black = np.zeros((FULL_RECT.h, FULL_RECT.w, 3), dtype=np.uint8)
    screen_image = _noise(FULL_RECT.w, FULL_RECT.h, seed=11)
    source = FakeSource(black, screen_image=screen_image)
    capture = _make_capture(
        FakeLocator(_window_info(FULL_RECT, foreground=True)),
        source,
        require_foreground=True,
    )
    frame = capture.capture()
    assert source.grab_calls == []
    assert source.screen_calls == [FULL_RECT]
    assert source.print_calls == []
    np.testing.assert_array_equal(frame.image, screen_image)

    # 第二次固定桌面截屏，不再尝试窗口 BitBlt
    capture.capture()
    assert len(source.screen_calls) == 2


def test_capture_auto_fullscreen_prefers_screen_even_if_window_image_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归：窗口 BitBlt 可能返回“有效但冻结”的旧画面，不能靠画面校验发现，必须优先桌面截屏。"""
    _patch_virtual_screen(monkeypatch)
    frozen = _noise(FULL_RECT.w, FULL_RECT.h, seed=21)
    live = _noise(FULL_RECT.w, FULL_RECT.h, seed=22)
    source = FakeSource(frozen, screen_image=live)
    capture = _make_capture(
        FakeLocator(_window_info(FULL_RECT, foreground=True)),
        source,
        require_foreground=True,
    )
    frame = capture.capture()
    assert source.grab_calls == []
    np.testing.assert_array_equal(frame.image, live)


def test_capture_auto_falls_back_to_window_when_screen_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_virtual_screen(monkeypatch)
    window_image = _noise(FULL_RECT.w, FULL_RECT.h, seed=23)
    source = FakeSource(window_image)
    source.screen_error = CaptureUnavailable("桌面截屏失败")
    capture = _make_capture(
        FakeLocator(_window_info(FULL_RECT, foreground=True)),
        source,
        require_foreground=True,
    )
    frame = capture.capture()
    assert source.screen_calls == [FULL_RECT]
    assert len(source.grab_calls) == 1
    np.testing.assert_array_equal(frame.image, window_image)


def test_capture_window_method_keeps_print_window_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_virtual_screen(monkeypatch)
    black = np.zeros((FULL_RECT.h, FULL_RECT.w, 3), dtype=np.uint8)
    print_image = _noise(FULL_RECT.w, FULL_RECT.h, seed=12)
    source = FakeSource(black, print_image=print_image, screen_image=_noise(FULL_RECT.w, FULL_RECT.h, seed=13))
    capture = GdiCapture(
        FakeLocator(_window_info(FULL_RECT, foreground=True)),
        clock=FakeClock(),
        source=source,
        require_foreground=True,
        capture_method="window",
    )
    frame = capture.capture()
    assert source.screen_calls == []
    np.testing.assert_array_equal(frame.image, print_image)


def test_capture_screen_method_forces_screen_dc(monkeypatch: pytest.MonkeyPatch) -> None:
    screen_image = _noise(CLIENT_RECT.w, CLIENT_RECT.h, seed=14)
    source = FakeSource(_noise(CLIENT_RECT.w, CLIENT_RECT.h, seed=15), screen_image=screen_image)
    capture = GdiCapture(
        FakeLocator(_window_info()),
        clock=FakeClock(),
        source=source,
        capture_method="screen",
    )
    frame = capture.capture()
    assert source.grab_calls == []
    assert source.screen_calls == [CLIENT_RECT]
    np.testing.assert_array_equal(frame.image, screen_image)


def test_capture_auto_non_fullscreen_keeps_print_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_virtual_screen(monkeypatch)
    black = np.zeros((CLIENT_RECT.h, CLIENT_RECT.w, 3), dtype=np.uint8)
    print_image = _noise(CLIENT_RECT.w, CLIENT_RECT.h, seed=16)
    source = FakeSource(black, print_image=print_image, screen_image=_noise(CLIENT_RECT.w, CLIENT_RECT.h, seed=17))
    capture = _make_capture(FakeLocator(_window_info()), source, require_foreground=True)
    frame = capture.capture()
    assert source.screen_calls == []
    np.testing.assert_array_equal(frame.image, print_image)


def test_capture_invalid_screen_falls_back_to_print_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_virtual_screen(monkeypatch)
    black = np.zeros((FULL_RECT.h, FULL_RECT.w, 3), dtype=np.uint8)
    print_image = _noise(FULL_RECT.w, FULL_RECT.h, seed=18)
    source = FakeSource(black, print_image=print_image, screen_image=black.copy())
    capture = _make_capture(
        FakeLocator(_window_info(FULL_RECT, foreground=True)),
        source,
        require_foreground=True,
    )
    frame = capture.capture()
    # 桌面截屏在“优先尝试”和“黑屏兜底”阶段各尝试一次，均失败后走 PrintWindow
    assert source.screen_calls == [FULL_RECT, FULL_RECT]
    np.testing.assert_array_equal(frame.image, print_image)


def test_capture_rejects_unknown_capture_method() -> None:
    with pytest.raises(ValueError, match="capture_method"):
        GdiCapture(
            FakeLocator(_window_info()),
            clock=FakeClock(),
            source=FakeSource(_noise(CLIENT_RECT.w, CLIENT_RECT.h)),
            capture_method="bogus",
        )


def test_capture_records_timing_statistics() -> None:
    ticks = iter([0.0, 0.5, 0.5, 0.65])
    capture = _make_capture(
        FakeLocator(_window_info()),
        FakeSource(_noise(CLIENT_RECT.w, CLIENT_RECT.h)),
        perf=lambda: next(ticks),
    )
    capture.capture()  # 500 ms，超过预算
    capture.capture()  # 150 ms
    stats = capture.stats
    assert stats.last_ms == pytest.approx(150.0)
    assert stats.max_ms == pytest.approx(500.0)
    assert stats.avg_ms == pytest.approx(325.0)
    assert stats.p95_ms == pytest.approx(500.0)
    assert stats.slow_frames == 1


def test_capture_close_releases_source() -> None:
    source = FakeSource(_noise(CLIENT_RECT.w, CLIENT_RECT.h))
    capture = _make_capture(FakeLocator(_window_info()), source)
    capture.capture()
    assert capture.last_frame is not None
    capture.close()
    assert source.closed == 1
    assert capture.last_frame is None
    assert capture.dpi_awareness == "unknown"  # 假来源没有 dpi 信息


# ---------------------------------------------------------------------------
# GDI 图像来源：用假 GDI 函数验证 DC/位图生命周期
# ---------------------------------------------------------------------------


class _FakeGdi:
    """记录调用的假 GDI 层，替换 capture 模块里的低层函数。"""

    def __init__(self) -> None:
        self.get_dc_calls = 0
        self.release_dc_calls = 0
        self.create_dc_calls = 0
        self.create_dib_calls = 0
        self.select_calls = 0
        self.delete_object_calls: list[int] = []
        self.delete_dc_calls = 0
        self.bitblt_calls = 0
        self.bitblt_from_calls: list[tuple[int, int]] = []
        self.print_calls = 0
        self.bitblt_ok = True
        self.get_dc_ok = True
        self.print_ok = True
        self._next = iter(range(1000, 5000))

    def next_handle(self) -> int:
        return next(self._next)

    def get_dc(self, hwnd: int) -> int:
        self.get_dc_calls += 1
        return 777 if self.get_dc_ok else 0

    def release_dc(self, hwnd: int, dc: int) -> None:
        self.release_dc_calls += 1

    def create_compatible_dc(self, source_dc: int) -> int:
        self.create_dc_calls += 1
        return self.next_handle()

    def create_dib_section(self, source_dc: int, width: int, height: int) -> tuple[int, int]:
        self.create_dib_calls += 1
        return self.next_handle(), 0xB17B17

    def select_object(self, dc: int, obj: int) -> int:
        self.select_calls += 1
        return self.next_handle()

    def delete_object(self, obj: int) -> None:
        self.delete_object_calls.append(obj)

    def delete_dc(self, dc: int) -> None:
        self.delete_dc_calls += 1

    def bitblt(self, dst_dc: int, width: int, height: int, src_dc: int) -> bool:
        self.bitblt_calls += 1
        return self.bitblt_ok

    def bitblt_from(
        self, dst_dc: int, width: int, height: int, src_dc: int, src_x: int, src_y: int
    ) -> bool:
        self.bitblt_from_calls.append((src_x, src_y))
        return self.bitblt_ok

    def print_window(self, hwnd: int, hdc: int) -> bool:
        self.print_calls += 1
        return self.print_ok


def _patch_gdi(monkeypatch: pytest.MonkeyPatch) -> _FakeGdi:
    fake = _FakeGdi()
    monkeypatch.setattr(capture_mod, "_gdi_get_dc", fake.get_dc)
    monkeypatch.setattr(capture_mod, "_gdi_release_dc", fake.release_dc)
    monkeypatch.setattr(capture_mod, "_gdi_create_compatible_dc", fake.create_compatible_dc)
    monkeypatch.setattr(capture_mod, "_gdi_create_dib_section", fake.create_dib_section)
    monkeypatch.setattr(capture_mod, "_gdi_select_object", fake.select_object)
    monkeypatch.setattr(capture_mod, "_gdi_delete_object", fake.delete_object)
    monkeypatch.setattr(capture_mod, "_gdi_delete_dc", fake.delete_dc)
    monkeypatch.setattr(capture_mod, "_gdi_bitblt", fake.bitblt)
    monkeypatch.setattr(capture_mod, "_gdi_bitblt_from", fake.bitblt_from)
    monkeypatch.setattr(capture_mod, "_gdi_print_window", fake.print_window)
    monkeypatch.setattr(capture_mod, "is_dpi_aware", lambda: True)

    def fake_dib_to_bgr(bits: int, width: int, height: int, stride: int | None = None) -> np.ndarray:
        assert bits == 0xB17B17
        return _noise(width, height, seed=4)

    monkeypatch.setattr(capture_mod, "dib_section_to_bgr", fake_dib_to_bgr)
    return fake


def test_gdi_frame_source_reuses_resources_until_size_change(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_gdi(monkeypatch)
    source = GdiFrameSource()
    assert source.dpi_awareness == "already_aware"

    rect = Rect(0, 0, 64, 48)
    first = source.grab(HWND, rect)
    second = source.grab(HWND, rect)
    assert first.shape == (48, 64, 3)
    assert second.shape == (48, 64, 3)
    assert fake.create_dc_calls == 1
    assert fake.create_dib_calls == 1
    assert fake.bitblt_calls == 2
    assert fake.select_calls == 1

    bigger = Rect(0, 0, 100, 48)
    third = source.grab(HWND, bigger)
    assert third.shape == (48, 100, 3)
    # 尺寸变化：旧位图先 select 回来再删除，DC/位图重建
    assert fake.create_dc_calls == 2
    assert fake.create_dib_calls == 2
    assert fake.select_calls == 3
    assert len(fake.delete_object_calls) == 1
    assert fake.delete_dc_calls == 1

    source.close()
    assert len(fake.delete_object_calls) == 2
    assert fake.delete_dc_calls == 2
    source.close()  # 幂等：已释放后不再调用 GDI
    assert len(fake.delete_object_calls) == 2
    assert fake.delete_dc_calls == 2


def test_gdi_frame_source_raises_when_bitblt_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_gdi(monkeypatch)
    fake.bitblt_ok = False
    source = GdiFrameSource()
    with pytest.raises(CaptureUnavailable, match="BitBlt 失败"):
        source.grab(HWND, Rect(0, 0, 64, 48))
    source.close()


def test_gdi_frame_source_raises_when_get_dc_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_gdi(monkeypatch)
    fake.get_dc_ok = False
    source = GdiFrameSource()
    with pytest.raises(CaptureUnavailable, match="GetDC 失败"):
        source.grab(HWND, Rect(0, 0, 64, 48))
    assert fake.create_dc_calls == 0


def test_gdi_frame_source_print_window_path(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_gdi(monkeypatch)
    source = GdiFrameSource()
    rect = Rect(0, 0, 64, 48)
    image = source.grab_print_window(HWND, rect)
    assert image.shape == (48, 64, 3)
    assert fake.print_calls == 1

    fake.print_ok = False
    with pytest.raises(CaptureUnavailable, match="PrintWindow 失败"):
        source.grab_print_window(HWND, rect)
    source.close()


def test_gdi_frame_source_dpi_awareness_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_mod, "is_dpi_aware", lambda: False)
    monkeypatch.setattr(capture_mod, "set_dpi_awareness", lambda: "per_monitor_v2")
    assert GdiFrameSource().dpi_awareness == "per_monitor_v2"
    assert GdiFrameSource(dpi_aware=False).dpi_awareness == "skipped"


def test_gdi_frame_source_grab_screen_uses_desktop_dc(monkeypatch: pytest.MonkeyPatch) -> None:
    """桌面 DC 截屏从客户区在屏幕上的 (x,y) 复制，而不是窗口 DC 的 (0,0)。"""
    fake = _patch_gdi(monkeypatch)
    source = GdiFrameSource()
    rect = Rect(100, 50, 64, 48)
    image = source.grab_screen(rect)
    assert image.shape == (48, 64, 3)
    assert fake.bitblt_from_calls == [(100, 50)]
    assert fake.print_calls == 0
    source.close()


def test_gdi_frame_source_grab_screen_raises_when_bitblt_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _patch_gdi(monkeypatch)
    fake.bitblt_ok = False
    source = GdiFrameSource()
    with pytest.raises(CaptureUnavailable, match="桌面 BitBlt 失败"):
        source.grab_screen(Rect(0, 0, 64, 48))
    source.close()


def test_gdi_frame_source_uses_real_dib_conversion_without_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """不打桩 dib_section_to_bgr，验证 grab 把 DIB 内存真正读成 BGR。"""
    fake = _patch_gdi(monkeypatch)
    width, height = 3, 2
    pixels = _noise(width, height, seed=6)
    # 用一块真实内存模拟 DIB：填充 BGRA 数据到 bits 指针
    dib = np.empty((height, width, 4), dtype=np.uint8)
    dib[:, :, :3] = pixels
    dib[:, :, 3] = 255
    raw = dib.tobytes()
    memory = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    real_dib_to_bgr = dib_section_to_bgr  # 模块导入时绑定的真实实现

    def fake_create_dib(source_dc: int, w: int, h: int) -> tuple[int, int]:
        fake.create_dib_calls += 1
        assert (w, h) == (width, height)
        return 4242, ctypes.addressof(memory)

    monkeypatch.setattr(capture_mod, "_gdi_create_dib_section", fake_create_dib)
    monkeypatch.setattr(capture_mod, "dib_section_to_bgr", real_dib_to_bgr)
    source = GdiFrameSource()
    image = source.grab(HWND, Rect(0, 0, width, height))
    np.testing.assert_array_equal(image, pixels)
    source.close()
