"""命令行入口测试：只验证 --help/--version/--dry-run 与配置错误退出码，不启动游戏。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from src.common.errors import ConfigError
from src.main import EXIT_CONFIG_ERROR, _list_ships, _resolve_ship, build_parser

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-m", "src.main", *args],
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        env=env,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace")
    stderr = completed.stderr.decode("utf-8", errors="replace")
    return subprocess.CompletedProcess(completed.args, completed.returncode, stdout, stderr)


def test_parser_help_contains_account_options() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    assert "--account" in help_text
    assert "--rotate" in help_text
    assert "--dry-run" in help_text
    assert "--ship-name" in help_text
    assert "--list-ships" in help_text


def test_main_help_exits_zero() -> None:
    result = _run(["--help"])
    assert result.returncode == 0, result.stderr
    assert "--rotate" in result.stdout


def test_main_missing_config_exits_config_error(tmp_path: Path) -> None:
    result = _run(["--root", str(tmp_path), "--dry-run"])
    assert result.returncode == EXIT_CONFIG_ERROR
    assert "config.yaml" in result.stderr


def test_main_version() -> None:
    result = _run(["--version"])
    assert result.returncode == 0
    assert "wows_script" in result.stdout


def test_list_ships_real_repo() -> None:
    ships = _list_ships(PROJECT_ROOT, "cn360")
    assert ships
    ship_ids = [ship_id for ship_id, _ in ships]
    assert "leningrad" in ship_ids
    assert any(display for _, display in ships)


def test_resolve_ship_by_display_name() -> None:
    ships = dict(_list_ships(PROJECT_ROOT, "cn360"))
    display_name = ships["leningrad"]
    assert display_name == "列宁格勒"
    assert _resolve_ship(PROJECT_ROOT, "cn360", None, display_name) == "leningrad"
    assert _resolve_ship(PROJECT_ROOT, "cn360", None, "leningrad") == "leningrad"


def test_resolve_ship_unknown_name_lists_candidates() -> None:
    with pytest.raises(ConfigError) as exc_info:
        _resolve_ship(PROJECT_ROOT, "cn360", None, "不存在的船")
    assert "现有船只" in str(exc_info.value)


def test_main_list_ships_cli() -> None:
    result = _run(["--list-ships"])
    assert result.returncode == 0, result.stderr
    assert "leningrad" in result.stdout
