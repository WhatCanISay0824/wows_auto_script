"""运行服务包（M0，GUI 设计文档 3.2 / 13.1）。

- :mod:`src.runtime.messages`：控制命令、生命周期、快照、事件的纯数据类型（无 Tk/ctypes）；
- :mod:`src.runtime.service`：``RuntimeService`` 运行服务（生命周期/锁/队列/快照/统一清理）；
- :mod:`src.runtime.runner`：运行时装配与主循环（原 ``src/main.py`` 的实现迁入）；
- :mod:`src.runtime.plan`：启动选项解析、船只名称解析与生效值；
- :mod:`src.runtime.capabilities`：能力开关（未修复/未实机验证的能力显式关闭）。

``src/main.py`` 只保留 CLI 薄壳；GUI 只通过 ``RuntimeService`` 与 ``messages`` 交互。
"""

from __future__ import annotations

__all__ = ["capabilities", "messages", "plan", "runner", "service"]
