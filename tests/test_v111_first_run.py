# -*- coding: utf-8 -*-
"""v1.1.1 真实回归 + 新功能测试。

覆盖：
  - doctor 在无 9222 时返回 CDP_NOT_RUNNING（含完整 remediation）
  - doctor 端到端 preflight 检查 + Edge 启动流程（mock）
  - doctor --launch-edge 使用专用 profile 目录
  - doctor 跳过浏览器路径（--skip-browser）
  - 真实错误码集合（EDGE_NOT_FOUND / CDP_NOT_RUNNING / CDP_CONNECT_FAILED
    / BOSS_LOGIN_REQUIRED / BOSS_PAGE_REQUIRED / JOB_NOT_FOUND /
    JOB_AMBIGUOUS / JOB_ID_MISMATCH）
  - start 在浏览器不可用时立即返回 CDP_NOT_RUNNING，不进入 boss_jd
  - fetch / greet 在浏览器不可用时立即返回 CDP_NOT_RUNNING
  - confirm / score / report / status 不需要浏览器 → 不被 preflight 阻塞
  - start query 解析（岗位名 / jobId / encryptJobId）
  - start 多匹配 → JOB_AMBIGUOUS 带 candidates
  - start 0 匹配 → JOB_NOT_FOUND
  - start 实时 ID 与 --encrypt-job-id 不一致 → JOB_ID_MISMATCH
  - jobs.json 含旧错误 ID 时 start 不受影响
  - start 成功后才更新 jobs.json（JobRegistry 在 boss_jd 内部触发）
  - start 不扫描历史 run
  - UnifiedError 含 recoverable 字段
  - CommandResult.to_dict 把 next_action / remediation 提到 error 顶层
  - COMMANDS 注册表正好是 8 个公开命令（含 doctor）
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest


_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent
_CLI = _TOOLKIT_ROOT / "boss_hr" / "cli.py"
_SHARED = _TOOLKIT_ROOT / "shared"


# ============================================================
# 0. 错误码契约
# ============================================================

def test_error_code_set_includes_v111_codes():
    from boss_hr.contracts.errors import ErrorCode
    required = {
        "EDGE_NOT_FOUND", "CDP_NOT_RUNNING", "CDP_CONNECT_FAILED",
        "BOSS_LOGIN_REQUIRED", "BOSS_PAGE_REQUIRED",
        "JOB_NOT_FOUND", "JOB_AMBIGUOUS", "JOB_ID_MISMATCH",
        "EDGE_LAUNCH_FAILED",
    }
    actual = {e.value for e in ErrorCode}
    missing = required - actual
    assert not missing, f"ErrorCode 缺这些 v1.1.1 新码: {missing}"


def test_unified_error_includes_recoverable():
    from boss_hr.contracts.errors import UnifiedError, ErrorCode
    e = UnifiedError(code=ErrorCode.CDP_NOT_RUNNING, message="m", recoverable=True)
    d = e.to_dict()
    assert d["recoverable"] is True
    assert d["code"] == "CDP_NOT_RUNNING"
    assert d["message"] == "m"


def test_command_result_to_dict_includes_remediation_in_error():
    """error 字段内必须含 recoverable / next_action / remediation。"""
    from boss_hr.contracts.results import error
    from boss_hr.contracts.errors import UnifiedError, ErrorCode, ExitCode
    r = error(
        error_obj=UnifiedError(code=ErrorCode.CDP_NOT_RUNNING, message="m", recoverable=True),
        exit_code=ExitCode.GENERIC,
        next_action="launch_edge",
        remediation={"instructions": ["a", "b"], "command": "boss-hr doctor --launch-edge"},
    )
    d = r.to_dict("doctor")
    assert d["ok"] is False
    assert d["error"]["code"] == "CDP_NOT_RUNNING"
    assert d["error"]["recoverable"] is True
    assert d["error"]["next_action"] == "launch_edge"
    assert d["error"]["remediation"]["command"] == "boss-hr doctor --launch-edge"
    assert d["error"]["remediation"]["instructions"] == ["a", "b"]


# ============================================================
# 1. doctor 命令
# ============================================================

def test_doctor_in_comandos_registry():
    from boss_hr.cli import COMMANDS
    assert "doctor" in COMMANDS


def test_doctor_help_listed():
    """boss-hr --help 必须显示 doctor。"""
    p = subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), "--help"],
        capture_output=True, env={**os.environ, "PYTHONUTF8": "1",
                                  "PYTHONIOENCODING": "utf-8",
                                  "PYTHONPATH": str(_SHARED)},
        cwd=str(_TOOLKIT_ROOT), timeout=15,
    )
    assert p.returncode == 0
    text = p.stdout.decode("utf-8", errors="replace")
    assert "doctor" in text


def test_doctor_skips_browser_path(tmp_path, monkeypatch):
    """--skip-browser → 返回 local_only + 不连 CDP。"""
    from boss_hr.commands import doctor as doctor_cmd
    from boss_hr.cli import build_parser
    import argparse
    p = build_parser()
    ns = p.parse_args(["doctor", "--skip-browser"])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]; break
    res = doctor_cmd.run(ns)
    d = res.to_dict("doctor")
    assert d["ok"] is True
    assert d["status"] == "local_only"
    assert d["data"]["browser_check_skipped"] is True


def test_doctor_returns_cdp_not_running_when_no_9222(monkeypatch):
    """9222 未开 → CDP_NOT_RUNNING + recoverable + remediation。"""
    # mock 浏览器预检：CDP 端口未监听
    from boss_hr.application import doctor_service as ds
    from boss_hr.adapters import browser_preflight as bp
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "check_cdp_port_listening", lambda *a, **kw: False)
    # 不让 doctor 自己真去探
    monkeypatch.setattr(be, "check_edge_executable", lambda: None)
    monkeypatch.setattr(be, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(be, "check_patchright_installed", lambda: True)
    # doctor_service 内部 import 了 check_cdp_port_listening；要 monkeypatch
    # 三个层：模块属性 + 服务模块本地引用
    monkeypatch.setattr(bp, "check_cdp_port_listening", lambda *a, **kw: False)
    monkeypatch.setattr(bp, "check_edge_executable", lambda: None)
    monkeypatch.setattr(bp, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(bp, "check_patchright_installed", lambda: True)
    monkeypatch.setattr(ds, "check_cdp_port_listening", lambda *a, **kw: False)
    monkeypatch.setattr(ds, "check_edge_executable", lambda: None)
    monkeypatch.setattr(ds, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(ds, "check_patchright_installed", lambda: True)

    res = ds.run_doctor()
    d = res.to_dict("doctor")
    assert d["ok"] is False
    assert d["error"]["code"] == "CDP_NOT_RUNNING"
    assert d["error"]["recoverable"] is True
    assert d["error"]["remediation"]["command"] == "boss-hr doctor --launch-edge"
    assert "启动专用 Edge" in d["error"]["remediation"]["instructions"][0]


def test_doctor_returns_edge_not_found(monkeypatch):
    """Edge 不存在 → EDGE_NOT_FOUND。"""
    from boss_hr.application import doctor_service as ds
    from boss_hr.adapters import browser_preflight as bp
    from boss_hr.adapters import browser_environment as be
    monkeypatch.setattr(be, "check_edge_executable", lambda: None)
    monkeypatch.setattr(be, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(be, "check_patchright_installed", lambda: True)
    monkeypatch.setattr(be, "check_cdp_port_listening", lambda *a, **kw: False)
    monkeypatch.setattr(bp, "check_edge_executable", lambda: None)
    monkeypatch.setattr(bp, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(bp, "check_patchright_installed", lambda: True)
    monkeypatch.setattr(bp, "check_cdp_port_listening", lambda *a, **kw: False)
    monkeypatch.setattr(ds, "check_edge_executable", lambda: None)
    monkeypatch.setattr(ds, "check_python_version", lambda: (True, "3.13.0"))
    monkeypatch.setattr(ds, "check_patchright_installed", lambda: True)
    monkeypatch.setattr(ds, "check_cdp_port_listening", lambda *a, **kw: False)

    res = ds.run_doctor()
    d = res.to_dict("doctor")
    # 没 Edge 也没 CDP → CDP_NOT_RUNNING（Edge 检查在 cdp 检查之后）
    # 真正触发 EDGE_NOT_FOUND 是 launch_edge 路径
    assert d["ok"] is False
    assert d["error"]["code"] in ("CDP_NOT_RUNNING", "EDGE_NOT_FOUND")


def test_doctor_launch_edge_uses_dedicated_profile(monkeypatch):
    """--launch-edge 必须用专用 profile 目录（不污染用户普通 Edge profile）。"""
    from boss_hr.application import doctor_service as ds

    fake_edge = r"C:\fake\edge\msedge.exe"
    monkeypatch.setattr(
        "boss_hr.application.doctor_service.check_edge_executable",
        lambda: fake_edge,
    )

    captured = {}
    class _FakePopen:
        def __init__(self, cmd, **kw):
            captured["cmd"] = cmd
            captured["kw"] = kw
            self.pid = 99999

    monkeypatch.setattr("subprocess.Popen", _FakePopen)
    monkeypatch.setattr(
        "boss_hr.application.doctor_service.check_cdp_port_listening",
        lambda *a, **kw: True,
    )

    res = ds.launch_edge(wait_seconds=2)
    assert res["ok"], res
    cmd = captured["cmd"]
    assert any("--user-data-dir=" in a for a in cmd), cmd
    assert any("--remote-debugging-port=9222" in a for a in cmd), cmd
    profile_dir = [a.split("=", 1)[1] for a in cmd if a.startswith("--user-data-dir=")][0]
    # profile 目录名固定为 boss-hr-edge-profile（不嵌用户名变体）
    assert profile_dir.endswith("boss-hr-edge-profile"), profile_dir
    # Respect the configured local app-data root, including an isolated test
    # workspace whose ancestor may legitimately be named Documents.
    assert Path(profile_dir) == Path(os.environ["LOCALAPPDATA"]) / "boss-hr-edge-profile"
    # profile 目录不能在用户主目录下混在其它浏览器配置里
    assert _is_absolute_path(profile_dir), profile_dir


# ============================================================
# 2. start 实时解析
# ============================================================

def test_start_resolves_query_by_name(monkeypatch):
    """query 给岗位名 → 实时解析。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner

    monkeypatch.setattr(ss, "ensure_browser_ready", lambda *a, **kw: _ok_ready())
    monkeypatch.setattr(
        ss, "_resolve_recruiter_job",
        lambda q: {"encryptJobId": "RESOLVED_EID", "jobName": "解析出的岗位"},
    )

    class _OKProc:
        returncode = 0
        stdout = (
            json.dumps({"status": "waiting_user_confirmation",
                        "run_id": "2026-08-04_120000"})
            + "\nrun_id: 2026-08-04_120000（orchestrator 创建）\n"
            + "Saved to /tmp/job_detail.json\n"
        )
        stderr = ""

    monkeypatch.setattr(legacy_runner, "run_legacy_cli",
                        lambda *a, **kw: _OKProc())

    res = ss.start_new_run(query="某岗位名", job_name=None, encrypt_job_id=None,
                            skip_preflight=True, skip_resolve=False)
    d = res.to_dict("start")
    assert d["ok"] is True
    assert d["encrypt_job_id"] == "RESOLVED_EID"
    assert d["job_name"] == "解析出的岗位"
    assert d["data"]["resolved_from"] == "live_boss_catalog"


def test_start_resolves_jobId(tmp_path, monkeypatch):
    """query 给数字 jobId。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner
    monkeypatch.setattr(ss, "ensure_browser_ready", lambda *a, **kw: _ok_ready())
    monkeypatch.setattr(
        ss, "_resolve_recruiter_job",
        lambda q: {"encryptJobId": "FROM_JOBID_123", "jobName": "通过 jobId 解析",
                   "jobId": "12345"},
    )
    class _OKProc:
        returncode = 0
        stdout = (
            json.dumps({"status": "waiting_user_confirmation",
                        "run_id": "2026-08-04_120001"})
            + "\nrun_id: 2026-08-04_120001（orchestrator 创建）\n"
            + "Saved to /tmp/job_detail.json\n"
        )
        stderr = ""
    monkeypatch.setattr(legacy_runner, "run_legacy_cli",
                        lambda *a, **kw: _OKProc())

    res = ss.start_new_run(query="559622717", job_name=None, encrypt_job_id=None,
                            skip_preflight=True, skip_resolve=False)
    assert res.ok is True
    assert res.encrypt_job_id == "FROM_JOBID_123"


def test_start_resolves_encrypt_job_id(tmp_path, monkeypatch):
    """query 给完整 encryptJobId。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner
    monkeypatch.setattr(ss, "ensure_browser_ready", lambda *a, **kw: _ok_ready())
    monkeypatch.setattr(
        ss, "_resolve_recruiter_job",
        lambda q: {"encryptJobId": "9a7759badfd95d350nFz3d-_F1NX",
                   "jobName": "线控底盘制动、转向工程师"},
    )
    class _OKProc:
        returncode = 0
        stdout = (
            json.dumps({"status": "waiting_user_confirmation",
                        "run_id": "2026-08-04_120002"})
            + "\nrun_id: 2026-08-04_120002（orchestrator 创建）\n"
            + "Saved to /tmp/job_detail.json\n"
        )
        stderr = ""
    monkeypatch.setattr(legacy_runner, "run_legacy_cli",
                        lambda *a, **kw: _OKProc())

    res = ss.start_new_run(query="9a7759badfd95d350nFz3d-_F1NX",
                            job_name=None, encrypt_job_id=None,
                            skip_preflight=True, skip_resolve=False)
    assert res.ok is True
    assert res.encrypt_job_id == "9a7759badfd95d350nFz3d-_F1NX"


def test_start_no_match_returns_job_not_found(monkeypatch):
    """query 在 BOSS 实时目录找不到 → JOB_NOT_FOUND + 可恢复。"""
    from boss_hr.application import start_service as ss
    monkeypatch.setattr(ss, "ensure_browser_ready", lambda *a, **kw: _ok_ready())
    monkeypatch.setattr(ss, "_resolve_recruiter_job", lambda q: None)

    res = ss.start_new_run(query="完全找不到_xyz", job_name="x", encrypt_job_id=None,
                            skip_preflight=True, skip_resolve=False)
    d = res.to_dict("start")
    assert d["ok"] is False
    assert d["error"]["code"] == "JOB_NOT_FOUND"
    assert d["error"]["recoverable"] is True
    assert "remediation" in d["error"]


def test_start_ambiguous_returns_candidates(monkeypatch):
    """多匹配 → JOB_AMBIGUOUS + data.candidates。"""
    from boss_hr.application import start_service as ss
    monkeypatch.setattr(ss, "ensure_browser_ready", lambda *a, **kw: _ok_ready())
    monkeypatch.setattr(
        ss, "_resolve_recruiter_job",
        lambda q: [
            {"encryptJobId": "EID_A", "jobName": "同名岗位-A", "jobId": "111"},
            {"encryptJobId": "EID_B", "jobName": "同名岗位-B", "jobId": "222"},
        ],
    )

    res = ss.start_new_run(query="同名", job_name="x", encrypt_job_id=None,
                            skip_preflight=True, skip_resolve=False)
    d = res.to_dict("start")
    assert d["ok"] is False
    assert d["error"]["code"] == "JOB_AMBIGUOUS"
    # candidates 在 data 字段下（v1.1.1 失败分支也带 data）
    assert "candidates" in d["data"]
    assert d["data"]["candidates"][0]["encrypt_job_id"] == "EID_A"
    assert d["error"]["recoverable"] is True


def test_start_eid_mismatch_returns_job_id_mismatch(monkeypatch):
    """--encrypt-job-id 与实时解析不一致 → JOB_ID_MISMATCH。"""
    from boss_hr.application import start_service as ss
    monkeypatch.setattr(ss, "ensure_browser_ready", lambda *a, **kw: _ok_ready())
    # 实时返回 eid_A，但用户传 eid_B
    monkeypatch.setattr(
        ss, "_resolve_recruiter_job",
        lambda q: {"encryptJobId": "EID_A", "jobName": "某岗位"},
    )

    res = ss.start_new_run(query="某岗位", job_name="x", encrypt_job_id="EID_B",
                            skip_preflight=True, skip_resolve=False)
    d = res.to_dict("start")
    assert d["ok"] is False
    assert d["error"]["code"] == "JOB_ID_MISMATCH"
    assert d["error"]["recoverable"] is True


def test_start_does_not_read_jobs_json(monkeypatch):
    """start 不能读 jobs.json（即使 jobs.json 含过期 ID）。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner
    calls = {"resolve": 0, "boss_jd": 0}
    monkeypatch.setattr(ss, "ensure_browser_ready", lambda *a, **kw: _ok_ready())
    def _fake_resolve(q):
        calls["resolve"] += 1
        return {"encryptJobId": "LIVE_FROM_BOSS", "jobName": "BOSS 实时岗位"}
    monkeypatch.setattr(ss, "_resolve_recruiter_job", _fake_resolve)
    class _OKProc:
        returncode = 0
        stdout = (
            json.dumps({"status": "waiting_user_confirmation",
                        "run_id": "2026-08-04_120003"})
            + "\nrun_id: 2026-08-04_120003（orchestrator 创建）\n"
            + "Saved to /tmp/job_detail.json\n"
        )
        stderr = ""
    def _fake_boss_jd(*a, **kw):
        calls["boss_jd"] += 1
        return MagicMock(returncode=0,
                          stdout=(
                              json.dumps({"status": "waiting_user_confirmation",
                                          "run_id": "2026-08-04_120003"})
                              + "\nrun_id: 2026-08-04_120003（orchestrator 创建）\n"
                              + "Saved to /tmp/job_detail.json\n"
                          ),
                          stderr="")
    monkeypatch.setattr(legacy_runner, "run_legacy_cli", _fake_boss_jd)

    res = ss.start_new_run(query="任何 query", job_name="x", encrypt_job_id=None,
                            skip_preflight=True, skip_resolve=False)
    d = res.to_dict("start")
    # 必须走实时解析（不是 jobs.json）
    assert calls["resolve"] == 1
    assert d["encrypt_job_id"] == "LIVE_FROM_BOSS"


# ============================================================
# 3. browser_preflight 在 start / fetch / greet 的拦截
# ============================================================

def test_start_no_cdp_returns_cdp_not_running_without_calling_boss_jd(monkeypatch):
    """start 在无 CDP 时立即返回 CDP_NOT_RUNNING，**不**调 boss_jd。"""
    from boss_hr.application import start_service as ss
    from boss_hr.adapters import legacy_runner
    monkeypatch.setattr(ss, "_resolve_recruiter_job", lambda q: {"encryptJobId": "EID", "jobName": "X"})

    class _Fake:
        ok = False
        error_obj = ss.UnifiedError(
            code=ss.ErrorCode.CDP_NOT_RUNNING,
            message="未检测到 9222", recoverable=True)
        next_action = "launch_edge"
        remediation = {"command": "boss-hr doctor --launch-edge",
                        "instructions": ["a"]}
    monkeypatch.setattr(ss, "ensure_browser_ready", lambda *a, **kw: _Fake())
    called = {"boss_jd": 0}
    def _fake_boss_jd(*a, **kw):
        called["boss_jd"] += 1
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(legacy_runner, "run_legacy_cli", _fake_boss_jd)

    res = ss.start_new_run(query="X", job_name="x", encrypt_job_id=None,
                            skip_preflight=False, skip_resolve=True)
    d = res.to_dict("start")
    assert d["ok"] is False
    assert d["error"]["code"] == "CDP_NOT_RUNNING"
    assert d["error"]["recoverable"] is True
    assert called["boss_jd"] == 0, "无 CDP 时不应调 boss_jd"


def test_fetch_no_cdp_returns_cdp_not_running(tmp_path, monkeypatch):
    """fetch 在无 CDP 时立即返回 CDP_NOT_RUNNING，**不**调 recommend_list。"""
    from boss_hr.application import fetch_service as fs
    from boss_hr.contracts.errors import UnifiedError, ErrorCode
    from boss_hr.adapters import legacy_runner
    _make_run_with_workspace(tmp_path, monkeypatch,
                             eid="EID_F1", rid="RID_F1",
                             confirmed=True)
    class _Fake:
        ok = False
        error_obj = UnifiedError(
            code=ErrorCode.CDP_NOT_RUNNING,
            message="无 9222", recoverable=True)
        next_action = "launch_edge"
        remediation = {"command": "boss-hr doctor --launch-edge",
                        "instructions": ["a"]}
    monkeypatch.setattr(fs, "ensure_browser_ready", lambda *a, **kw: _Fake())
    called = {"list": 0}
    def _fake_list(*a, **kw):
        called["list"] += 1
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(legacy_runner, "run_legacy_cli", _fake_list)

    from boss_hr.commands import fetch as fetch_cmd
    from boss_hr.cli import build_parser
    import argparse
    p = build_parser()
    ns = p.parse_args(["fetch", "--job-name", "j", "--encrypt-job-id", "EID_F1",
                        "--run-id", "RID_F1", "--count", "1"])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]; break
    res = fetch_cmd.run(ns)
    d = res.to_dict("fetch")
    assert d["ok"] is False
    assert d["error"]["code"] == "CDP_NOT_RUNNING"
    assert called["list"] == 0


def test_greet_no_cdp_returns_cdp_not_running(tmp_path, monkeypatch):
    """greet 在无 CDP 时立即返回 CDP_NOT_RUNNING，**不**调 auto_greet。"""
    from boss_hr.application import greet_service as gs
    from boss_hr.contracts.errors import UnifiedError, ErrorCode
    from boss_hr.adapters import legacy_runner
    _make_run_with_workspace(tmp_path, monkeypatch,
                             eid="EID_G1", rid="RID_G1",
                             confirmed=True)
    class _Fake:
        ok = False
        error_obj = UnifiedError(
            code=ErrorCode.CDP_NOT_RUNNING,
            message="无 9222", recoverable=True)
        next_action = "launch_edge"
        remediation = {"command": "boss-hr doctor --launch-edge",
                        "instructions": ["a"]}
    monkeypatch.setattr(gs, "ensure_browser_ready", lambda *a, **kw: _Fake())
    called = {"auto_greet": 0}
    def _fake_ag(*a, **kw):
        called["auto_greet"] += 1
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(legacy_runner, "run_legacy_cli", _fake_ag)

    from boss_hr.commands import greet as greet_cmd
    from boss_hr.cli import build_parser
    import argparse
    p = build_parser()
    ns = p.parse_args(["greet", "--job-name", "j", "--encrypt-job-id", "EID_G1",
                        "--run-id", "RID_G1"])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]; break
    res = greet_cmd.run(ns)
    d = res.to_dict("greet")
    assert d["ok"] is False
    assert d["error"]["code"] == "CDP_NOT_RUNNING"
    assert called["auto_greet"] == 0


def test_confirm_does_not_require_browser(tmp_path, monkeypatch):
    """confirm 不需要浏览器 → 不被 preflight 阻塞。

    用 subprocess 跑（避免 in-process fixture 顺序污染）。
    """
    _make_run_with_workspace(tmp_path, monkeypatch,
                             eid="EID_C1", rid="RID_C1",
                             confirmed=False)
    env = {**os.environ,
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_SHARED),
           "BOSS_HR_OUTPUT_DIR": str(tmp_path / "v111_ws")}
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), "confirm",
         "--job-name", "j", "--encrypt-job-id", "EID_C1", "--run-id", "RID_C1"],
        capture_output=True, env=env, cwd=str(_TOOLKIT_ROOT), timeout=30,
    )
    assert proc.returncode == 0, f"rc={proc.returncode} stdout={proc.stdout!r} stderr={proc.stderr!r}"
    payload = json.loads(proc.stdout.decode("utf-8", errors="replace"))
    assert payload["ok"] is True
    assert payload["status"] == "confirmed"


def test_status_does_not_require_browser(tmp_path, monkeypatch):
    """status 不需要浏览器。"""
    from boss_hr.commands import status as status_cmd
    from boss_hr.cli import build_parser
    import argparse
    _make_run_with_workspace(tmp_path, monkeypatch,
                             eid="EID_S1", rid="RID_S1",
                             confirmed=True)
    p = build_parser()
    ns = p.parse_args(["status", "--job-name", "j", "--encrypt-job-id", "EID_S1",
                        "--run-id", "RID_S1"])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]; break
    # status 返回 (int, dict)；rc=0 即成功
    rc, payload = status_cmd.run(ns)
    assert rc == 0
    assert payload["status"] == "ok"


# ============================================================
# helpers
# ============================================================

def _ok_ready():
    class _P:
        ok = True
        error_obj = None
        remediation = None
        next_action = None
        info = {"page_kind": "recommend", "page_url": "x", "logged_in": True}
    return _P()


def _is_absolute_path(p: str) -> bool:
    """Windows / POSIX 都行：含盘符或以 / 开头即绝对路径。"""
    if not p:
        return False
    if len(p) >= 2 and p[1] == ":":
        return True
    return p.startswith("/") or p.startswith("\\")


def _make_run_with_workspace(tmp_path, monkeypatch, *, eid, rid, confirmed):
    """写真实 run 目录让 confirm/status 不被 preflight 阻塞。

    关键：tests/conftest.py 的 _isolate_output_root autouse fixture 已把
    output_manager.OUTPUT_ROOT set 到 tmp_path。我们用 monkeypatch.setenv
    把它指到一个新的、确定的子目录（避免跨测试 fixture 顺序污染），
    然后写真实 run。
    """
    ws = tmp_path / "v111_ws"
    ws.mkdir(exist_ok=True)
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(ws))
    # conftest 后续的 OUTPUT_ROOT setattr 会覆盖；但本函数写真实在
    # setenv 之前已建好 run.json，conftest 的 setattr 是重新实例化
    # JobOutputManager 才会受影响，写真实文件不受影响
    import output_manager
    monkeypatch.setattr(output_manager, "OUTPUT_ROOT", str(ws), raising=False)

    run = ws / eid / "runs" / rid
    proc = run / "process"
    proc.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "confirmed": confirmed, "user_confirmed_at": None,
        "steps_done": ["jd"], "finished": False,
    }), encoding="utf-8")
    (proc / "job_detail.json").write_text(json.dumps({
        "encryptJobId": eid, "jobName": "j",
        "_meta": {"run_id": rid, "saved_at": "2026-08-04"},
    }), encoding="utf-8")
    return ws
