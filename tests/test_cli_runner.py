# -*- coding: utf-8 -*-
"""Exercise the actual worker runner with a local fixture, never a browser."""
from __future__ import annotations

import json
import subprocess

import pytest

from shared import cli_runner as runner


@pytest.fixture
def worker(monkeypatch):
    # Ephemeral test entry only; production allowlist remains unchanged.
    monkeypatch.setitem(runner.TOOLS, "fixture", "tests/fixtures/argv_echo.py")
    return lambda args, **kwargs: runner.run_python_cli("fixture", args, **kwargs)


@pytest.mark.parametrize("args", [
    ["--job-name", "线控底盘制动、转向工程师"],
    ["--job-name", "机械 研发（结构） 15-20K"],
    ["--job-info", '{"name":"研发 A","skills":["CAD","结构"]}'],
    ["--job-name", "literal $() & ; | ` value"],
])
def test_argument_boundaries_and_fixed_cwd(worker, args):
    proc = worker(args)
    assert proc.returncode == 0
    payload = json.loads(proc.stdout.removeprefix("[argv_echo] "))
    assert payload["unknown"] == args
    assert payload["cwd"].casefold() == str(runner.TOOLKIT_ROOT).casefold()
    assert "--run-id" not in payload["argv"]


def test_utf8_stdout_and_stderr(worker):
    proc = worker(["--emit", "中文输出", "--emit-stderr", "中文错误"])
    assert json.loads(proc.stdout.removeprefix("[argv_echo] "))["emit"] == "中文输出"
    assert proc.stderr.strip() == "中文错误"


@pytest.mark.parametrize("code", [0, 20, 26, 27])
def test_worker_exit_code_is_preserved(worker, code):
    assert worker(["--exit-code", str(code)]).returncode == code


def test_real_worker_timeout(worker):
    with pytest.raises(subprocess.TimeoutExpired):
        worker(["--sleep", "10"], timeout=0.2)


@pytest.mark.parametrize("args", ["--job-name x", b"x", None, 42])
def test_command_strings_and_non_sequences_rejected(worker, args):
    with pytest.raises(TypeError):
        worker(args)


def test_unknown_tool_rejected():
    with pytest.raises(ValueError, match="白名单"):
        runner.run_python_cli("../arbitrary.py", [])


def test_path_escape_rejected_before_execution(monkeypatch):
    monkeypatch.setitem(runner.TOOLS, "escape", "../outside.py")
    with pytest.raises(ValueError, match="逃逸"):
        runner.run_python_cli("escape", [])


def test_missing_worker_rejected(monkeypatch):
    monkeypatch.setitem(runner.TOOLS, "missing", "missing_worker.py")
    with pytest.raises(FileNotFoundError):
        runner.run_python_cli("missing", [])


@pytest.mark.parametrize("tool", list(runner.TOOLS))
def test_retained_workers_have_executable_help(tool):
    # Resource deletion must not break any worker still used by the CLI.
    proc = runner.run_python_cli(tool, ["--help"], timeout=15)
    assert proc.returncode == 0, proc.stderr
    assert "usage:" in proc.stdout.lower()
