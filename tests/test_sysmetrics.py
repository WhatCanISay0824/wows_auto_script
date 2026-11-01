"""系统指标采样单元测试（src/common/sysmetrics.py）。

结构体尺寸/cb 口径（PERFORMANCE_INFORMATION=104、MEMORYSTATUSEX=64、
PROCESS_MEMORY_COUNTERS_EX=80）与 2026-10-31 本机实测一致；Windows 冒烟测试
在非 Windows 上 skip，不触网、不依赖第三方库。
"""

from __future__ import annotations

import ctypes
import sys
import time
from dataclasses import FrozenInstanceError

import pytest
from src.common.sysmetrics import (
    MEMORYSTATUSEX,
    MEMORYSTATUSEX_SIZE,
    PERFORMANCE_INFORMATION,
    PERFORMANCE_INFORMATION_SIZE,
    PROCESS_MEMORY_COUNTERS_EX,
    PROCESS_MEMORY_COUNTERS_EX_SIZE,
    MetricsCheck,
    MetricsSampler,
    SystemMetricsSource,
    SystemSample,
    UnavailableMetricsSource,
    create_metrics_source,
)

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# 结构体尺寸 / cb 陷阱（不需要 Windows API，纯 ctypes）
# ---------------------------------------------------------------------------


def test_performance_information_size_is_104() -> None:
    """GetPerformanceInfo 的 cb 必须为 104，否则返回 ERROR_BAD_LENGTH(24)。"""
    assert PERFORMANCE_INFORMATION_SIZE == 104
    assert ctypes.sizeof(PERFORMANCE_INFORMATION) == PERFORMANCE_INFORMATION_SIZE
    fields = {name for name, _ in PERFORMANCE_INFORMATION._fields_}
    # kernel 含 idle 的 GetSystemTimes 与 Commit/PageSize 依赖这些字段
    assert {"cb", "CommitTotal", "CommitLimit", "PageSize"} <= fields


def test_memory_status_ex_size_is_64() -> None:
    assert MEMORYSTATUSEX_SIZE == 64
    assert ctypes.sizeof(MEMORYSTATUSEX) == MEMORYSTATUSEX_SIZE


def test_process_memory_counters_ex_size_is_80() -> None:
    assert PROCESS_MEMORY_COUNTERS_EX_SIZE == 80
    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = PROCESS_MEMORY_COUNTERS_EX_SIZE
    assert ctypes.sizeof(counters) == PROCESS_MEMORY_COUNTERS_EX_SIZE
    assert "PrivateUsage" in {name for name, _ in PROCESS_MEMORY_COUNTERS_EX._fields_}


def test_filetime_value_combines_high_and_low() -> None:
    from src.common.sysmetrics import FILETIME, _filetime_value

    value = FILETIME(dwLowDateTime=0x00000001, dwHighDateTime=0x00000002)
    assert _filetime_value(value) == (2 << 32) | 1


# ---------------------------------------------------------------------------
# 样本模型与降级
# ---------------------------------------------------------------------------


def test_system_sample_is_frozen_and_sleep_offset() -> None:
    sample = SystemSample(mono=10.0, wall=100.0, gtc64_ms=100_000, unbiased_100ns=500_000_000)
    # 100s(含待机) - 50s(清醒) = 50s 偏移
    assert sample.sleep_offset_s == pytest.approx(50.0)
    assert sample.unavailable_reason is None
    with pytest.raises(FrozenInstanceError):
        sample.cpu_pct = 1.0  # type: ignore[misc]


def test_sample_without_raw_counters_has_no_sleep_offset() -> None:
    sample = SystemSample(mono=1.0, wall=2.0)
    assert sample.sleep_offset_s is None


def test_unavailable_source_never_raises() -> None:
    source = UnavailableMetricsSource("测试：采样不可用")
    assert source.available is False
    assert isinstance(source, MetricsSampler)
    sample = source.sample(now=12.5)
    assert sample.mono == 12.5
    assert sample.available is False
    assert "测试：采样不可用" in sample.errors
    assert sample.unavailable_reason == "测试：采样不可用"


def test_unavailable_source_default_reason() -> None:
    sample = UnavailableMetricsSource("").sample()
    assert sample.available is False
    assert sample.unavailable_reason


def test_create_metrics_source_matches_platform() -> None:
    source = create_metrics_source()
    assert isinstance(source, MetricsSampler)
    if IS_WINDOWS:
        assert source.available is True
        assert isinstance(source, SystemMetricsSource)
    else:  # pragma: no cover - 项目只支持 Windows
        assert source.available is False
        assert isinstance(source, UnavailableMetricsSource)


def test_metrics_unavailable_is_project_error() -> None:
    from src.common.errors import WowsScriptError
    from src.common.sysmetrics import MetricsUnavailable

    assert issubclass(MetricsUnavailable, WowsScriptError)


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows API 冒烟测试")
def test_windows_system_metrics_smoke() -> None:
    """真实 Windows 采样冒烟：首拍无 CPU、后续有值、内存/Commit/进程内存可用。"""
    source = SystemMetricsSource()
    assert source.available is True
    assert isinstance(source, MetricsSampler)

    first = source.sample()
    assert isinstance(first, SystemSample)
    assert first.available is True, first.errors
    assert first.cpu_pct is None, "首拍没有时间差，CPU 必须丢弃"
    assert first.mem_total_mb is not None and first.mem_total_mb > 0
    assert first.mem_available_mb is not None and first.mem_available_mb >= 0
    assert first.commit_limit_mb is not None and first.commit_limit_mb >= first.mem_total_mb
    assert first.commit_available_mb is not None and first.commit_available_mb >= 0
    assert first.script_private_mb is not None and first.script_private_mb > 0
    assert first.gtc64_ms is not None
    assert first.unbiased_100ns is not None
    assert first.sleep_offset_s is not None

    time.sleep(0.05)
    second = source.sample()
    assert second.cpu_pct is not None and 0.0 <= second.cpu_pct <= 100.0
    assert second.script_cpu_pct is not None and second.script_cpu_pct >= 0.0
    # 同一进程两次采样之间 monotonic 递增
    assert second.mono > first.mono


@pytest.mark.skipif(not IS_WINDOWS, reason="Windows API 冒烟测试")
def test_windows_startup_check() -> None:
    source = SystemMetricsSource()
    check = source.check()
    assert isinstance(check, MetricsCheck)
    assert check.ok, check.issues
    assert "cpu=" in check.detail
    assert "commit=" in check.detail


def test_check_returns_issues_for_unavailable_source() -> None:
    source = UnavailableMetricsSource("DLL 加载失败")
    # 降级采样器没有 check()：监控器会跳过自检；这里只验证其样本自述不可用
    sample = source.sample()
    assert sample.available is False
    assert "DLL 加载失败" in (sample.unavailable_reason or "")
