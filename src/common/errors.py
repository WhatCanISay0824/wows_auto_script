"""项目异常类型。

原则（详细设计 2.3 / 13.1）：
- 感知失败不伪装成正常观察，用异常抛出，由状态机进入异常恢复；
- 配置错误在启动阶段一次性收集，以退出码 2 结束；
- 输入/截屏不可用时进入暂停，不静默继续。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class WowsScriptError(Exception):
    """项目内所有可预期异常的基类。"""


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """配置/地图包校验问题（详细设计 3.5 / 4.7）。"""

    level: Severity
    code: str
    location: str
    message: str

    def format(self) -> str:
        return f"[{self.level.value.upper()}] {self.location}: {self.message} ({self.code})"

    @property
    def is_error(self) -> bool:
        return self.level is Severity.ERROR


class ConfigError(WowsScriptError):
    """配置校验失败（详细设计 3.5：一次性列出所有问题，退出码 2）。"""

    def __init__(self, issues: list[ValidationIssue]) -> None:
        self.issues = list(issues)
        errors = [issue for issue in self.issues if issue.is_error]
        warnings = [issue for issue in self.issues if not issue.is_error]
        lines = [issue.format() for issue in errors] + [issue.format() for issue in warnings]
        super().__init__("配置校验失败：\n" + "\n".join(lines))

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if not issue.is_error]


class WindowUnavailable(WowsScriptError):
    """游戏窗口不存在、已最小化或客户区无效。"""


class CaptureUnavailable(WowsScriptError):
    """截屏失败或画面不可用（黑屏、尺寸为零等）。"""


class InputUnavailable(WowsScriptError):
    """SendInput 调用失败或输入后端不可用。"""


class MapMatchError(WowsScriptError):
    """地图识别失败。"""


class RouteUnavailable(WowsScriptError):
    """没有可用航线（详细设计 4.6）。"""


class StepFailure(WowsScriptError):
    """步骤序列执行失败（详细设计 10.1）。"""

    def __init__(self, message: str, step_index: int, step: str) -> None:
        self.step_index = step_index
        self.step = step
        super().__init__(f"步骤 #{step_index} ({step}) 失败: {message}")


@dataclass(frozen=True, slots=True)
class StopRequest:
    """停止/暂停请求（详细设计 13.5）。"""

    reason: str
    detail: str = ""
    hook: str = field(default="")

    def format(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


class SafetyStop(WowsScriptError):
    """安全停止（紧急停止、会话上限、看门狗等）。"""

    def __init__(self, request: StopRequest) -> None:
        self.request = request
        super().__init__(request.format())


class DatabaseError(WowsScriptError):
    """数据库操作失败。"""
