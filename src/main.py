"""程序入口（详细设计 2.1 / B.1）。

用法::

    python -m src.main --profile cn360                    # 使用全局 ship.ship_id
    python -m src.main --profile cn360 --ship <ship_id>
    python -m src.main --account alt1                     # 指定本地账号档案
    python -m src.main --rotate                           # 按 accounts.yaml 轮换账号
    python -m src.main --dry-run                          # 只校验配置，不启动

M0 之后本文件是 CLI 薄壳（GUI 设计文档 3.5 / 13.1）：

- 运行时装配、主循环、统一清理已迁入 ``src/runtime/{runner,plan}.py``；
- 为兼容既有测试/行为，本模块保留原私有名字（``_build_runtime``/``_run_session``/
  ``_teardown``/``_load_app``/... 的导入别名或薄包装），CLI 参数、输出与退出码不变；
- 账号轮换：``RuntimeService`` 已实现服务级轮换（``WAIT_ACCOUNT``/``confirm_account_switch``/
  ``abort_rotation``）；CLI 为保持既有“控制台提示 + 回车继续”的交互，仍使用
  ``_run_accounts`` 循环，但复用同一 ``AccountRotator`` 语义（上限/计数/advance），
  由 ``tests/test_runtime_rotation.py::test_cli_rotate_uses_same_account_rotator_semantics`` 对照。

安全口径（用户扩展需求）：
- 账号只使用本地别名（accounts.yaml），不保存真实账号/密码/令牌，不自动登录；
- 轮换到下一个账号时脚本停下来提示使用者手动切号并登录，按回车后继续；
- 同一时间只运行一个账号、一个游戏窗口。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from .common.errors import ConfigError
from .common.single_instance import SingleInstanceLock
from .config.loader import load_project
from .config.validate import validate_app
from .runtime.plan import (
    _list_ships,
    _load_accounts,
    _load_app,
    _resolve_ship,
    with_log_level,
)
from .runtime.runner import (
    Runtime,
    _build_runtime,
    _cleanup_partial_runtime,
    _ensure_game_foreground,
    _EventBridge,
    _make_safety_event_handler,
    _reset_input_after_failure,
    _run_session,
    _sync_safety_controller,
    _teardown,
    reset_window_cache,
)
from .state_machine.accounts import AccountRotator

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_RUNTIME_ERROR = 1
#: 已有另一个脚本实例在运行（ISS-003）
EXIT_INSTANCE_RUNNING = 3


# ---------------------------------------------------------------------------
# 账号轮换循环（M3 迁入 RuntimeService，当前 CLI 保留原行为）
# ---------------------------------------------------------------------------


def _run_accounts(
    args: argparse.Namespace,
    root: Path,
    aliases: list[str | None],
    rotator: AccountRotator | None,
) -> int:
    """逐账号运行；每个账号的运行时构建/会话/清理由 try/finally 绑定（ISS-008）。"""
    rotating = bool(rotator is not None and len(aliases) > 1)
    no_battle_streak = 0
    try:
        while True:
            alias = rotator.current.alias if rotator is not None else aliases[0] if len(aliases) == 1 else None
            app = _load_app(root, args.profile, args.ship, alias)
            problems = validate_app(app)
            fatal = [issue for issue in problems if issue.is_error]
            if fatal:
                _print_issues(fatal)
                print(f"账号 {alias or '(默认)'} 的配置校验失败，停止", file=sys.stderr)
                return EXIT_CONFIG_ERROR
            if args.log_level:
                app = with_log_level(app, args.log_level)
            reset_window_cache()
            runtime: Runtime | None = None
            reason, battles = "runtime_error", 0
            try:
                runtime = _build_runtime(app)
                if app.account is not None:
                    runtime.logger.info("当前账号档案: %s", app.account.alias)
                limit = args.max_battles
                if limit is None and rotator is not None:
                    limit = rotator.current_limit()
                reason, battles = _run_session(runtime, max_battles=limit)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                # ISS-008：异常原因交给调用方显示，finally 保证统一清理完成
                logging.getLogger(__name__).exception("会话异常终止: %s", exc)
                print(f"会话异常终止：{exc}", file=sys.stderr)
                reason, battles = "runtime_error", 0
            finally:
                if runtime is not None:
                    _teardown(runtime)
            print(f"账号 {alias or '(默认)'} 会话结束：原因={reason} 场次={battles}")
            if rotator is not None:
                rotator.record_battle(alias)
            if not rotating:
                return EXIT_OK if reason not in ("game_crash", "runtime_error") else EXIT_RUNTIME_ERROR
            if reason in ("emergency_stop", "manual") or str(reason).startswith("overload_"):
                return EXIT_OK
            if battles == 0:
                no_battle_streak += 1
                if no_battle_streak >= max(1, len(aliases)):
                    print("所有账号本次都无法开始战斗（可能已达每日上限/不在允许时段），退出。")
                    return EXIT_OK
            else:
                no_battle_streak = 0
            rotator.advance()
            next_alias = rotator.current.alias
            print()
            print("=" * 72)
            print(f"请手动切换到账号「{next_alias}」：")
            print("  1. 在游戏客户端退出当前账号并登录目标账号（脚本不会代登录、不保存密码）；")
            print("  2. 确认已回到港口界面、船只与空装备符合配置；")
            print("  3. 回到本控制台按回车继续。")
            print("=" * 72)
            try:
                input("切换完成后按回车继续（Ctrl+C 退出）...")
            except EOFError:
                return EXIT_OK
    except KeyboardInterrupt:
        print("用户中断，退出。")
        return EXIT_OK


def _print_issues(issues: list[Any]) -> None:
    for issue in issues:
        print(issue.format(), file=sys.stderr)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="战舰世界联合作战（PvE）本地托管脚本（截屏+模拟键鼠，仅本地技术研究）",
    )
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    parser.add_argument("--profile", default="cn360", help="客户端配置 id（默认 cn360）")
    parser.add_argument("--ship", default=None, help="会话固定船只 id（默认用 config.yaml 的 ship.ship_id）")
    parser.add_argument(
        "--ship-name",
        default=None,
        help="按船只显示名称选择会话固定船只（如 列宁格勒）；脚本会在港口船只轮播条里找并选中它",
    )
    parser.add_argument("--list-ships", action="store_true", help="列出当前客户端配置下的船只档案后退出")
    parser.add_argument("--account", default=None, help="账号档案别名（accounts.yaml 中的 alias）")
    parser.add_argument("--rotate", action="store_true", help="按 accounts.yaml 轮换启用账号（需手动切号）")
    parser.add_argument("--max-battles", type=int, default=None, help="单账号本次会话最多场次（覆盖配置）")
    parser.add_argument("--dry-run", action="store_true", help="只校验配置并打印摘要，不启动脚本")
    parser.add_argument("--log-level", default=None, help="覆盖文本日志级别，例如 DEBUG")
    parser.add_argument("--version", action="version", version="wows_script 0.1.0")
    return parser


def _print_instance_holder(holder: dict[str, Any] | None) -> None:
    holder = holder or {}
    print(
        f"已有另一个脚本实例在运行（PID={holder.get('pid', '未知')}，"
        f"启动于 {holder.get('started_at', '未知')}），本次不启动游戏控制。",
        file=sys.stderr,
    )
    print(
        "如确认没有实例，请在任务管理器结束残留的 python/src.main 进程；"
        "命令行可用：taskkill /PID <pid> /T /F（结束进程树，避免只杀 py 启动器）。",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not (root / "config.yaml").is_file():
        print(f"找不到配置文件: {root / 'config.yaml'}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    if args.list_ships:
        ships = _list_ships(root, args.profile)
        if not ships:
            print(f"客户端 {args.profile} 下没有船只档案（profiles/{args.profile}/ships/<id>/）", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        for ship_id, display_name in ships:
            print(f"{ship_id}\t{display_name}")
        return EXIT_OK

    # --ship-name 解析为 ship_id（脚本随后在船只轮播条里用卡片模板找并选中该船）
    try:
        ship_id = _resolve_ship(root, args.profile, args.ship, args.ship_name)
    except ConfigError as exc:
        _print_issues(exc.errors + exc.warnings)
        return EXIT_CONFIG_ERROR
    args.ship = ship_id

    # 账号档案
    accounts = _load_accounts(root) if (args.rotate or args.account) else None
    rotator: AccountRotator | None = None
    aliases: list[str | None]
    if args.rotate:
        if accounts is None:
            print("当前版本暂不支持 --rotate（账号支持任务尚未完成）", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        rotator = AccountRotator(accounts)
        if not rotator.enabled:
            print(
                "accounts.yaml 中没有启用的账号：请至少将一个 accounts[].enabled 设为 true，"
                "并把 rotation.enabled 设为 true",
                file=sys.stderr,
            )
            return EXIT_CONFIG_ERROR
        aliases = [account.alias for account in rotator.plan()]
    elif args.account:
        if accounts is None:
            print("当前版本暂不支持 --account（账号支持任务尚未完成）", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        if accounts.get(args.account) is None:
            print(f"accounts.yaml 中找不到账号别名: {args.account}", file=sys.stderr)
            return EXIT_CONFIG_ERROR
        aliases = [args.account]
    else:
        aliases = [None]

    # 校验（以第一个账号/默认 profile 为基准；后续每个账号运行前会再校验一次）
    try:
        loaded = load_project(root, args.profile, ship_id=args.ship)
    except ConfigError as exc:
        _print_issues(exc.errors + exc.warnings)
        return EXIT_CONFIG_ERROR
    issues = list(loaded.issues)
    try:
        app0 = _load_app(root, args.profile, args.ship, aliases[0])
    except ConfigError as exc:
        _print_issues(exc.errors + exc.warnings)
        return EXIT_CONFIG_ERROR
    issues.extend(validate_app(app0))
    errors = [issue for issue in issues if issue.is_error]
    warnings = [issue for issue in issues if not issue.is_error]
    _print_issues(errors)
    if warnings:
        print(f"校验警告 {len(warnings)} 条：", file=sys.stderr)
        _print_issues(warnings)
    if errors:
        print(f"配置校验失败：{len(errors)} 个错误，{len(warnings)} 个警告（退出码 2）", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    print(
        f"配置校验通过：profile={app0.profile.client_id} ship={app0.ship.ship_id or '(未配置)'} "
        f"maps={len(app0.maps)} 账号={app0.account_alias or '(未指定)'}"
    )
    if args.dry_run:
        print("--dry-run：未启动脚本。")
        return EXIT_OK

    # ISS-003：跨进程单实例锁（命令行与 GUI 共用），防止两个实例同时发送键鼠
    lock = SingleInstanceLock(root / app0.global_config.paths.data_dir)
    if not lock.acquire():
        _print_instance_holder(lock.holder)
        return EXIT_INSTANCE_RUNNING
    try:
        return _run_accounts(args, root, aliases, rotator)
    finally:
        lock.release()


#: 兼容旧测试/调用方的模块级私有名字（M0 迁移后由 ``src.runtime`` 提供实现）。
__all__ = [
    "EXIT_CONFIG_ERROR",
    "EXIT_INSTANCE_RUNNING",
    "EXIT_OK",
    "EXIT_RUNTIME_ERROR",
    "Runtime",
    "_EventBridge",
    "_build_runtime",
    "_cleanup_partial_runtime",
    "_ensure_game_foreground",
    "_list_ships",
    "_load_accounts",
    "_load_app",
    "_make_safety_event_handler",
    "_reset_input_after_failure",
    "_resolve_ship",
    "_run_accounts",
    "_run_session",
    "_sync_safety_controller",
    "_teardown",
    "build_parser",
    "main",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
