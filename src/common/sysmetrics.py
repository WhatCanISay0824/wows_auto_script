"""Windows 系统指标采样（GUI 性能面板与过载保护技术层，设计 §6 / §6.2）。

口径与约束：

- 只用标准库 ``ctypes`` 调用 Windows API，**不引入 psutil 等第三方依赖**；
- 返回不可变 :class:`SystemSample`：全机 CPU%、物理内存总量/可用、Commit 上限/余量、
  脚本进程 PrivateUsage / 进程 CPU%、采样单调/墙上时间、``GetTickCount64`` 与
  ``QueryUnbiasedInterruptTime`` 原始值；
- **首拍 CPU 丢弃**（没有时间差）：第一次 ``sample()`` 的 ``cpu_pct``/``script_cpu_pct`` 为 None；
- 单个 API 失败只降级为对应字段 ``None`` 并记录 ``errors``，不让进程崩溃；关键内存 API
  全部失败时 ``available=False``（界面显示“不可用”，不把缺失值当健康）；
- 本机事实（2026-10-31 实测，代码按这些口径写死结构体大小）：
  ``GetSystemTimes`` 单位 100ns 且 kernel 含 idle；``GetPerformanceInfo`` 在 ``psapi.dll``，
  ``PERFORMANCE_INFORMATION.cb`` 必须等于 104；页大小取 ``PERFORMANCE_INFORMATION.PageSize``；
  ``MEMORYSTATUSEX`` 64 字节；``PROCESS_MEMORY_COUNTERS_EX`` 80 字节；
  ``ullAvailPageFile`` 与 ``(CommitLimit-CommitTotal)*PageSize`` 可互相校验；
  ``time.monotonic()`` 在本机含待机，睡眠识别由 ``GetTickCount64 - QueryUnbiasedInterruptTime``
  的增量完成（策略层实现）。

非 Windows 平台：``SystemMetricsSource()`` 抛 :class:`MetricsUnavailable`；工厂
:func:`create_metrics_source` 返回 ``available=False`` 的 :class:`UnavailableMetricsSource`，
调用方无需按平台分支。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .errors import WowsScriptError

logger = logging.getLogger(__name__)

_IS_WINDOWS = sys.platform == "win32"
_BYTES_PER_MB = 1024.0 * 1024.0
#: GetPerformanceInfo 结构体大小（x64，cb 不为 104 时 API 返回 ERROR_BAD_LENGTH=24）
PERFORMANCE_INFORMATION_SIZE = 104
#: MEMORYSTATUSEX 结构体大小
MEMORYSTATUSEX_SIZE = 64
#: PROCESS_MEMORY_COUNTERS_EX 结构体大小
PROCESS_MEMORY_COUNTERS_EX_SIZE = 80
#: GetPerformanceInfo 不可用时的页大小回退值
FALLBACK_PAGE_SIZE = 4096


class MetricsUnavailable(WowsScriptError):
    """系统指标采样不可用（非 Windows、DLL/API 加载失败等）。"""


# ---------------------------------------------------------------------------
# ctypes 结构与类型别名（定义不使用 ctypes.wintypes，便于跨平台做尺寸测试）
# ---------------------------------------------------------------------------

_DWORD = ctypes.c_uint32
_SIZE_T = ctypes.c_size_t
_ULONGLONG = ctypes.c_uint64


class FILETIME(ctypes.Structure):
    """Win32 FILETIME：两个 32 位分量拼成 64 位计数（单位取决于 API）。"""

    _fields_ = (
        ("dwLowDateTime", _DWORD),
        ("dwHighDateTime", _DWORD),
    )


class PERFORMANCE_INFORMATION(ctypes.Structure):
    """psapi.h PERFORMANCE_INFORMATION（x64 大小 104，cb 必须等于该值）。"""

    _fields_ = (
        ("cb", _DWORD),
        ("CommitTotal", _SIZE_T),
        ("CommitLimit", _SIZE_T),
        ("CommitPeak", _SIZE_T),
        ("PhysicalTotal", _SIZE_T),
        ("PhysicalAvailable", _SIZE_T),
        ("SystemCache", _SIZE_T),
        ("KernelTotal", _SIZE_T),
        ("KernelPaged", _SIZE_T),
        ("KernelNonpaged", _SIZE_T),
        ("PageSize", _SIZE_T),
        ("HandleCount", _DWORD),
        ("ProcessCount", _DWORD),
        ("ThreadCount", _DWORD),
    )


class MEMORYSTATUSEX(ctypes.Structure):
    """Windows.h MEMORYSTATUSEX（64 字节）。"""

    _fields_ = (
        ("dwLength", _DWORD),
        ("dwMemoryLoad", _DWORD),
        ("ullTotalPhys", _ULONGLONG),
        ("ullAvailPhys", _ULONGLONG),
        ("ullTotalPageFile", _ULONGLONG),
        ("ullAvailPageFile", _ULONGLONG),
        ("ullTotalVirtual", _ULONGLONG),
        ("ullAvailVirtual", _ULONGLONG),
        ("ullAvailExtendedVirtual", _ULONGLONG),
    )


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    """psapi.h PROCESS_MEMORY_COUNTERS_EX（80 字节，含 PrivateUsage）。"""

    _fields_ = (
        ("cb", _DWORD),
        ("PageFaultCount", _DWORD),
        ("PeakWorkingSetSize", _SIZE_T),
        ("WorkingSetSize", _SIZE_T),
        ("QuotaPeakPagedPoolUsage", _SIZE_T),
        ("QuotaPagedPoolUsage", _SIZE_T),
        ("QuotaPeakNonPagedPoolUsage", _SIZE_T),
        ("QuotaNonPagedPoolUsage", _SIZE_T),
        ("PagefileUsage", _SIZE_T),
        ("PeakPagefileUsage", _SIZE_T),
        ("PrivateUsage", _SIZE_T),
    )


# 尺寸一致性（导入时校验，防止有人改结构体字段后静默出错）
assert ctypes.sizeof(PERFORMANCE_INFORMATION) == PERFORMANCE_INFORMATION_SIZE  # noqa: S101
assert ctypes.sizeof(MEMORYSTATUSEX) == MEMORYSTATUSEX_SIZE  # noqa: S101
assert ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX) == PROCESS_MEMORY_COUNTERS_EX_SIZE  # noqa: S101


def _filetime_value(value: FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


@dataclass(frozen=True, slots=True)
class SystemSample:
    """一次系统指标采样（不可变；不可用字段为 None，不填 0 冒充健康）。"""

    #: 采样时刻（``time.monotonic()`` 口径，秒）
    mono: float
    #: 采样时刻（``time.time()`` 口径，秒）
    wall: float
    cpu_pct: float | None = None
    mem_total_mb: float | None = None
    mem_available_mb: float | None = None
    commit_limit_mb: float | None = None
    commit_available_mb: float | None = None
    script_private_mb: float | None = None
    script_cpu_pct: float | None = None
    #: GetTickCount64 原始值（毫秒，含待机）
    gtc64_ms: int | None = None
    #: QueryUnbiasedInterruptTime 原始值（100ns，不含待机/休眠）
    unbiased_100ns: int | None = None
    #: 关键指标是否可用；False 时界面显示“不可用”，不得当作健康
    available: bool = True
    #: 致命错误（API 失败导致字段缺失）
    errors: tuple[str, ...] = ()
    #: 非致命说明（交叉校验差异等）
    notes: tuple[str, ...] = ()

    @property
    def sleep_offset_s(self) -> float | None:
        """``GetTickCount64 - QueryUnbiasedInterruptTime`` 偏移（秒，含待机）。

        相邻样本该值的增量 ≈ 期间待机/休眠秒数；供过载策略识别睡眠与挂起。
        """
        if self.gtc64_ms is None or self.unbiased_100ns is None:
            return None
        return self.gtc64_ms / 1000.0 - self.unbiased_100ns / 1e7

    @property
    def unavailable_reason(self) -> str | None:
        if self.available:
            return None
        if self.errors:
            return "；".join(self.errors)
        return "系统指标不可用"


@dataclass(frozen=True, slots=True)
class MetricsCheck:
    """启动自检结果（64 位、结构体尺寸、CommitLimit≥物理内存、GPI/GMS 交叉校验）。"""

    ok: bool
    issues: tuple[str, ...] = ()
    detail: str = ""


@runtime_checkable
class MetricsSampler(Protocol):
    """指标采样器协议：过载监控器只依赖该协议（测试注入假样本序列）。"""

    @property
    def available(self) -> bool:
        """采样器是否可用（不可用时 ``sample()`` 返回 ``available=False`` 的样本）。"""

    def sample(self, now: float | None = None) -> SystemSample:
        """采样一次；``now`` 可用于注入单调时间（默认 ``time.monotonic()``）。"""


class UnavailableMetricsSource:
    """不可用采样器：非 Windows 或 API 加载失败时的降级实现（不抛异常）。"""

    def __init__(self, reason: str) -> None:
        self._reason = str(reason or "系统指标不可用")

    @property
    def available(self) -> bool:
        return False

    @property
    def reason(self) -> str:
        return self._reason

    def sample(self, now: float | None = None) -> SystemSample:
        moment = time.monotonic() if now is None else float(now)
        return SystemSample(
            mono=moment,
            wall=time.time(),
            available=False,
            errors=(self._reason,),
        )


class SystemMetricsSource:
    """ctypes 实现：GetSystemTimes / GetPerformanceInfo / GlobalMemoryStatusEx /
    GetProcessMemoryInfo / GetProcessTimes / GetTickCount64 / QueryUnbiasedInterruptTime。

    构造即加载 kernel32/psapi 并绑定函数签名；任何绑定失败抛 :class:`MetricsUnavailable`。
    ``sample()`` 内部逐项容错，绝不向调用方抛异常（除编程错误外的极端情况也会被兜住）。
    """

    def __init__(self) -> None:
        if not _IS_WINDOWS:
            raise MetricsUnavailable("当前平台不是 Windows，无法读取 Windows 系统指标")
        self._lock = threading.Lock()
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._psapi = ctypes.WinDLL("psapi", use_last_error=True)
        self._bind()
        # 首拍 CPU 需要时间差：保存上一拍计数（None 表示下一拍仍无 CPU 值）
        self._prev_cpu: tuple[int, int] | None = None  # (idle, total)
        self._prev_proc: tuple[int, float] | None = None  # (进程 100ns 总时间, mono)
        self._current_process = int(self._kernel32.GetCurrentProcess())

    @property
    def available(self) -> bool:
        return True

    def _bind(self) -> None:
        kernel32 = self._kernel32
        psapi = self._psapi
        filetime_ptr = ctypes.POINTER(FILETIME)
        kernel32.GetSystemTimes.argtypes = [filetime_ptr, filetime_ptr, filetime_ptr]
        kernel32.GetSystemTimes.restype = ctypes.c_int
        kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
        kernel32.GlobalMemoryStatusEx.restype = ctypes.c_int
        kernel32.GetProcessTimes.argtypes = [
            ctypes.c_void_p,
            filetime_ptr,
            filetime_ptr,
            filetime_ptr,
            filetime_ptr,
        ]
        kernel32.GetProcessTimes.restype = ctypes.c_int
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetTickCount64.argtypes = []
        kernel32.GetTickCount64.restype = _ULONGLONG
        kernel32.QueryUnbiasedInterruptTime.argtypes = [ctypes.POINTER(_ULONGLONG)]
        kernel32.QueryUnbiasedInterruptTime.restype = ctypes.c_int
        psapi.GetPerformanceInfo.argtypes = [ctypes.POINTER(PERFORMANCE_INFORMATION), _DWORD]
        psapi.GetPerformanceInfo.restype = ctypes.c_int
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
            _DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int

    # -- 单项读取（失败抛 OSError，由 sample() 统一降级） ---------------------
    def _system_times(self) -> tuple[int, int, int]:
        idle = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not self._kernel32.GetSystemTimes(
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        ):
            raise OSError(ctypes.get_last_error(), "GetSystemTimes 失败")
        return _filetime_value(idle), _filetime_value(kernel), _filetime_value(user)

    def _memory_status(self) -> MEMORYSTATUSEX:
        status = MEMORYSTATUSEX()
        status.dwLength = MEMORYSTATUSEX_SIZE
        if not self._kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            raise OSError(ctypes.get_last_error(), "GlobalMemoryStatusEx 失败")
        return status

    def _performance_info(self) -> PERFORMANCE_INFORMATION:
        info = PERFORMANCE_INFORMATION()
        info.cb = PERFORMANCE_INFORMATION_SIZE
        if not self._psapi.GetPerformanceInfo(ctypes.byref(info), PERFORMANCE_INFORMATION_SIZE):
            raise OSError(ctypes.get_last_error(), "GetPerformanceInfo 失败（cb 必须为 104）")
        return info

    def _process_memory(self) -> int:
        counters = PROCESS_MEMORY_COUNTERS_EX()
        counters.cb = PROCESS_MEMORY_COUNTERS_EX_SIZE
        if not self._psapi.GetProcessMemoryInfo(
            self._current_process, ctypes.byref(counters), PROCESS_MEMORY_COUNTERS_EX_SIZE
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo 失败")
        return int(counters.PrivateUsage)

    def _process_times(self) -> int:
        creation = FILETIME()
        exit_time = FILETIME()
        kernel = FILETIME()
        user = FILETIME()
        if not self._kernel32.GetProcessTimes(
            self._current_process,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessTimes 失败")
        return _filetime_value(kernel) + _filetime_value(user)

    def _sleep_counters(self) -> tuple[int | None, int | None]:
        gtc64: int | None
        unbiased: int | None
        try:
            gtc64 = int(self._kernel32.GetTickCount64())
        except (OSError, TypeError):
            gtc64 = None
        value = _ULONGLONG()
        try:
            ok = bool(self._kernel32.QueryUnbiasedInterruptTime(ctypes.byref(value)))
            unbiased = int(value.value) if ok else None
        except (OSError, TypeError):
            unbiased = None
        return gtc64, unbiased

    # -- 对外采样 ------------------------------------------------------------
    def sample(self, now: float | None = None) -> SystemSample:
        moment = time.monotonic() if now is None else float(now)
        wall = time.time()
        errors: list[str] = []
        notes: list[str] = []
        cpu_pct: float | None = None
        mem_total_mb: float | None = None
        mem_available_mb: float | None = None
        commit_limit_mb: float | None = None
        commit_available_mb: float | None = None
        script_private_mb: float | None = None
        script_cpu_pct: float | None = None
        gtc64_ms, unbiased_100ns = self._sleep_counters()

        try:
            with self._lock:
                # --- 全机 CPU（首拍丢弃） ---
                try:
                    idle, kernel, user = self._system_times()
                    total = kernel + user
                    if self._prev_cpu is not None:
                        prev_idle, prev_total = self._prev_cpu
                        delta_total = total - prev_total
                        delta_idle = idle - prev_idle
                        if delta_total > 0:
                            busy = delta_total - delta_idle
                            cpu_pct = _clamp(100.0 * busy / delta_total, 0.0, 100.0)
                    self._prev_cpu = (idle, total)
                except OSError as exc:
                    errors.append(f"CPU 采样失败: {exc}")

                # --- 物理内存 / 页面文件（GlobalMemoryStatusEx） ---
                gms = None
                try:
                    gms = self._memory_status()
                    mem_total_mb = gms.ullTotalPhys / _BYTES_PER_MB
                    mem_available_mb = gms.ullAvailPhys / _BYTES_PER_MB
                except OSError as exc:
                    errors.append(f"物理内存采样失败: {exc}")

                # --- Commit（GetPerformanceInfo 优先，GMS 兜底并交叉校验） ---
                gpi_commit_limit_mb: float | None = None
                gpi_commit_available_mb: float | None = None
                try:
                    info = self._performance_info()
                    page = int(info.PageSize) or FALLBACK_PAGE_SIZE
                    gpi_commit_limit_mb = int(info.CommitLimit) * page / _BYTES_PER_MB
                    gpi_commit_available_mb = (
                        (int(info.CommitLimit) - int(info.CommitTotal)) * page / _BYTES_PER_MB
                    )
                    commit_limit_mb = gpi_commit_limit_mb
                    commit_available_mb = gpi_commit_available_mb
                except OSError as exc:
                    errors.append(f"Commit 采样失败: {exc}")

                gms_commit_limit_mb: float | None = None
                gms_commit_available_mb: float | None = None
                if gms is not None:
                    gms_commit_limit_mb = gms.ullTotalPageFile / _BYTES_PER_MB
                    gms_commit_available_mb = gms.ullAvailPageFile / _BYTES_PER_MB
                    if commit_limit_mb is None:
                        commit_limit_mb = gms_commit_limit_mb
                    if commit_available_mb is None:
                        commit_available_mb = gms_commit_available_mb
                if gpi_commit_available_mb is not None and gms_commit_available_mb is not None:
                    tolerance = max(64.0, gpi_commit_available_mb * 0.02)
                    if abs(gpi_commit_available_mb - gms_commit_available_mb) > tolerance:
                        notes.append(
                            "GetPerformanceInfo 与 ullAvailPageFile 交叉校验差异: "
                            f"{gpi_commit_available_mb:.0f}MB vs {gms_commit_available_mb:.0f}MB"
                        )

                # --- 脚本进程内存 / CPU ---
                try:
                    script_private_mb = self._process_memory() / _BYTES_PER_MB
                except OSError as exc:
                    errors.append(f"进程内存采样失败: {exc}")

                try:
                    proc_time = self._process_times()
                    if self._prev_proc is not None:
                        prev_proc_time, prev_mono = self._prev_proc
                        elapsed = moment - prev_mono
                        delta = proc_time - prev_proc_time
                        if elapsed > 0 and delta >= 0:
                            script_cpu_pct = 100.0 * (delta / 1e7) / elapsed
                            script_cpu_pct = max(0.0, script_cpu_pct)
                    self._prev_proc = (proc_time, moment)
                except OSError as exc:
                    errors.append(f"进程 CPU 采样失败: {exc}")
        except Exception as exc:  # pragma: no cover - 兜底，避免采样线程静默死掉
            logger.exception("系统指标采样出现未预期异常")
            errors.append(f"采样异常: {exc}")

        available = mem_total_mb is not None or commit_available_mb is not None
        sample = SystemSample(
            mono=moment,
            wall=wall,
            cpu_pct=cpu_pct,
            mem_total_mb=mem_total_mb,
            mem_available_mb=mem_available_mb,
            commit_limit_mb=commit_limit_mb,
            commit_available_mb=commit_available_mb,
            script_private_mb=script_private_mb,
            script_cpu_pct=script_cpu_pct,
            gtc64_ms=gtc64_ms,
            unbiased_100ns=unbiased_100ns,
            available=available,
            errors=tuple(errors),
            notes=tuple(notes),
        )
        if not available:
            logger.warning("系统指标不可用: %s", "；".join(errors) or "未知原因")
        return sample

    # -- 启动自检 ------------------------------------------------------------
    def check(self) -> MetricsCheck:
        """启动自检：位数、结构体尺寸、关键 API、CommitLimit≥物理内存、GPI/GMS 交叉校验。"""
        issues: list[str] = []
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            issues.append("当前 Python 进程不是 64 位，性能采样的结构体口径不可靠")
        if ctypes.sizeof(PERFORMANCE_INFORMATION) != PERFORMANCE_INFORMATION_SIZE:
            issues.append("PERFORMANCE_INFORMATION 结构体尺寸与实测口径（104）不一致")
        sample = self.sample()
        if not sample.available:
            issues.extend(sample.errors or ("系统指标不可用",))
            return MetricsCheck(ok=False, issues=tuple(issues), detail="系统指标采样不可用")
        if sample.mem_total_mb is None:
            issues.append("物理内存总量不可用")
        if sample.commit_limit_mb is None:
            issues.append("Commit 上限不可用")
        elif sample.mem_total_mb is not None and sample.commit_limit_mb < sample.mem_total_mb:
            issues.append(
                f"CommitLimit={sample.commit_limit_mb:.0f}MB 小于物理内存 "
                f"{sample.mem_total_mb:.0f}MB（异常）"
            )
        issues.extend(sample.notes)
        if sample.errors:
            issues.extend(sample.errors)
        detail = (
            f"cpu={_fmt(sample.cpu_pct)}% phys={_fmt(sample.mem_available_mb)}/"
            f"{_fmt(sample.mem_total_mb)}MB commit={_fmt(sample.commit_available_mb)}/"
            f"{_fmt(sample.commit_limit_mb)}MB script={_fmt(sample.script_private_mb)}MB"
        )
        return MetricsCheck(ok=not issues, issues=tuple(issues), detail=detail)


def _clamp(value: float, low: float, high: float) -> float:
    return low if value < low else high if value > high else value


def _fmt(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}"


def create_metrics_source() -> MetricsSampler:
    """创建默认采样器：Windows 返回真实采样器，其它平台/失败返回不可用降级实现。"""
    if not _IS_WINDOWS:
        return UnavailableMetricsSource("当前平台不是 Windows，性能采样不可用（不视为健康）")
    try:
        return SystemMetricsSource()
    except MetricsUnavailable as exc:  # pragma: no cover - 依赖本机 DLL 环境
        logger.warning("初始化 Windows 指标采样失败，降级为不可用: %s", exc)
        return UnavailableMetricsSource(str(exc))
    except OSError as exc:  # pragma: no cover - DLL 加载失败
        logger.warning("加载 kernel32/psapi 失败，降级为不可用: %s", exc)
        return UnavailableMetricsSource(f"加载 Windows 指标 DLL 失败: {exc}")


__all__ = [
    "FALLBACK_PAGE_SIZE",
    "FILETIME",
    "MEMORYSTATUSEX",
    "MEMORYSTATUSEX_SIZE",
    "MetricsCheck",
    "MetricsSampler",
    "MetricsUnavailable",
    "PERFORMANCE_INFORMATION",
    "PERFORMANCE_INFORMATION_SIZE",
    "PROCESS_MEMORY_COUNTERS_EX",
    "PROCESS_MEMORY_COUNTERS_EX_SIZE",
    "SystemMetricsSource",
    "SystemSample",
    "UnavailableMetricsSource",
    "create_metrics_source",
]
