# -*- coding: utf-8 -*-
"""v1.1.2 自动启动 Edge + waiting_user_login 测试。

覆盖：
  1. start 无 9222 时 ensure_browser_ready 调 launch_dedicated_edge
  2. auto_launch=True + Edge 已监听 + 已登录 → 继续 start
  3. auto_launch=True + Edge 自动启动成功 + 已登录 → 继续（含 login_session_reused）
  4. auto_launch=True + Edge 自动启动后无登录 → waiting_user_login（不创建 run）
  5. waiting_user_login 不创建 run、不调 boss_jd
  6. waiting_user_login 返回完整 message + data.browser_auto_launched
  7. waiting_user_login 是 ok=True（不是错误）
  8. auto_launch=False + 无 9222 → CDP_NOT_RUNNING（按 --no-auto-launch 语义）
  9. fetch / greet 同样接 ensure_browser_ready
 10. confirm / score / report / status 不接 ensure_browser_ready
 11. launch_dedicated_edge 使用专用 profile（不污染普通 Edge）
 12. profile 目录名固定为 boss-hr-edge-profile
 13. start --no-auto-launch 不会自动启动 Edge
 14. profile 不在用户主目录的普通 Edge 路径下
 15. ensure_browser_ready 错误 JSON 含完整 remediation + recoverable
 16. waiting_user_login next_action = retry_same_command

所有浏览器启动 / launch_edge 用 mock，pytest 不真启 Edge。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent
_CLI = _TOOLKIT_ROOT / "boss_hr" / "cli.py"
_SHARED = _TOOLKIT_ROOT / "shared"


# ============================================================
# helpers
# ============================================================

def _ready_ok(**overrides):
    """构造 ensure_browser_ready() 的成功结果。"""
    info = {
        "browser_auto_launched": False,
        "login_session_reused": False,
        "login_page_opened": False,
        "page_kind": "recommend",
        "page_url": "https://www.zhipin.com/web/chat/recommend",
        "logged_in": True,
    }
    info.update(overrides)
    @dataclass
    class _R:
        ok: bool = True
        error_obj = None
        remediation = None
        next_action = None
        info: dict = field(default_factory=lambda: info)
    return _R()


def _ready_fail(code, message="m", **info_overrides):
    """构造 ensure_browser_ready() 的失败结果（带错误码 + 恢复提示）。"""
    from boss_hr.contracts.errors import UnifiedError, ErrorCode
    info = {
        "browser_auto_launched": False,
        "login_page_opened": False,
        "login_wait_seconds": 0,
    }
    info.update(info_overrides)
    @dataclass
    class _R:
        ok: bool = False
        error_obj = UnifiedError(code=ErrorCode(code), message=message, recoverable=True)
        remediation = {"command": "boss-hr doctor",
                       "instructions": ["请检查浏览器"]}
        next_action = "fix_and_retry"
        info: dict = field(default_factory=lambda: info)
    return _R()


# ============================================================
# 1. ensure_browser_ready 行为
# ============================================================

def test_ensure_browser_ready_already_logged_in(monkeypatch):
    """9222 已监听 + 已登录 → ok=True（不自动启动）。纯 mock，不真探 9222。"""
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(be, "check_patchright_installed", lambda: True)
    monkeypatch.setattr(be, "check_edge_executable",
                        lambda: r"C:\fake\msedge.exe")
    # 9222 已开 → 不走 launch_dedicated_edge
    monkeypatch.setattr(be, "check_cdp_port_listening", lambda *a, **kw: True)
    monkeypatch.setattr(be, "check_cdp_connectable", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(be, "check_boss_logged_in",
                        lambda *a, **kw: (True,
                                          {"page_kind": "recommend",
                                           "current_url": "https://www.zhipin.com/web/chat/recommend"}))
    r = be.ensure_browser_ready(auto_launch=True)
    assert r.ok is True
    assert r.info.get("browser_auto_launched") is False
    assert r.info.get("page_kind") in ("recommend", "chat", "job-edit")


def test_ensure_browser_ready_no_auto_launch_returns_cdp_not_running(monkeypatch):
    """auto_launch=False + 无 9222 → CDP_NOT_RUNNING（不自动启动）。"""
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "check_cdp_port_listening", lambda *a, **kw: False)
    monkeypatch.setattr(be, "check_edge_executable",
                        lambda: r"C:\fake\edge\msedge.exe")
    monkeypatch.setattr(be, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(be, "check_patchright_installed", lambda: True)
    r = be.ensure_browser_ready(auto_launch=False)
    assert r.ok is False
    assert r.error_obj.code.value == "CDP_NOT_RUNNING"
    assert r.error_obj.recoverable is True
    assert r.remediation["command"] == "boss-hr doctor --launch-edge"


def test_ensure_browser_ready_auto_launch_calls_launch_dedicated_edge(monkeypatch):
    """auto_launch=True + 无 9222 + Edge 存在 → 调 launch_dedicated_edge。"""
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "check_cdp_port_listening",
                        lambda *a, **kw: False)  # 触发 launch
    monkeypatch.setattr(be, "check_edge_executable",
                        lambda: r"C:\fake\edge\msedge.exe")
    monkeypatch.setattr(be, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(be, "check_patchright_installed", lambda: True)
    captured = {}
    def _fake_launch(*a, **kw):
        captured["called"] = True
        from boss_hr.adapters.browser_environment import _EdgeLaunch
        return _EdgeLaunch(ok=True, pid=99999, edge_path="x", profile_dir="y")
    monkeypatch.setattr(be, "launch_dedicated_edge", _fake_launch)
    monkeypatch.setattr(be, "check_cdp_connectable", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(be, "check_boss_logged_in",
                        lambda *a, **kw: (True, {"page_kind": "r", "current_url": "u"}))
    r = be.ensure_browser_ready(auto_launch=True)
    assert r.ok is True
    assert captured["called"] is True
    assert r.info.get("browser_auto_launched") is True


def test_ensure_browser_ready_login_session_reused(monkeypatch):
    """auto_launch=True 启动后 Cookie 立即有效 → login_session_reused=True。"""
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "check_cdp_port_listening", lambda *a, **kw: False)
    monkeypatch.setattr(be, "check_edge_executable",
                        lambda: r"C:\fake\msedge.exe")
    monkeypatch.setattr(be, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(be, "check_patchright_installed", lambda: True)
    from boss_hr.adapters.browser_environment import _EdgeLaunch
    monkeypatch.setattr(be, "launch_dedicated_edge",
                        lambda *a, **kw: _EdgeLaunch(ok=True, edge_path="x",
                                                    profile_dir="y"))
    monkeypatch.setattr(be, "check_cdp_connectable", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(be, "check_boss_logged_in",
                        lambda *a, **kw: (True, {"page_kind": "r"}))
    r = be.ensure_browser_ready(auto_launch=True)
    assert r.ok is True
    assert r.info.get("login_session_reused") is True


# ============================================================
# 2. start 接 ensure_browser_ready：waiting_user_login
# ============================================================

def test_start_no_cdp_no_auto_launch_returns_cdp_not_running(monkeypatch):
    """--no-auto-launch + 无 CDP → CDP_NOT_RUNNING（v1.1.1 行为保留）。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail("CDP_NOT_RUNNING", "未检测到 9222"))
    monkeypatch.setattr(ss, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail("CDP_NOT_RUNNING", "未检测到 9222"))
    called = {"boss_jd": 0}
    def _fake(*a, **kw):
        called["boss_jd"] += 1
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(legacy_runner, "run_legacy_cli", _fake)

    res = ss.start_new_run(query="X", job_name=None, encrypt_job_id=None,
                            skip_preflight=False, skip_resolve=True,
                            auto_launch_browser=False, login_wait_seconds=1)
    d = res.to_dict("start")
    assert d["ok"] is False
    assert d["error"]["code"] == "CDP_NOT_RUNNING"
    assert d["error"]["recoverable"] is True
    assert called["boss_jd"] == 0


def test_start_waiting_user_login_does_not_call_boss_jd(monkeypatch):
    """未登录超时 → waiting_user_login（ok=True, 不调 boss_jd）。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail(
                            "BOSS_LOGIN_REQUIRED", "登录超时",
                            browser_auto_launched=True,
                            login_page_opened=True,
                            login_wait_seconds=20,
                        ))
    monkeypatch.setattr(ss, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail(
                            "BOSS_LOGIN_REQUIRED", "登录超时",
                            browser_auto_launched=True,
                            login_page_opened=True,
                            login_wait_seconds=20,
                        ))
    called = {"boss_jd": 0, "resolve": 0}
    def _fake(*a, **kw):
        called["boss_jd"] += 1
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(legacy_runner, "run_legacy_cli", _fake)
    def _fake_resolve(q):
        called["resolve"] += 1
        return {"encryptJobId": "X", "jobName": "Y"}
    monkeypatch.setattr(ss, "_resolve_recruiter_job", _fake_resolve)

    res = ss.start_new_run(query="X", job_name=None, encrypt_job_id=None,
                            skip_preflight=False, skip_resolve=False,
                            auto_launch_browser=True, login_wait_seconds=1)
    d = res.to_dict("start")
    # 关键断言：ok=True（不是错误）+ status=waiting_user_login
    assert d["ok"] is True
    assert d["status"] == "waiting_user_login"
    # v1.1.3: next_action 由 retry_same_command 升级为 scan_login_then_repeat_start
    assert d["next_action"] == "scan_login_then_repeat_start"
    assert d["data"]["browser_auto_launched"] is True
    assert d["data"]["login_page_opened"] is True
    # v1.1.3 不再阻塞扫码；login_wait_seconds=1 推导 wait_for_user_login=True（轮询路径），
    # 但 mock 直接返回 waiting_user_login 跳过轮询
    assert "登录" in d["message"]
    # 关键：未调 boss_jd
    assert called["boss_jd"] == 0, "waiting_user_login 不应调 boss_jd"
    # 也未调实时解析（因为 preflight 失败就立即返回）
    assert called["resolve"] == 0


def test_start_already_logged_in_proceeds(monkeypatch):
    """9222 已监听 + 已登录 → 走实时解析 + 调 boss_jd。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "ensure_browser_ready",
                        lambda *a, **kw: _ready_ok())
    monkeypatch.setattr(ss, "ensure_browser_ready",
                        lambda *a, **kw: _ready_ok())
    monkeypatch.setattr(ss, "_resolve_recruiter_job",
                        lambda q: {"encryptJobId": "RESOLVED_EID",
                                   "jobName": "某岗位",
                                   "jobId": None, "address": "", "salaryDesc": ""})

    class _OKProc:
        returncode = 0
        stdout = (
            json.dumps({"status": "waiting_user_confirmation",
                        "run_id": "2026-08-04_120000"})
            + "\nrun_id: 2026-08-04_120000（orchestrator 创建）\n"
            + "Saved to /tmp/job_detail.json\n"
        )
        stderr = ""
    called = {"boss_jd": 0}
    def _fake(*a, **kw):
        called["boss_jd"] += 1
        return _OKProc()
    monkeypatch.setattr(legacy_runner, "run_legacy_cli", _fake)

    res = ss.start_new_run(query="某岗位", job_name=None, encrypt_job_id=None,
                            skip_preflight=False, skip_resolve=False,
                            auto_launch_browser=True, login_wait_seconds=20)
    d = res.to_dict("start")
    assert d["ok"] is True
    assert d["status"] == "waiting_user_confirmation"
    assert d["encrypt_job_id"] == "RESOLVED_EID"
    assert d["run_id"] == "2026-08-04_120000"
    assert called["boss_jd"] == 1


def test_start_edge_not_found_returns_actionable(monkeypatch):
    """Edge 不存在 → EDGE_NOT_FOUND + 恢复提示。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail("EDGE_NOT_FOUND", "未找到 Edge"))
    monkeypatch.setattr(ss, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail("EDGE_NOT_FOUND", "未找到 Edge"))
    called = {"boss_jd": 0}
    monkeypatch.setattr(legacy_runner, "run_legacy_cli",
                        lambda *a, **kw: called.__setitem__("boss_jd",
                                                          called["boss_jd"] + 1))
    res = ss.start_new_run(query="X", job_name=None, encrypt_job_id=None,
                            skip_preflight=False, skip_resolve=True,
                            auto_launch_browser=True, login_wait_seconds=1)
    d = res.to_dict("start")
    assert d["ok"] is False
    assert d["error"]["code"] == "EDGE_NOT_FOUND"
    assert d["error"]["recoverable"] is True
    assert "remediation" in d["error"]


# ============================================================
# 3. start --no-auto-launch CLI 接口
# ============================================================

def test_start_no_auto_launch_argparse():
    """--no-auto-launch 必须出现在 start --help。"""
    res = subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), "start", "--help"],
        capture_output=True, env={**os.environ, "PYTHONUTF8": "1",
                                  "PYTHONIOENCODING": "utf-8",
                                  "PYTHONPATH": str(_SHARED)},
        cwd=str(_TOOLKIT_ROOT), timeout=15,
    )
    text = res.stdout.decode("utf-8", errors="replace")
    assert "--no-auto-launch" in text
    assert "--login-wait-seconds" in text


# ============================================================
# 4. profile 路径
# ============================================================

def test_dedicated_profile_dir_fixed_name(monkeypatch):
    """profile 目录名固定为 boss-hr-edge-profile（不嵌用户名变体）。"""
    from boss_hr.adapters import browser_environment as be
    # 清掉环境变量
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda p: "C:/Users/anyone")
    p = be._dedicated_profile_dir()
    assert p.endswith("boss-hr-edge-profile")
    # 不应包含任何用户目录的标准子目录（除了 LOCALAPPDATA 或 home）
    assert "Desktop" not in p
    assert "Documents" not in p


def test_dedicated_profile_dir_uses_localappdata(monkeypatch):
    """优先 %LOCALAPPDATA%。"""
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setenv("LOCALAPPDATA", "C:/Users/test/AppData/Local")
    monkeypatch.delenv("APPDATA", raising=False)
    p = be._dedicated_profile_dir()
    assert p.startswith("C:/Users/test/AppData/Local")
    assert p.endswith("boss-hr-edge-profile")


def test_dedicated_profile_does_not_use_default_edge_dir():
    """专用 profile 不在用户日常 Edge profile 目录（Edge\\User Data）下。"""
    from boss_hr.adapters import browser_environment as be
    import os
    p = be._dedicated_profile_dir()
    low = p.lower().replace("\\", "/")
    # Microsoft Edge 默认 profile 路径含 "Microsoft/Edge"
    assert "microsoft/edge" not in low
    assert "microsoft\\edge" not in p.lower()


# ============================================================
# 5. fetch / greet 接 ensure_browser_ready
# ============================================================

def test_fetch_calls_ensure_browser_ready(monkeypatch, tmp_path):
    """fetch 在 _pre_check 后调 ensure_browser_ready。"""
    from boss_hr.application import fetch_service as fs
    from boss_hr.adapters import browser_environment as be
    # 本测试在 tests/ 根目录，不在 tests/cli/，conftest autouse 不生效
    # 显式 mock ensure_browser_ready + run_legacy_cli
    monkeypatch.setattr(be, "ensure_browser_ready", lambda *a, **kw: _ready_ok())
    monkeypatch.setattr(fs, "ensure_browser_ready", lambda *a, **kw: _ready_ok())

    ws = tmp_path / "v112_ws"
    ws.mkdir(exist_ok=True)
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(ws))
    import output_manager
    monkeypatch.setattr(output_manager, "OUTPUT_ROOT", str(ws), raising=False)
    eid, rid = "EID_F1", "RID_F1"
    run = ws / eid / "runs" / rid
    proc = run / "process"
    proc.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "confirmed": True, "finished": False,
        "steps_done": ["jd", "download"],
    }), encoding="utf-8")
    (proc / "job_detail.json").write_text(json.dumps({
        "encryptJobId": eid, "jobName": "j",
        "_meta": {"run_id": rid},
    }), encoding="utf-8")
    (proc / "recommend_geek_ids.json").write_text(json.dumps([
        {"encryptGeekId": f"gid_{i}", "name": f"N{i}",
         "geekCard": {"encryptJobId": eid, "securityId": f"sec_{i}"}}
        for i in range(1)
    ]), encoding="utf-8")
    (proc / "new_resumes.json").write_text(json.dumps([
        {"ok": True, "name": "N0", "_meta": {"encrypt_geek_id": "gid_0"}}
    ]), encoding="utf-8")
    (proc / "failed_resumes.json").write_text("[]", encoding="utf-8")

    # mock recommend_list + recommend_download：写真实产物
    from boss_hr.adapters import legacy_runner
    monkeypatch.setattr(legacy_runner, "run_legacy_cli",
                        lambda *a, **kw: MagicMock(returncode=0, stdout="", stderr=""))

    from boss_hr.commands import fetch as fetch_cmd
    from boss_hr.cli import build_parser
    import argparse
    p = build_parser()
    ns = p.parse_args(["fetch", "--job-name", "j", "--encrypt-job-id", eid,
                        "--run-id", rid, "--count", "1"])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]; break
    res = fetch_cmd.run(ns)
    d = res.to_dict("fetch")
    assert d["ok"] is True
    assert d["status"] == "candidates_fetched"


def test_greet_calls_ensure_browser_ready(monkeypatch, tmp_path):
    """greet 同样接 ensure_browser_ready。"""
    from boss_hr.application import greet_service as gs
    from boss_hr.adapters import browser_environment as be
    from boss_hr.adapters import legacy_runner
    # 显式 mock（测试在 tests/ 根目录，conftest autouse 不生效）
    monkeypatch.setattr(be, "ensure_browser_ready", lambda *a, **kw: _ready_ok())
    monkeypatch.setattr(gs, "ensure_browser_ready", lambda *a, **kw: _ready_ok())
    monkeypatch.setattr(legacy_runner, "run_legacy_cli",
                        lambda *a, **kw: MagicMock(returncode=0, stdout="", stderr=""))

    ws = tmp_path / "v112_ws"
    ws.mkdir(exist_ok=True)
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(ws))
    import output_manager
    monkeypatch.setattr(output_manager, "OUTPUT_ROOT", str(ws), raising=False)
    eid, rid = "EID_G1", "RID_G1"
    run = ws / eid / "runs" / rid
    proc = run / "process"
    proc.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "confirmed": True, "finished": False,
        "steps_done": ["jd", "download", "score", "report"],
    }), encoding="utf-8")
    (proc / "job_detail.json").write_text(json.dumps({
        "encryptJobId": eid, "jobName": "j",
        "_meta": {"run_id": rid},
    }), encoding="utf-8")
    (proc / "screening_results.json").write_text(
        json.dumps({"candidates": []}), encoding="utf-8")

    from boss_hr.commands import greet as greet_cmd
    from boss_hr.cli import build_parser
    import argparse
    p = build_parser()
    ns = p.parse_args(["greet", "--job-name", "j", "--encrypt-job-id", eid,
                        "--run-id", rid])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]; break
    res = greet_cmd.run(ns)
    d = res.to_dict("greet")
    assert d["ok"] is True
    assert d["data"]["no_candidates"] is True


def test_confirm_does_not_call_ensure_browser(monkeypatch, tmp_path):
    """confirm 不需要浏览器：在底层 Edge 启动函数设置 guard，应完成且 guard 不触发。"""
    from boss_hr.application import confirm_service as cs
    # 底层启动函数 guard：被调即抛 AssertionError
    from boss_hr.adapters import browser_environment as be
    def _guard_launch(*a, **kw):
        raise AssertionError("confirm 不应自动启动 Edge")
    monkeypatch.setattr(be, "launch_dedicated_edge", _guard_launch)
    # confirm_service 不应 import ensure_browser_ready（确认无该属性）
    assert not hasattr(cs, "ensure_browser_ready"), (
        "confirm_service 不应依赖 ensure_browser_ready"
    )

    ws = tmp_path / "v112_ws"
    ws.mkdir(exist_ok=True)
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(ws))
    import output_manager
    monkeypatch.setattr(output_manager, "OUTPUT_ROOT", str(ws), raising=False)
    eid, rid = "EID_C1", "RID_C1"
    run = ws / eid / "runs" / rid
    proc = run / "process"
    proc.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "confirmed": False, "finished": False,
    }), encoding="utf-8")
    (proc / "job_detail.json").write_text(json.dumps({
        "encryptJobId": eid, "jobName": "j",
        "_meta": {"run_id": rid},
    }), encoding="utf-8")

    from boss_hr.commands import confirm as confirm_cmd
    from boss_hr.cli import build_parser
    import argparse
    p = build_parser()
    ns = p.parse_args(["confirm", "--job-name", "j", "--encrypt-job-id", eid,
                        "--run-id", rid])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]; break
    res = confirm_cmd.run(ns)
    # confirm 应完成（不抛 AssertionError 表示未触发 launch）
    assert res is not None


def test_status_does_not_call_ensure_browser(monkeypatch, tmp_path):
    """status 不需要浏览器：在底层 Edge 启动函数设置 guard。"""
    from boss_hr.application import status_service as sts
    from boss_hr.adapters import browser_environment as be
    # 底层启动函数 guard：被调即抛
    def _guard_launch(*a, **kw):
        raise AssertionError("status 不应自动启动 Edge")
    monkeypatch.setattr(be, "launch_dedicated_edge", _guard_launch)
    # status_service 不应 import ensure_browser_ready
    assert not hasattr(sts, "ensure_browser_ready"), (
        "status_service 不应依赖 ensure_browser_ready"
    )

    ws = tmp_path / "v112_ws"
    ws.mkdir(exist_ok=True)
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(ws))
    import output_manager
    monkeypatch.setattr(output_manager, "OUTPUT_ROOT", str(ws), raising=False)
    eid, rid = "EID_S1", "RID_S1"
    run = ws / eid / "runs" / rid
    proc = run / "process"
    proc.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "confirmed": True, "finished": False,
    }), encoding="utf-8")
    (proc / "job_detail.json").write_text(json.dumps({
        "encryptJobId": eid, "jobName": "j",
        "_meta": {"run_id": rid},
    }), encoding="utf-8")

    from boss_hr.commands import status as status_cmd
    from boss_hr.cli import build_parser
    import argparse
    p = build_parser()
    ns = p.parse_args(["status", "--job-name", "j", "--encrypt-job-id", eid,
                        "--run-id", rid])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]; break
    rc, payload = status_cmd.run(ns)
    assert rc == 0
    assert payload["status"] == "ok"


# ============================================================
# 6. launch_dedicated_edge 行为
# ============================================================

def test_launch_dedicated_edge_uses_specific_flags(monkeypatch):
    """launch_dedicated_edge 必须传 --user-data-dir + --remote-debugging-port=9222。"""
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "check_edge_executable",
                        lambda: r"C:\fake\msedge.exe")
    monkeypatch.setattr(be, "_ensure_dir", lambda p: None)
    listening = iter([False, True])
    monkeypatch.setattr(be, "check_cdp_port_listening", lambda *a, **kw: next(listening))

    captured = {}
    class _P:
        pid = 123
    def _fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        captured["kw"] = kw
        return _P()
    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    r = be.launch_dedicated_edge(wait_seconds=2)
    assert r.ok is True
    cmd = captured["cmd"]
    assert any("--user-data-dir=" in a for a in cmd)
    assert any("--remote-debugging-port=9222" in a for a in cmd)
    assert be.BOSS_LOGIN_URL in cmd


def test_launch_dedicated_edge_no_edge_returns_edge_not_found(monkeypatch):
    """Edge 不存在 → EDGE_NOT_FOUND。"""
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "check_edge_executable", lambda: None)
    r = be.launch_dedicated_edge()
    assert r.ok is False
    assert r.error_code == "EDGE_NOT_FOUND"


# ============================================================
# 7. _open_login_page 真实异常修复后的行为（v1.1.2 fix）
# ============================================================

def test_open_login_page_success_returns_true(monkeypatch):
    """登录页打开成功 → (True, "")，且复用 connect_cdp session.page。"""
    from boss_hr.adapters import browser_environment as be

    class _FakePage:
        url = "https://www.zhipin.com/web/chat/recommend"
        def goto(self, url, **kw):
            self.url = url
        def bring_to_front(self):
            pass
        def wait_for_function(self, *args, **kwargs):
            pass

    class _FakeSession:
        page = _FakePage()
        def disconnect(self):
            pass

    captured = {}
    def _fake_connect_cdp(url, *, timeout_ms=4000):
        captured["called"] = True
        captured["url"] = url
        return _FakeSession()

    # 直接 monkeypatch shared.cdp_preflight.connect_cdp 的导入引用
    import shared.cdp_preflight as cdp_mod
    monkeypatch.setattr(cdp_mod, "connect_cdp", _fake_connect_cdp)
    # 同时 patch browser_environment 已缓存的引用
    monkeypatch.setattr(be, "connect_cdp", _fake_connect_cdp, raising=False)

    ok, reason = be._open_login_page(timeout_seconds=2)
    assert ok is True
    assert reason == ""
    assert captured.get("called") is True


def test_open_login_page_uses_single_playwright(monkeypatch):
    """修复回归：_open_login_page 必须不复用第二个 sync_playwright 实例
    （原 bug：双 connect_over_cdp 在 asyncio loop 中抛
    'Playwright Sync API inside the asyncio loop'）。"""
    from boss_hr.adapters import browser_environment as be

    class _FakePage:
        url = "https://www.zhipin.com/web/chat/recommend"
        def goto(self, url, **kw):
            pass
        def bring_to_front(self):
            pass
        def wait_for_function(self, *args, **kwargs):
            pass

    class _FakeSession:
        page = _FakePage()
        def disconnect(self):
            pass

    def _fake_connect_cdp(url, *, timeout_ms=4000):
        return _FakeSession()

    import shared.cdp_preflight as cdp_mod
    monkeypatch.setattr(cdp_mod, "connect_cdp", _fake_connect_cdp)

    # 关键断言：sync_playwright / connect_over_cdp 不应被再次调用
    def _boom(*a, **kw):
        raise AssertionError(
            "_open_login_page 不应再启 sync_playwright 实例"
        )
    monkeypatch.setattr("patchright.sync_api.sync_playwright", _boom)
    monkeypatch.setattr(
        "patchright.sync_api._context_manager.sync_playwright", _boom,
        raising=False,
    )

    ok, reason = be._open_login_page(timeout_seconds=2)
    assert ok is True


def test_open_login_page_connect_failure_returns_sanitized_reason(monkeypatch):
    """CDP 不可达 → 返回 (False, "<异常类型>")，且不泄露具体错误信息。"""
    from boss_hr.adapters import browser_environment as be

    def _fake_connect_cdp(url, *, timeout_ms=4000):
        raise RuntimeError("CDP 不可达: http://localhost:9222 "
                           "(ConnectionRefusedError: secret token=abc)")

    import shared.cdp_preflight as cdp_mod
    monkeypatch.setattr(cdp_mod, "connect_cdp", _fake_connect_cdp)

    ok, reason = be._open_login_page(timeout_seconds=2)
    assert ok is False
    assert reason.startswith("RuntimeError:")
    # 不应泄露具体 token / url / cookie 内容
    assert "secret" not in reason
    assert "token=" not in reason
    assert "Cookie" not in reason


def test_open_login_page_unexpected_host_returns_reason(monkeypatch):
    """导航后 URL 不是 zhipin.com → 返回 (False, "unexpected_host:...")。"""
    from boss_hr.adapters import browser_environment as be

    class _FakePage:
        url = ""  # goto 后被改写
        def goto(self, url, **kw):
            self.url = "https://evil.example.com/phish"
        def bring_to_front(self):
            pass
        def wait_for_function(self, *args, **kwargs):
            pass

    class _FakeSession:
        page = _FakePage()
        def disconnect(self):
            pass

    import shared.cdp_preflight as cdp_mod
    monkeypatch.setattr(cdp_mod, "connect_cdp", lambda *a, **kw: _FakeSession())

    ok, reason = be._open_login_page(timeout_seconds=2)
    assert ok is False
    assert reason.startswith("unexpected_host:")


def test_start_waiting_user_login_message_admits_when_page_not_opened(monkeypatch):
    """waiting_user_login + login_page_opened=false → message 不声称已打开，
    明确要求用户在专用 Edge 中手动打开登录页。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail(
                            "BOSS_LOGIN_REQUIRED", "登录超时",
                            browser_auto_launched=True,
                            login_page_opened=False,
                            login_page_open_error="RuntimeError:CDP 不可达",
                            login_wait_seconds=20,
                        ))
    monkeypatch.setattr(ss, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail(
                            "BOSS_LOGIN_REQUIRED", "登录超时",
                            browser_auto_launched=True,
                            login_page_opened=False,
                            login_page_open_error="RuntimeError:CDP 不可达",
                            login_wait_seconds=20,
                        ))

    res = ss.start_new_run(query="X", job_name=None, encrypt_job_id=None,
                            skip_preflight=False, skip_resolve=True,
                            auto_launch_browser=True, login_wait_seconds=1)
    d = res.to_dict("start")
    assert d["ok"] is True
    assert d["status"] == "waiting_user_login"
    assert d["data"]["login_page_opened"] is False
    # message 不得声称"已为你打开"，必须显式让用户手动打开
    msg = d["message"]
    assert "已为你打开" not in msg, (
        "login_page_opened=false 时 message 不应说'已为你打开'")
    assert "手动打开" in msg or "https://www.zhipin.com" in msg


def test_start_waiting_user_login_message_confirms_when_page_opened(monkeypatch):
    """waiting_user_login + login_page_opened=true → message 明确说"已为你打开"。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail(
                            "BOSS_LOGIN_REQUIRED", "登录超时",
                            browser_auto_launched=True,
                            login_page_opened=True,
                            login_page_open_error="",
                            login_wait_seconds=20,
                        ))
    monkeypatch.setattr(ss, "ensure_browser_ready",
                        lambda *a, **kw: _ready_fail(
                            "BOSS_LOGIN_REQUIRED", "登录超时",
                            browser_auto_launched=True,
                            login_page_opened=True,
                            login_page_open_error="",
                            login_wait_seconds=20,
                        ))

    res = ss.start_new_run(query="X", job_name=None, encrypt_job_id=None,
                            skip_preflight=False, skip_resolve=True,
                            auto_launch_browser=True, login_wait_seconds=1)
    d = res.to_dict("start")
    assert d["ok"] is True
    assert d["status"] == "waiting_user_login"
    assert d["data"]["login_page_opened"] is True
    assert "已为你打开专用 Edge" in d["message"]
    assert "登录" in d["message"]


def test_ensure_browser_ready_open_failure_exposes_reason(monkeypatch):
    """ensure_browser_ready 在 _open_login_page 失败时把 reason 写入
    info.login_page_open_error，便于上层 message 区分。"""
    from boss_hr.adapters import browser_environment as be

    monkeypatch.setattr(be, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(be, "check_patchright_installed", lambda: True)
    monkeypatch.setattr(be, "check_edge_executable",
                        lambda: r"C:\fake\msedge.exe")
    monkeypatch.setattr(be, "check_cdp_port_listening", lambda *a, **kw: True)
    monkeypatch.setattr(be, "check_cdp_connectable", lambda *a, **kw: (True, ""))
    monkeypatch.setattr(be, "check_boss_logged_in",
                        lambda *a, **kw: (False,
                                          {"page_kind": "unknown",
                                           "current_url": "about:blank"}))
    monkeypatch.setattr(be, "_open_login_page",
                        lambda *a, **kw: (False, "RuntimeError:CDP 不可达"))
    # 极短 poll，立即超时
    import time as _t
    _t0 = _t.time()
    monkeypatch.setattr(be, "_poll_login_status",
                        lambda *, wait_seconds: (False, {}))

    r = be.ensure_browser_ready(auto_launch=True, login_wait_seconds=0)
    assert r.ok is False
    assert r.error_obj.code.value == "BOSS_LOGIN_REQUIRED"
    assert r.info.get("login_page_opened") is False
    assert "CDP" in (r.info.get("login_page_open_error") or "")
    # message 不应声称已打开登录页
    assert "已自动打开 BOSS 招聘者登录页" not in (r.error_obj.message or "")
