"""启动选项解析与生效值（GUI 设计文档 4.3 / 4.4，M0 实施）。

本模块是从 ``src/main.py`` 迁入的只读解析逻辑，外加服务预检所需的“生效值计算”：

- ``_load_app`` / ``_load_accounts`` / ``_list_ships`` / ``_resolve_ship``：与迁入前行为一致
  （``src/main.py`` 保留同名兼容别名，旧测试通过 monkeypatch 注入）；
- ``preflight()``：``RuntimeService.start()`` 入口的快速校验（不加载完整运行时）；
- ``plan_preview()`` / ``resolve_start_plan()``：加载配置、应用账号覆盖后给出 ``EffectivePlan``。

约束：本模块只做读取与解析，不创建运行时对象、不写配置、不启动线程。
"""

from __future__ import annotations

import inspect
import sqlite3
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

from ..common.errors import ConfigError, Severity, ValidationIssue
from ..config.loader import load_project, load_ship
from ..config.models import AccountsConfig, AppConfig
from ..config.validate import validate_app
from ..state_machine.accounts import AccountRotator
from .messages import (
    EffectivePlan,
    PlanResult,
    StartOptions,
    ValidationIssueView,
)

#: 默认客户端配置 id（与 CLI ``--profile`` 默认值一致）
DEFAULT_PROFILE_ID = "cn360"
#: 允许的日志级别（GUI 设计文档 4.3）
LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARNING", "ERROR"})


# ---------------------------------------------------------------------------
# 问题映射
# ---------------------------------------------------------------------------


def issue_view(issue: ValidationIssue) -> ValidationIssueView:
    """把 ``src/config/validate.py`` 的 ValidationIssue 映射为只读视图。"""
    return ValidationIssueView(
        severity=issue.level.value,
        code=issue.code,
        location=issue.location,
        message=issue.message,
    )


def issue_views(issues: list[ValidationIssue] | tuple[ValidationIssue, ...]) -> tuple[ValidationIssueView, ...]:
    return tuple(issue_view(issue) for issue in issues)


def issue(code: str, location: str, message: str) -> ValidationIssue:
    """构造一条 error 级校验问题（预检/解析失败用）。"""
    return ValidationIssue(Severity.ERROR, code, location, message)


def _issue(message: str) -> ValidationIssue:  # 兼容 main.py 迁移前的私有辅助
    return issue(
        "account_unsupported",
        "src.main",
        message,
    )


# ---------------------------------------------------------------------------
# 配置/账号/船只加载（从 src/main.py 迁入，行为保持一致）
# ---------------------------------------------------------------------------


def load_app_for_account(root: Path, profile: str, ship: str | None, alias: str | None) -> AppConfig:
    """严格加载并校验配置（原 ``main._load_app``；账号参数不支持时给出明确问题）。"""
    kwargs: dict[str, Any] = {"ship_id": ship}
    parameters = inspect.signature(load_project).parameters
    if alias:
        if "account" not in parameters:
            raise ConfigError(
                [
                    _issue(
                        "账号功能尚未就绪：src/config/loader.py 的 load_app 还没有 account 参数。"
                        "请等待账号支持任务（T7）完成，或先不使用 --account/--rotate。"
                    )
                ]
            )
        kwargs["account"] = alias
    project = load_project(root, profile, **kwargs)
    issues = list(project.issues) + validate_app(project.config)
    errors = [item for item in issues if item.is_error]
    if errors:
        raise ConfigError(issues)
    return project.config


def load_accounts_or_none(root: Path) -> AccountsConfig | None:
    """加载账号档案；loader 不提供该接口时返回 None（原 ``main._load_accounts``）。"""
    try:
        from ..config.loader import load_accounts
    except ImportError:  # pragma: no cover - 账号支持任务未完成时的向后兼容分支
        return None
    try:
        return load_accounts(root)
    except TypeError:  # pragma: no cover - loader 旧签名
        return None


def list_ships(root: Path, profile_id: str) -> list[tuple[str, str]]:
    """列出客户端配置下所有船只档案的 (ship_id, display_name)。"""
    ships_dir = root / "profiles" / profile_id / "ships"
    ships: list[tuple[str, str]] = []
    for ship_yaml in sorted(ships_dir.glob("*/ship.yaml")):
        try:
            ship = load_ship(ship_yaml)
        except Exception:  # 单个船只配置损坏不影响其它船只的列出
            continue
        if ship.ship_id:
            ships.append((ship.ship_id, ship.display_name))
    return ships


def resolve_ship(
    root: Path, profile_id: str, ship_id: str | None, ship_name: str | None
) -> str | None:
    """把 ``--ship-name``（显示名称/编号）解析为 ship_id；未指定名称时原样返回 ship_id。"""
    if not ship_name:
        return ship_id
    ships = list_ships(root, profile_id)
    if not ships:
        raise ConfigError(
            [issue("profile_not_found", f"profiles/{profile_id}/ships", f"客户端 {profile_id} 下没有任何船只档案（profiles/{profile_id}/ships/<id>/）")]
        )
    exact = [item for item in ships if item[1] == ship_name or item[0] == ship_name]
    partial = [item for item in ships if ship_name in item[1]]
    matches = exact or partial
    if not matches:
        available = "、".join(f"{sid}({name})" for sid, name in ships)
        raise ConfigError([issue("ship_unresolved", "profiles", f"找不到船只名称 {ship_name!r}；现有船只：{available}")])
    if len(matches) > 1:
        available = "、".join(f"{sid}({name})" for sid, name in matches)
        raise ConfigError(
            [
                issue(
                    "ship_ambiguous",
                    "profiles",
                    f"船只名称 {ship_name!r} 匹配到多个档案：{available}；请改用 ship_id 精确指定",
                )
            ]
        )
    return matches[0][0]


# 兼容 main.py 迁移前的私有名字（旧测试/CLI 通过 main 模块别名调用的是这些实现）
_load_app = load_app_for_account
_load_accounts = load_accounts_or_none
_list_ships = list_ships
_resolve_ship = resolve_ship


# ---------------------------------------------------------------------------
# 路径与账号覆盖
# ---------------------------------------------------------------------------


def resolve_data_dir(root: Path) -> Path:
    """解析 ``paths.data_dir``（单实例锁目录）；配置读不到时回退 ``<root>/data``。"""
    try:
        from ..config.loader import load_global

        global_config = load_global(root / "config.yaml", project_root=root)
        data_dir = Path(global_config.paths.data_dir)
    except Exception:  # pragma: no cover - 配置极端损坏时仍允许给出锁目录
        return root / "data"
    return data_dir if data_dir.is_absolute() else (root / data_dir).resolve()


def resolve_log_dir(root: Path, app: AppConfig | None = None) -> Path:
    """解析本次会话的日志目录（具体 ``session_*.log`` 在 STARTING 配置日志时确定）。"""
    if app is not None:
        path = Path(app.global_config.paths.log_dir)
        return path if path.is_absolute() else (app.project_root / path).resolve()
    return root / "logs"


def resolve_db_dir(root: Path, app: AppConfig | None = None) -> Path:
    if app is not None:
        path = Path(app.global_config.paths.data_dir)
        return path if path.is_absolute() else (app.project_root / path).resolve()
    return root / "data"


# ---------------------------------------------------------------------------
# 快速预检（RuntimeService.start 入口，不加载完整运行时）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreflightResult:
    """``preflight()`` 的结果：问题列表 + 解析成功的船/账号。"""

    issues: tuple[ValidationIssue, ...]
    profile_id: str
    ship_id: str | None
    account_alias: str | None
    rotator: AccountRotator | None
    accounts: AccountsConfig | None

    @property
    def ok(self) -> bool:
        return not any(item.is_error for item in self.issues)


def preflight(root: Path, options: StartOptions) -> PreflightResult:
    """快速校验启动选项（根目录/客户端/船名/账号/场次/日志级别）。

    只读取少量 YAML（profile 存在性、船只档案、accounts.yaml），不加载全局配置与地图包；
    完整校验由 :func:`plan_preview` / :func:`resolve_start_plan` 在 STARTING 阶段完成。
    """
    issues: list[ValidationIssue] = []
    root = Path(root)
    profile_id = options.profile_id or DEFAULT_PROFILE_ID

    # root / config.yaml
    if not root.is_dir() or not (root / "config.yaml").is_file():
        issues.append(issue("root_invalid", "config.yaml", f"根目录缺少 config.yaml: {root}"))

    # profile
    profile_path = root / "profiles" / profile_id / "profile.yaml"
    if not profile_path.is_file():
        issues.append(issue("profile_not_found", f"profiles/{profile_id}/profile.yaml", f"客户端配置不存在: {profile_id}"))

    # 场次上限
    if options.max_battles is not None and (
        isinstance(options.max_battles, bool) or not isinstance(options.max_battles, int) or options.max_battles <= 0
    ):
        issues.append(
            issue("max_battles_invalid", "StartOptions.max_battles", "本次场次上限必须是 >0 的整数或 None")
        )

    # 日志级别
    if options.log_level is not None and str(options.log_level).upper() not in LOG_LEVELS:
        issues.append(
            issue(
                "log_level_invalid",
                "StartOptions.log_level",
                f"日志级别必须是 {'/'.join(sorted(LOG_LEVELS))} 之一，实际 {options.log_level!r}",
            )
        )

    # 船只：显式 ship_id 优先（设计 4.3）；否则按名称解析；都未给出时回退配置
    ship_id = options.ship_id
    if not ship_id and options.ship_name:
        try:
            ship_id = resolve_ship(root, profile_id, None, options.ship_name)
        except ConfigError as exc:
            issues.extend(exc.issues)

    # 账号/轮换
    account_alias = options.account_alias
    accounts: AccountsConfig | None = None
    rotator: AccountRotator | None = None
    if options.rotate or account_alias:
        accounts = load_accounts_or_none(root)
        if accounts is None:
            issues.append(
                issue(
                    "account_unsupported",
                    "accounts.yaml",
                    "当前版本暂不支持账号档案（账号支持任务尚未完成）",
                )
            )
        elif options.rotate:
            if not accounts.rotation_enabled:
                issues.append(
                    issue(
                        "rotation_disabled",
                        "accounts.yaml: rotation.enabled",
                        "accounts.yaml 的 rotation.enabled 不是 true，无法轮换账号",
                    )
                )
            elif not accounts.enabled_accounts():
                issues.append(
                    issue(
                        "rotation_empty",
                        "accounts.yaml: accounts",
                        "accounts.yaml 中没有启用的账号：请至少将一个 accounts[].enabled 设为 true",
                    )
                )
            else:
                rotator = AccountRotator(accounts)
                if account_alias:
                    # 轮换的中途会话（服务启动下一账号时显式传入别名）：显式别名优先，
                    # 但仍必须属于启用的轮换计划，避免绕过轮换配置。
                    if account_alias not in rotator.aliases:
                        issues.append(
                            issue(
                                "account_not_found",
                                f"accounts.yaml: accounts.{account_alias}",
                                f"账号别名 {account_alias!r} 不在启用的轮换计划中"
                                f"（计划：{'、'.join(rotator.aliases) or '(空)'}）",
                            )
                        )
                    else:
                        # 把轮换起点移到显式账号，使 rotation_limit/生效值预览与实际首场一致
                        for _ in range(len(rotator.aliases) + 1):
                            current = rotator.current
                            if current is not None and current.alias == account_alias:
                                break
                            rotator.advance()
                else:
                    account_alias = rotator.current.alias if rotator.current is not None else None
        elif account_alias and accounts.get(account_alias) is None:
            available = "、".join(item.alias for item in accounts.accounts) or "(空)"
            issues.append(
                issue(
                    "account_not_found",
                    f"accounts.yaml: accounts.{account_alias}",
                    f"accounts.yaml 中找不到账号别名: {account_alias}（现有：{available}）",
                )
            )

    errors = [item for item in issues if item.is_error]
    return PreflightResult(
        issues=tuple(errors),
        profile_id=profile_id,
        ship_id=ship_id,
        account_alias=account_alias,
        rotator=rotator,
        accounts=accounts,
    )


# ---------------------------------------------------------------------------
# 生效值（4.4）
# ---------------------------------------------------------------------------


def _count_daily_battles(db_path: Path, day: date, account_alias: str | None) -> int | None:
    """以只读方式统计今日场次；数据库不存在/读不到时返回 None（界面显示“读取失败”）。"""
    if not db_path.is_file():
        return None
    try:
        uri = f"file:{db_path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.5)
        try:
            if account_alias is None:
                cursor = connection.execute(
                    "SELECT COUNT(*) FROM battles WHERE local_date = ?", (day.isoformat(),)
                )
            else:
                cursor = connection.execute(
                    "SELECT COUNT(*) FROM battles WHERE local_date = ? AND account_alias = ?",
                    (day.isoformat(), account_alias),
                )
            row = cursor.fetchone()
            return int(row[0]) if row is not None else 0
        finally:
            connection.close()
    except (sqlite3.Error, OSError):  # 数据库忙/表未建好都不影响启动
        return None


def build_effective_plan(
    app: AppConfig,
    options: StartOptions,
    *,
    rotator: AccountRotator | None = None,
) -> EffectivePlan:
    """按账号覆盖后的配置生成“本次实际会用到什么”的只读生效值。"""
    config = app.global_config
    account = app.account
    daily_alias = app.account_alias
    db_path = (resolve_db_dir(app.project_root, app) / "wows_script.db").resolve()
    try:
        daily_used = _count_daily_battles(db_path, date.today(), daily_alias)
    except Exception:  # pragma: no cover - 统计失败不影响启动
        daily_used = None
    return EffectivePlan(
        client_id=app.profile.client_id,
        client_display_name=app.profile.display_name,
        ship_id=app.ship.ship_id,
        ship_display_name=app.ship.display_name,
        account_alias=app.account_alias,
        account_display_name=account.display_name if account is not None else None,
        max_battles_override=options.max_battles,
        rotation_limit=rotator.current_limit() if rotator is not None else None,
        safety_battle_cap=int(config.safety.max_battles_per_session),
        daily_cap=int(config.session.daily_battle_cap),
        daily_used=daily_used,
        allowed_hours=config.session.allowed_hours,
        max_session_minutes=float(config.safety.max_session_minutes),
        navigation_mode=str(config.navigation.mode),
        emergency_stop_key=str(config.safety.emergency_stop_key),
        resume_key=str(config.safety.resume_key),
        heartbeat_s=float(config.safety.heartbeat_s),
        config_hash=str(app.config_hash),
        log_path=resolve_log_dir(app.project_root, app),
        db_path=db_path,
        diagnostics_dir=(app.project_root / config.logging.diagnostics_dir).resolve(),
        profiles_dir=Path(config.paths.profiles_dir),
        start_paused=bool(options.start_paused),
    )


# ---------------------------------------------------------------------------
# 预检入口（GUI 校验按钮 / 服务 STARTING）
# ---------------------------------------------------------------------------


def plan_preview(root: Path, options: StartOptions) -> PlanResult:
    """只读预检：加载配置 + 校验 + 生效值；可重复调用，不产生运行资源。"""
    root = Path(root)
    pre = preflight(root, options)
    if not pre.ok:
        return PlanResult(ok=False, issues=issue_views(pre.issues))

    ship_id = options.ship_id or pre.ship_id
    alias = pre.account_alias
    try:
        project = load_project(root, pre.profile_id, ship_id=ship_id, account=alias)
    except ConfigError as exc:
        return PlanResult(
            ok=False,
            issues=issue_views(exc.errors),
            warnings=issue_views(exc.warnings),
        )
    all_issues = list(project.issues) + validate_app(project.config)
    errors = [item for item in all_issues if item.is_error]
    warnings = [item for item in all_issues if not item.is_error]
    if errors:
        return PlanResult(ok=False, issues=issue_views(errors), warnings=issue_views(warnings))
    plan = build_effective_plan(project.config, options, rotator=pre.rotator)
    return PlanResult(ok=True, plan=plan, warnings=issue_views(warnings))


def resolve_start_plan(options: StartOptions, *, root: Path | None = None) -> tuple[AppConfig, EffectivePlan]:
    """服务 STARTING 阶段使用：完整校验后返回 (AppConfig, EffectivePlan)。

    校验失败抛 :class:`~src.common.errors.ConfigError`（由服务转成 VALIDATION_FAILED 事件并
    回到 STOPPED）。
    """
    project_root = Path(root) if root is not None else Path(options.root)
    pre = preflight(project_root, options)
    if not pre.ok:
        raise ConfigError(list(pre.issues))
    ship_id = options.ship_id or pre.ship_id
    project = load_project(project_root, pre.profile_id, ship_id=ship_id, account=pre.account_alias)
    all_issues = list(project.issues) + validate_app(project.config)
    errors = [item for item in all_issues if item.is_error]
    if errors:
        raise ConfigError(all_issues)
    plan = build_effective_plan(project.config, options, rotator=pre.rotator)
    return project.config, plan


def with_log_level(app: AppConfig, log_level: str | None) -> AppConfig:
    """把本次会话的日志级别覆盖应用到配置（原 ``_run_accounts`` 的内联替换）。"""
    if not log_level:
        return app
    return replace(
        app,
        global_config=replace(
            app.global_config,
            runtime=replace(app.global_config.runtime, log_level=log_level),
        ),
    )


__all__ = [
    "DEFAULT_PROFILE_ID",
    "LOG_LEVELS",
    "PreflightResult",
    "build_effective_plan",
    "issue",
    "issue_view",
    "issue_views",
    "list_ships",
    "load_accounts_or_none",
    "load_app_for_account",
    "plan_preview",
    "preflight",
    "resolve_data_dir",
    "resolve_db_dir",
    "resolve_log_dir",
    "resolve_ship",
    "resolve_start_plan",
    "with_log_level",
]
