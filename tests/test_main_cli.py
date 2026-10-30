"""命令行入口测试：只验证 --help/--version/--dry-run 与配置错误退出码，不启动游戏。"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from src.main import EXIT_CONFIG_ERROR, build_parser

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
