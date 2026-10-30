"""账号轮换计划（用户扩展需求：多账号选择 / 每账号独立配置 / 会话间轮换）。

设计口径（由用户 2026-10-30 确认，属于详细设计原范围之外的扩展）：
- 只保存本地别名（alias），不保存真实账号、密码、令牌；
- 不自动登录、不代登录；真实登录与切号由使用者手动在客户端完成；
- 同一时间仍只运行一个账号、一个游戏窗口；
- 每个账号有独立客户端配置、船只、每日场次上限与允许时段；
- 一个账号达到轮换场次后，脚本停止当前会话，提示使用者手动切号，
  切换完成并按恢复键后继续新账号会话。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config.models import AccountConfig, AccountsConfig

STOP_REASON_ROTATION = "account_rotation"


@dataclass
class AccountRotator:
    """按 round_robin 顺序规划账号；不负责登录，只负责选择与计数。"""

    accounts_config: AccountsConfig
    _order: tuple[AccountConfig, ...] = ()
    _index: int = 0
    _battles_done: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.reload()

    def reload(self) -> None:
        self._order = tuple(self.accounts_config.enabled_accounts())
        self._index = 0
        self._battles_done = {account.alias: 0 for account in self._order}

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self.accounts_config.rotation_enabled and self._order)

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(account.alias for account in self._order)

    @property
    def current(self) -> AccountConfig | None:
        if not self._order:
            return None
        return self._order[self._index % len(self._order)]

    @property
    def index(self) -> int:
        return self._index

    def battles_done(self, alias: str) -> int:
        return self._battles_done.get(alias, 0)

    def record_battle(self, alias: str | None = None) -> int:
        """记录一场完成；返回该账号在本轮已完成的场次。"""
        name = alias or (self.current.alias if self.current else "")
        self._battles_done[name] = self._battles_done.get(name, 0) + 1
        return self._battles_done[name]

    def limit_for(self, account: AccountConfig | None) -> int:
        if account is not None and account.max_battles_per_session is not None:
            return max(1, int(account.max_battles_per_session))
        return max(1, int(self.accounts_config.max_battles_per_account))

    def current_limit(self) -> int | None:
        if not self.enabled:
            return None
        return self.limit_for(self.current)

    def needs_rotation(self) -> bool:
        """当前账号是否已达到本轮场次上限。"""
        if not self.enabled or self.current is None:
            return False
        return self.battles_done(self.current.alias) >= self.limit_for(self.current)

    def advance(self) -> AccountConfig | None:
        """切到下一个启用账号；单账号时回到自身。"""
        if not self._order:
            return None
        self._index = (self._index + 1) % len(self._order)
        return self.current

    def next_alias(self) -> str | None:
        if len(self._order) <= 1:
            return None
        return self._order[(self._index + 1) % len(self._order)].alias

    def plan(self) -> tuple[AccountConfig, ...]:
        """本轮要依次运行的账号（单账号也返回它自己）。"""
        return self._order
