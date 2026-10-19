# -*- coding: utf-8 -*-
"""统一 CLI 文档契约测试（2026-08-04 GitHub 首版收口）。
"""
from __future__ import annotations
import re
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent
_SKILL = _TOOLKIT_ROOT / "boss-hr-auto" / "SKILL.md"
_README = _TOOLKIT_ROOT / "README.md"
_INSTALL_BAT = _TOOLKIT_ROOT / "install-windows.bat"
_UNINSTALL_BAT = _TOOLKIT_ROOT / "uninstall-windows.bat"


def _read(path: Path) -> str:
    assert path.is_file(), f"{path} 不存在"
    return path.read_text(encoding="utf-8")


_NEGATION = r"不支持|禁止|不得|不能|不允许|不会|不要|不应|不再|均不|不存在|内部实现|仅.*参考"


def _strip_comments_and_echo(text: str) -> str:
    """去掉 .bat 注释（REM / ::）和 echo 行，只看真实命令。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.upper().startswith("REM "):
            continue
        if s.startswith("::"):
            continue
        if s.upper().startswith("ECHO "):
            continue
        out.append(line)
    return "\n".join(out)


SEVEN_COMMANDS = ["start", "confirm", "fetch", "score", "report", "greet", "status"]
# v1.1.1: 加 doctor 后变成 8 个公开命令
EIGHT_COMMANDS_V111 = SEVEN_COMMANDS + ["doctor"]
FORBIDDEN_OLD_SCRIPTS = [
    "boss_jd.py", "confirm_run.py",
    "recommend_list.py", "recommend_download.py",
    "prepare_scoring_inputs.py", "collect_llm_scores.py", "score_resumes.py",
    "generate_html_report.py", "auto_greet.py",
    "shared/cli_runner.py",
]


# ============================================================
# 1-7. SKILL.md 内容契约
# ============================================================

def test_skill_only_mentions_seven_boss_hr_commands():
    src = _read(_SKILL)
    found = set(re.findall(r"boss-hr\s+([a-z][a-z0-9_-]*)", src))
    extra = found - set(EIGHT_COMMANDS_V111)
    assert not extra, (
        f"SKILL.md 提到了非 8 个公开命令的 boss-hr 子命令: {extra}"
    )
    missing = set(EIGHT_COMMANDS_V111) - found
    assert not missing, f"SKILL.md 缺少这些公开命令: {missing}"


def test_skill_does_not_direct_exec_old_scripts():
    """SKILL.md 不得包含"python <old_script>.py ..."形式的直接执行。

    例外：行内含"禁止 / 不得 / 不应 / 参考 / 内部实现" 元说明。
    """
    src = _read(_SKILL)
    bad = []
    for line in src.splitlines():
        for old in FORBIDDEN_OLD_SCRIPTS:
            if re.search(rf"python\s+\S*{re.escape(old)}", line):
                if not re.search(_NEGATION, line):
                    bad.append(f"{old}: {line.strip()}")
    assert not bad, (
        f"SKILL.md 不得直接执行旧脚本（应只在禁止列表里出现）:\n"
        + "\n".join(f"  {x}" for x in bad)
    )


def test_skill_does_not_reference_spec_json():
    """SKILL.md 提到 spec 只能作为元说明（"禁止 spec"）。不得有 spec 文件名/命令引用。"""
    src = _read(_SKILL)
    bad = []
    for line in src.splitlines():
        low = line.lower()
        if "spec" in low and re.search(r"spec\s*\.\w+|\bspec\b\s*\(|--spec", low):
            bad.append(line.strip())
    assert not bad, f"SKILL.md 引用了 spec 文件名/命令：\n" + "\n".join(bad)


def _has_negation_context(lines: list, idx: int) -> bool:
    """判断 lines[idx] 是否在"禁止/不支持"上下文中。

    看当前行 + 前 3 行 + 后 1 行（多行块如"禁止调用：a.py / b.py / c.py"）。
    """
    window = lines[max(0, idx - 3): min(len(lines), idx + 2)]
    return any(re.search(_NEGATION, l) for l in window)


def test_skill_does_not_implement_continue_or_batch():
    """SKILL.md 里 continue / batch 只能出现在"不支持 / 禁止"声明。"""
    src = _read(_SKILL)
    lines = src.splitlines()
    bad = []
    for i, line in enumerate(lines):
        low = line.lower()
        for keyword in ("continue", "batch"):
            if keyword in low and not _has_negation_context(lines, i):
                bad.append(f"{keyword}: {line.strip()}")
    assert not bad, (
        f"SKILL.md 提到 continue/batch 但不在禁止上下文：\n"
        + "\n".join(bad)
    )


def test_skill_has_start_stop_rule():
    src = _read(_SKILL)
    found = False
    for line in src.splitlines():
        if "start" in line.lower() and re.search(r"停止|停下", line):
            found = True
            break
    assert found, "SKILL.md 必须包含 start 后停止/停下的明确规则"


def test_skill_has_score_single_candidate_rule():
    src = _read(_SKILL)
    found = False
    for line in src.splitlines():
        if "score" in line.lower() and re.search(r"一位|只读一位|一位候选人|每次.*一位", line):
            found = True
            break
    assert found, "SKILL.md 必须包含 score 每次只处理一位候选人的规则"


def test_skill_has_greet_explicit_approval_rule():
    src = _read(_SKILL)
    found = False
    for line in src.splitlines():
        if "greet" in line.lower() and re.search(r"用户.*明确|明确.*用户|明确批准", line):
            found = True
            break
    assert found, "SKILL.md 必须包含 greet 需用户明确批准的规则"


def test_skill_does_not_mention_auto_greet():
    src = _read(_SKILL)
    lines = src.splitlines()
    bad = []
    for i, line in enumerate(lines):
        if "auto_greet" in line and not _has_negation_context(lines, i):
            bad.append(line.strip())
    assert not bad, f"SKILL.md 提到 auto_greet 但不在禁止上下文：\n" + "\n".join(bad)


# ============================================================
# 8. README 内容契约
# ============================================================

def test_readme_states_editable_install_positioning():
    src = _read(_README)
    assert "editable install" in src or "pip install -e" in src, (
        "README 必须包含 editable install 表述"
    )
    assert re.search(r"不是.*wheel|不.*独立|源码工具包", src), (
        "README 必须说明不是独立 wheel"
    )


def test_readme_mentions_seven_commands():
    """README 必须提到 7 个 boss-hr 命令。允许在表格 / 代码块 / 段落任何位置。"""
    src = _read(_README)
    for cmd in SEVEN_COMMANDS:
        assert f"boss-hr {cmd}" in src, f"README 缺少 `boss-hr {cmd}` 示例"


# ============================================================
# 9. install-windows.bat 静态契约
# ============================================================

def test_install_bat_no_hardcoded_user_paths():
    src = _strip_comments_and_echo(_read(_INSTALL_BAT))
    bad = []
    for pattern in ("C:\\Users", "%USERPROFILE%", "C:\\\\", "/Users/", "yuyu", "boss-hr-output"):
        if pattern in src:
            bad.append(pattern)
    assert not bad, f"install-windows.bat 硬编码了用户/桌面/输出路径: {bad}"


def test_install_bat_does_not_launch_edge_or_boss():
    """install-windows.bat 不得启动 Edge、连接 BOSS、调用 patchright。"""
    src = _strip_comments_and_echo(_read(_INSTALL_BAT))
    bad = []
    if "msedge" in src.lower():
        bad.append("msedge")
    if "patchright" in src.lower():
        bad.append("patchright")
    # "boss" 作为命令关键字（boss.exe / boss-cli / boss.bat）— 但品牌描述"BOSS 直聘"允许
    if re.search(r"\bboss[-_.]\w+\.(exe|bat|sh)\b", src.lower()):
        bad.append("boss.*.{exe,bat,sh}")
    assert not bad, f"install-windows.bat 含禁止动作: {bad}"


def test_install_bat_checks_python_version_and_returns_nonzero_on_fail():
    src = _read(_INSTALL_BAT)
    assert "python --version" in src or "PY_VERSION" in src
    assert "exit /b 1" in src or "exit /b 2" in src


def test_install_bat_uses_relative_paths():
    src = _read(_INSTALL_BAT)
    assert "%~dp0" in src, "install-windows.bat 缺少 `pushd %~dp0`"


# ============================================================
# 10. uninstall-windows.bat 静态契约
# ============================================================

def test_uninstall_bat_does_not_delete_source_or_data():
    """uninstall-windows.bat 不得在命令中删除源码 / 输出 / Edge profile。"""
    # 检查"实际命令行"（去掉注释 / echo）
    src = _strip_comments_and_echo(_read(_UNINSTALL_BAT))
    bad = []
    # 删除命令
    if re.search(r"\b(rmdir|del|rd)\s+", src.lower()):
        bad.append("rmdir/del")
    if "remove-item" in src.lower():
        bad.append("remove-item")
    # 用户数据
    if "boss-hr-output" in src.lower():
        bad.append("boss-hr-output")
    if re.search(r"chrome[-_]profile|edge[-_]profile", src.lower()):
        bad.append("edge profile")
    assert not bad, f"uninstall-windows.bat 命令行含禁止动作: {bad}"


def test_uninstall_bat_calls_pip_uninstall():
    src = _read(_UNINSTALL_BAT)
    assert "pip uninstall" in src
    assert "boss-hr-agent-toolkit" in src


# ============================================================
# 11. CLI_WORKFLOW.md 存在 + 含流程
# ============================================================

def test_cli_workflow_doc_exists_and_has_flow():
    p = _TOOLKIT_ROOT / "docs" / "CLI_WORKFLOW.md"
    assert p.is_file(), "docs/CLI_WORKFLOW.md 必须存在"
    src = p.read_text(encoding="utf-8")
    for cmd in SEVEN_COMMANDS:
        assert cmd in src, f"docs/CLI_WORKFLOW.md 缺 {cmd}"


def test_single_skill_and_all_local_document_links_resolve():
    """Removing atomic skills must leave one usable entry and no dead links."""
    assert sorted(_TOOLKIT_ROOT.glob("**/SKILL.md")) == [_SKILL]
    docs = [_README, _SKILL, _TOOLKIT_ROOT / "FILE_MANAGEMENT.md"]
    docs.extend((_TOOLKIT_ROOT / "docs").rglob("*.md"))
    for path in docs:
        for target in re.findall(r"\]\(([^)]+)\)", _read(path)):
            if "://" in target or target.startswith("#"):
                continue
            local = target.split("#", 1)[0]
            assert (path.parent / local).exists(), f"{path}: broken link {target}"


# ============================================================
# 12. CHANGELOG.md
# ============================================================

def test_changelog_exists_with_first_release_entry():
    p = _TOOLKIT_ROOT / "CHANGELOG.md"
    assert p.is_file(), "CHANGELOG.md 必须存在"
    src = p.read_text(encoding="utf-8")
    assert "v1.1.0" in src or "1.1.0" in src
    assert "boss-hr" in src
    assert "尚未" in src or "未验证" in src or "未完成" in src
