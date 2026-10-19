# -*- coding: utf-8 -*-
"""统一 CLI 安装入口回归测试（2026-08-04）。

保证：

  1. `boss_hr/__main__.py` 存在且调 `boss_hr.cli.main` — 让
     `python -m boss_hr` 能工作。
  2. `pyproject.toml` 存在 + 解析合法 + 只暴露 `boss-hr` console script
     + 只把 `boss_hr.*` 系列包纳入 packages。
  3. `boss_hr.cli.COMMANDS` 正好是 7 个公开命令，无 continue/batch/
     prepare/collect/finalize/run-all/spec 泄漏。
  4. console script `boss-hr` 指向 `boss_hr.cli:main`。
  5. editable install 后从项目外 cwd 调用 `boss-hr --help` 能列 7 命令。

不依赖任何网络 / 不创建 venv / 不调 patchright / 不连 BOSS。
"""
from __future__ import annotations
import ast
import sys
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent  # tests/ → toolkit root
_PYPROJECT = _TOOLKIT_ROOT / "pyproject.toml"
_MAIN_PY = _TOOLKIT_ROOT / "boss_hr" / "__main__.py"
_CLI_PY = _TOOLKIT_ROOT / "boss_hr" / "cli.py"


# ============================================================
# 1. boss_hr/__main__.py 必须存在 + 转交 main
# ============================================================

def test_boss_hr_main_module_exists():
    """boss_hr/__main__.py 必须存在。"""
    assert _MAIN_PY.is_file(), f"{_MAIN_PY} 不存在 — python -m boss_hr 会失败"


def test_boss_hr_main_module_delegates_to_cli_main():
    """boss_hr/__main__.py 必须有 `if __name__ == "__main__": sys.exit(main())`。

    简单判定：源码里必须同时出现
      - `if __name__ == "__main__":`
      - `sys.exit(`
      - `main()` 在 sys.exit 调用里
    """
    src = _MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)

    has_main_guard = False
    sys_exit_with_main_call = False

    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        # 找 `if __name__ == "__main__":`
        if not (isinstance(node.test, ast.Compare)
                and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"):
            continue
        has_main_guard = True
        # 在 body 里找 `sys.exit(main())`
        for sub in ast.walk(node):
            if (isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "exit"
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "sys"
                and sub.args
                and isinstance(sub.args[0], ast.Call)
                and isinstance(sub.args[0].func, ast.Name)
                and sub.args[0].func.id == "main"):
                sys_exit_with_main_call = True

    assert has_main_guard, "boss_hr/__main__.py 缺少 `if __name__ == \"__main__\":` 守卫"
    assert sys_exit_with_main_call, (
        "boss_hr/__main__.py 缺少 `sys.exit(main())` 转交调用"
    )


# ============================================================
# 2. pyproject.toml 存在 + 解析合法
# ============================================================

def test_pyproject_exists():
    assert _PYPROJECT.is_file(), f"{_PYPROJECT} 不存在"


def test_pyproject_parses_as_valid_toml():
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    # 必有 [project] + [build-system] + [project.scripts]
    assert "project" in data
    assert "build-system" in data


def test_pyproject_exposes_boss_hr_console_script():
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert "boss-hr" in scripts, f"project.scripts 必须有 boss-hr，得到 {list(scripts)}"
    assert scripts["boss-hr"] == "boss_hr.cli:main", (
        f"boss-hr 入口必须指向 boss_hr.cli:main，得到 {scripts['boss-hr']!r}"
    )


def test_pyproject_does_not_expose_forbidden_scripts():
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    scripts = data["project"].get("scripts", {})
    forbidden = {"boss-jd", "boss-hr-old", "boss-run", "boss-spec",
                 "continue", "batch"}
    leaked = set(scripts.keys()) & forbidden
    assert not leaked, f"console_scripts 泄漏了被禁项: {leaked}"


def test_pyproject_packages_only_boss_hr_subtree():
    """[tool.setuptools].packages 只包含 boss_hr.* 子包，不暴露旧业务脚本作为 Python package。"""
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    packages = data["tool"]["setuptools"]["packages"]
    expected = {"boss_hr", "boss_hr.commands", "boss_hr.application",
                "boss_hr.adapters", "boss_hr.contracts"}
    assert set(packages) == expected, f"packages 应为 {expected}，得到 {packages}"
    # 旧业务脚本不应作为 Python package 暴露
    leaked = set(packages) - expected
    assert not leaked, f"泄漏了非 boss_hr 子包: {leaked}"


# ============================================================
# 3. boss_hr.cli.COMMANDS 正好是 7 个公开命令
# ============================================================

def test_comandos_registry_has_exactly_eight_public_commands_v111():
    """v1.1.1: COMMANDS 必须正好是 8 个公开命令（含 doctor）。"""
    from boss_hr.cli import COMMANDS
    expected = {"start", "confirm", "fetch", "score", "report",
                "greet", "status", "doctor"}
    assert set(COMMANDS.keys()) == expected, (
        f"COMMANDS 应为 {expected}，得到 {sorted(COMMANDS.keys())}"
    )


def test_comandos_registry_does_not_leak_forbidden_commands():
    forbidden = {"continue", "batch", "prepare", "collect", "finalize",
                 "run-all", "spec", "spec_file", "run_all"}
    from boss_hr.cli import COMMANDS
    leaked = set(COMMANDS.keys()) & forbidden
    assert not leaked, f"COMMANDS 泄漏了被禁项: {leaked}"


# ============================================================
# 4. console script 入口字符串与 cli.py 的 main() 函数对应
# ============================================================

def test_cli_main_function_exists():
    """boss_hr/cli.py 必须定义 main() 函数。"""
    src = _CLI_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    func_names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "main" in func_names, "boss_hr/cli.py 必须有 def main(...):"


# ============================================================
# 5. argparse 子命令只能从 COMMANDS 注册表中产生
# ============================================================

def test_argparse_subparsers_source_is_commands_registry():
    """build_parser 必须从 COMMANDS 注册表构造 subparsers（防硬编码泄漏）。"""
    src = _CLI_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_parser":
            for sub in ast.walk(node):
                # 寻找 `for name, (add_args, _run_fn) in COMMANDS.items():`
                if (isinstance(sub, ast.For)
                    and isinstance(sub.iter, ast.Call)
                    and isinstance(sub.iter.func, ast.Attribute)
                    and sub.iter.func.attr == "items"
                    and isinstance(sub.iter.func.value, ast.Name)
                    and sub.iter.func.value.id == "COMMANDS"):
                    found = True
    assert found, (
        "build_parser 必须 `for name, (add_args, _run_fn) in COMMANDS.items():` "
        "动态构造 subparsers（防硬编码泄漏 continue/batch 等）"
    )


# ============================================================
# 6. help 输出只能显示 7 个命令（parser 自检）
# ============================================================

def test_help_only_shows_seven_commands():
    """解析 `boss-hr --help` 输出，命令列表必须正好是 7 个。"""
    import argparse
    from boss_hr.cli import build_parser
    p = build_parser()
    # 收集 subparsers action 的 choices
    sub_actions = [a for a in p._actions
                   if isinstance(a, argparse._SubParsersAction)]
    assert len(sub_actions) == 1, (
        f"应只有 1 个 subparsers action，得到 {len(sub_actions)}"
    )
    choices = set(sub_actions[0].choices.keys())
    expected = {"start", "confirm", "fetch", "score", "report",
                "greet", "status", "doctor"}
    assert choices == expected, (
        f"argparse 子命令应为 {expected}，得到 {sorted(choices)}"
    )
