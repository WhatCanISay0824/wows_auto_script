"""GUI 启动入口（``py -3.14 -m src.gui.main``）。

职责：

1. 在创建 Tk 根窗口**之前**调用 ``set_dpi_awareness()``（2.5 第 3 条）；
2. 创建根窗口与 :class:`src.gui.app.App`；
3. 在 ``main()`` 内**延迟导入** ``src.runtime.service.RuntimeService``——
   M0 尚未落地或导入失败时打印/显示中文错误并返回非 0，而不是崩溃
   （2.3 / 2.6）；
4. 服务与 Tk 任何一方不可用时都不影响命令行 ``python -m src.main``。

本模块顶层不导入 tkinter / 运行服务，保证 ``from src.gui.main import main``
在没有桌面会话的环境也能被导入（AST 测试守护延迟导入）。
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

EXIT_OK = 0
EXIT_TK_MISSING = 2
EXIT_NO_DESKTOP = 3
EXIT_SERVICE_UNAVAILABLE = 4


def _print_error(message: str) -> None:
    print(f"无法启动图形界面：{message}", file=sys.stderr)


def _show_error_dialog(root, message: str) -> None:
    try:
        from tkinter import messagebox

        messagebox.showerror("无法启动图形界面", message, parent=root)
    except Exception:  # noqa: BLE001 - 对话框不可用时只保留终端输出
        pass


def _import_tk():
    try:
        import tkinter as tk
    except ImportError as exc:  # Tk 组件缺失
        _print_error(f"未找到 Tk 组件（可能需要安装带 Tk 的 Python 发行版）：{exc}")
        return None
    return tk


def _apply_dpi_awareness() -> None:
    try:
        from src.common.window import set_dpi_awareness
    except Exception:  # noqa: BLE001 - DPI 辅助不可用不阻塞 GUI
        return
    try:
        set_dpi_awareness()
    except Exception:  # noqa: BLE001
        return


def _load_service(root, project_root: Path):
    """延迟导入真实运行服务；失败返回 ``None`` 并显示中文错误。"""

    try:
        from src.runtime.service import RuntimeService
    except ImportError as exc:
        message = (
            "运行服务尚未就绪（M0 尚未完成或导入失败）："
            f"{exc}。命令行入口 `py -3.14 -m src.main` 不受影响。"
        )
        _print_error(message)
        _show_error_dialog(root, message)
        return None
    try:
        return RuntimeService(project_root)
    except Exception as exc:  # noqa: BLE001 - 装配失败给出中文原因，不崩溃
        message = f"运行服务初始化失败：{type(exc).__name__}: {exc}"
        _print_error(message)
        _show_error_dialog(root, message)
        return None


def main(argv: Sequence[str] | None = None) -> int:
    del argv  # M1 不解析额外参数；配置由运行服务负责

    tk = _import_tk()
    if tk is None:
        return EXIT_TK_MISSING

    _apply_dpi_awareness()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        _print_error(f"无法创建窗口（可能没有桌面会话）：{exc}")
        return EXIT_NO_DESKTOP

    project_root = Path.cwd()
    try:
        from src.gui.app import App
    except Exception as exc:  # noqa: BLE001 - 界面模块自身错误也要给中文提示
        message = f"界面模块加载失败：{type(exc).__name__}: {exc}"
        _print_error(message)
        _show_error_dialog(root, message)
        root.destroy()
        return EXIT_SERVICE_UNAVAILABLE

    service = _load_service(root, project_root)
    if service is None:
        root.destroy()
        return EXIT_SERVICE_UNAVAILABLE

    root.withdraw()
    try:
        App(root, service)
    except Exception as exc:  # noqa: BLE001
        message = f"界面初始化失败：{type(exc).__name__}: {exc}"
        _print_error(message)
        _show_error_dialog(root, message)
        root.destroy()
        return EXIT_SERVICE_UNAVAILABLE
    root.deiconify()
    root.mainloop()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
