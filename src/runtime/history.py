"""托管记录的有界只读查询，不复用日志线程的数据库连接。"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

from .messages import BattleRecord, HistoryView


def read_history(path: Path, account_alias: str | None, today: date | None = None) -> HistoryView:
    """按当前账号展示最近 30 场，今日只统计已结束场次。"""
    if not path.is_file():
        return HistoryView()
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
            clause = "account_alias IS ?"
            rows = connection.execute(
                f"SELECT battle_id, started_at, COALESCE(returned_to_port_at, exit_confirmed_at, "
                f"death_detected_at), account_alias, result, map_id FROM battles WHERE {clause} "
                "ORDER BY battle_id DESC LIMIT 30", (account_alias,),
            ).fetchall()
            count = connection.execute(
                f"SELECT COUNT(*) FROM battles WHERE {clause} AND local_date=? AND result IS NOT NULL",
                (account_alias, (today or date.today()).isoformat()),
            ).fetchone()[0]
        return HistoryView(tuple(BattleRecord(*row) for row in rows), count)
    except (OSError, sqlite3.Error) as exc:
        return HistoryView(error=f"记录读取失败：{exc}")
