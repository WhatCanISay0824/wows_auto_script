"""配置校验命令行工具（详细设计 3.5 / 4.7 / 12.1）。

用法：:

    python -m src.tools.validate --root .
    python -m src.tools.validate --profile cn360 --ship placeholder --json

行为：
- 校验全局配置、客户端配置、按键映射、船只配置与全部地图包；
- 有问题时打印 ``[ERROR]``/``[WARNING]`` 清单（含文件与字段路径）；
- 有 error 时退出码 2，否则（含 warning）退出码 0；工具本身不抛异常退出。
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from ..config.validate import ValidationReport, validate_project

EXIT_OK = 0
EXIT_INVALID = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.tools.validate",
        description="校验 wows_script 的全局配置、客户端配置、船只与地图包",
    )
    parser.add_argument(
        "--root",
        default=".",
        help="项目根目录（包含 config.yaml 的目录），默认当前目录",
    )
    parser.add_argument(
        "--profile",
        default="cn360",
        help="客户端配置编号，对应 profiles/<client_id>/，默认 cn360",
    )
    parser.add_argument(
        "--ship",
        default=None,
        help="覆盖全局配置中的船只编号（对应 profiles/<client>/ships/<ship_id>/）",
    )
    parser.add_argument(
        "--account",
        default=None,
        help="按本地账号别名校验（accounts.yaml 的 alias，只做配置与统计隔离，不涉及登录）",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="以 JSON 输出校验结果（便于工具链集成）",
    )
    return parser


def _print_report(report: ValidationReport, root: Path, profile_id: str) -> None:
    errors = report.errors
    warnings = report.warnings
    print(f"配置校验: root={root} profile={profile_id}")
    for issue in errors:
        print(issue.format())
    if errors and warnings:
        print()
    for issue in warnings:
        print(issue.format())
    print()
    print(report.summary())
    if errors:
        print("存在错误，进程不会进入主循环（退出码 2）")
    else:
        print("校验通过")


def _report_to_json(report: ValidationReport, root: Path, profile_id: str) -> str:
    payload = {
        "root": str(root),
        "profile": profile_id,
        "errors": [
            {"code": issue.code, "location": issue.location, "message": issue.message}
            for issue in report.errors
        ],
        "warnings": [
            {"code": issue.code, "location": issue.location, "message": issue.message}
            for issue in report.warnings
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main(argv: list[str] | None = None) -> int:
    """校验入口：返回进程退出码（0 通过 / 2 有 error）。"""
    args = _build_parser().parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"[ERROR] 项目根目录不存在: {root}", file=sys.stderr)
        return EXIT_INVALID
    try:
        report = validate_project(root, args.profile, ship_id=args.ship, account=args.account)
    except Exception:  # noqa: BLE001 - 工具需要给出清晰错误而不是崩溃
        print("[ERROR] 校验过程中发生未预期异常：", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)
        return EXIT_INVALID
    if args.json:
        print(_report_to_json(report, root, args.profile))
    else:
        _print_report(report, root, args.profile)
    return EXIT_OK if report.ok else EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
