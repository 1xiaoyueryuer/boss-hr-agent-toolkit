# -*- coding: utf-8 -*-
"""boss-hr fetch 命令测试（tests/cli/）

通过 subprocess 调 python boss_hr/cli.py fetch ...，用 monkeypatch
monkeypatch `boss_hr.adapters.legacy_runner.run_legacy_cli` 返回 fake
LegacyRunResult 模拟子进程行为（不连真实 BOSS / 真实浏览器）。

24 个用例覆盖：
  1. 未 confirmed → rc=20
  2. run 不存在
  3. encrypt_job_id 不匹配
  4. 缺 run_id
  5. 缺 encrypt_job_id
  6. count=0
  7. count 负数
  8. recommend_list 先于 recommend_download
  9. list 失败时绝不调用 download
  10. list 成功后才调用 download
  11. count 正确映射给两个旧脚本（都收 --max N）
  12. 成功统计 listed/downloaded/failed
  13. 只读取当前 run 的产物
  14. 不读取 current_run.json
  15. 不扫描最新 run
  16. 不从其他 run 借候选人
  17. 不触发 prepare_scoring_inputs
  18. 不触发 score
  19. 不触发 report 或 greet
  20. 不创建 spec
  21. 重复执行行为与旧脚本一致（list 覆盖 / download 跳过已 success）
  22. 新旧产物核心内容等价（产物 JSON 字段一致）
  23. list 成功、download 失败时保留 list 产物
  24. run.json 和 state 文件副作用与旧实现一致
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import pytest


_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent.parent
_CLI = _TOOLKIT_ROOT / "boss_hr" / "cli.py"
_SHARED = _TOOLKIT_ROOT / "shared"


# ============================================================
# fake LegacyRunResult
# ============================================================

class FakeLegacyResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "",
                 report_file: Optional[str] = None):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.report_file = report_file


def _decode(b) -> str:
    if b is None:
        return ""
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return b


# ============================================================
# 真实 run 子进程 helper（不走 mock；不连真实 BOSS，只测 CLI 解析 + 守卫）
# ============================================================

def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """通过 subprocess 调 cli.py（用于不依赖 monkeypatch 的边界测试）。"""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_SHARED)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), *args],
        capture_output=True, env=env, cwd=str(_TOOLKIT_ROOT), timeout=30,
    )


def _run_cli_inproc(*, job_name: str, encrypt_job_id: str, run_id: str,
                     count: int = 10):
    """直接 import fetch_cmd.run() 并执行（让 monkeypatch 生效）。

    返回 namedtuple-like 对象：有 .returncode / .stdout 属性（与 subprocess 风格类似）。
    """
    from collections import namedtuple
    from boss_hr.commands import fetch as fetch_cmd
    import argparse
    from boss_hr.cli import build_parser
    p = build_parser()
    ns = p.parse_args([
        "fetch",
        "--job-name", job_name,
        "--encrypt-job-id", encrypt_job_id,
        "--run-id", run_id,
        "--count", str(count),
    ])
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]
            break
    result = fetch_cmd.run(ns)
    Proc = namedtuple("FakeProc", ["returncode", "stdout", "stderr"])
    return Proc(
        returncode=int(result.exit_code),
        stdout=json.dumps(result.to_dict("fetch"), ensure_ascii=False),
        stderr="",
    )


# ============================================================
# mock run_legacy_cli 的 fixture
# ============================================================

@pytest.fixture
def fetch_mocks(tmp_path, monkeypatch):
    """fixture：建一个 run（confirmed=true）；mock recommend_list + recommend_download。

    返回 (tmp_path, eid, job_name, target, mock_calls, write_outputs)。
    """
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_fetch"
    job_name = "fetch_test_job"
    target = "2026-08-03_120000"
    other = "2026-08-02_120000"

    # 建 target run（confirmed=true）
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid,
        "started_at": "2026-08-03 12:00:00",
        "confirmed": True, "user_confirmed_at": "2026-08-03 12:01:00",
        "steps_done": ["jd"],
        "last_step": "jd",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (process_dir / "job_detail.json").write_text(json.dumps({
        "jobName": job_name, "encryptJobId": eid,
    }, ensure_ascii=False), encoding="utf-8")
    # 写一份 state/ 目录让 store 调用不报错
    state_dir = tmp_path / eid / "state"
    state_dir.mkdir(exist_ok=True)

    # 写另一 run（诱饵用）
    (tmp_path / eid / "runs" / other).mkdir(parents=True, exist_ok=True)
    (tmp_path / eid / "runs" / other / "run.json").write_text(json.dumps({
        "run_id": other, "encrypt_job_id": eid,
        "started_at": "2026-08-02 12:00:00",
        "confirmed": True,
        "steps_done": ["jd", "download", "score"],
        "last_step": "score",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")

    mock_calls: list[dict] = []

    def _fake_run_legacy_cli(tool, args, *, timeout=60, **kwargs):
        # 记录调用
        mock_calls.append({"tool": tool, "args": list(args), "timeout": timeout})
        if tool == "recommend_list":
            # 写一份真实 list 产物（5 个候选人）
            list_path = process_dir / "recommend_geek_ids.json"
            list_path.write_text(json.dumps([
                {"encryptGeekId": f"gid_a_{i:03d}", "name": f"姓名{i}",
                 "geekCard": {"encryptJobId": eid, "securityId": f"sec_{i}"}}
                for i in range(5)
            ], ensure_ascii=False), encoding="utf-8")
            return FakeLegacyResult(returncode=0)
        if tool == "recommend_download":
            # 写真实 download 产物（3 成功 + 2 失败）
            new_resumes_path = process_dir / "new_resumes.json"
            failed_resumes_path = process_dir / "failed_resumes.json"
            new_resumes = [
                {"ok": True, "name": f"姓名{i}", "_meta": {"encrypt_geek_id": f"gid_a_{i:03d}"}}
                for i in range(3)
            ]
            failed_resumes = [
                {"ok": False, "name": f"姓名{i}", "reason": "已达查看上限", "encrypt_geek_id": f"gid_a_{i:03d}"}
                for i in range(3, 5)
            ]
            new_resumes_path.write_text(json.dumps(new_resumes, ensure_ascii=False), encoding="utf-8")
            failed_resumes_path.write_text(json.dumps(failed_resumes, ensure_ascii=False), encoding="utf-8")
            return FakeLegacyResult(returncode=0)
        return FakeLegacyResult(returncode=99)

    monkeypatch.setattr(
        "boss_hr.adapters.legacy_runner.run_legacy_cli",
        _fake_run_legacy_cli,
    )
    return tmp_path, eid, job_name, target, other, mock_calls


# ============================================================
# 1. 未 confirmed → rc=20
# ============================================================

def test_fetch_not_confirmed(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_nc"
    job = "nc_job"
    target = "2026-08-03_120000"
    run_dir = tmp_path / eid / "runs" / target
    (run_dir / "process").mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid,
        "confirmed": False, "user_confirmed_at": None,
        "steps_done": [], "last_step": None,
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "process" / "job_detail.json").write_text(
        json.dumps({"jobName": job, "encryptJobId": eid}, ensure_ascii=False),
        encoding="utf-8")
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc.returncode == 20
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert p["error"]["code"] == "AWAITING_CONFIRMATION"


# ============================================================
# 2. run 不存在
# ============================================================

def test_fetch_run_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    proc = _run_cli_inproc(job_name="x", encrypt_job_id="test_eid_x", run_id="9999-99-99_999999", count=5)
    assert proc.returncode == 23
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert p["error"]["code"] == "RUN_NOT_FOUND"


# ============================================================
# 3. encrypt_job_id 不匹配
# ============================================================

def test_fetch_encrypt_job_id_mismatch(fetch_mocks):
    tmp_path, eid, job, target, other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id="wrong_eid_xyz", run_id=target, count=5)
    assert proc.returncode == 23
    p = json.loads(_decode(proc.stdout))
    assert p["error"]["code"] == "RUN_NOT_FOUND"
    # 旧子脚本不应被调
    assert mock_calls == []


# ============================================================
# 4. 缺 run_id
# ============================================================

def test_fetch_missing_run_id(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    # run_id 缺失 → argparse.error → rc=2（不是业务层）
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid,
                            run_id=None, count=5) if False else subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), "fetch",
         "--job-name", job, "--encrypt-job-id", eid, "--count", "5"],
        capture_output=True, env={**os.environ, "PYTHONUTF8": "1",
                                  "PYTHONIOENCODING": "utf-8",
                                  "PYTHONPATH": str(_SHARED),
                                  "BOSS_HR_OUTPUT_DIR": str(tmp_path)},
        cwd=str(_TOOLKIT_ROOT), timeout=15,
    )
    assert proc.returncode == 2


# ============================================================
# 5. 缺 encrypt_job_id（无 env）
# ============================================================

def test_fetch_missing_encrypt_job_id(tmp_path, monkeypatch, fetch_mocks):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    _, eid, job, target, _, _ = fetch_mocks
    # 不传 --encrypt-job-id，env 也不设
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_SHARED), "BOSS_HR_OUTPUT_DIR": str(tmp_path)}
    env.pop("BOSS_HR_ENCRYPT_JOB_ID", None)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), "fetch",
         "--job-name", job, "--run-id", target, "--count", "5"],
        capture_output=True, env=env, cwd=str(_TOOLKIT_ROOT), timeout=30,
    )
    assert proc.returncode == 2
    assert "--encrypt-job-id" in _decode(proc.stderr)


# ============================================================
# 6. count=0
# ============================================================

def test_fetch_count_zero(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=0)
    assert proc.returncode == 1
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert "count" in p["error"]["message"]
    assert mock_calls == []


# ============================================================
# 7. count 负数
# ============================================================

def test_fetch_count_negative(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=-1)
    assert proc.returncode == 1
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert "count" in p["error"]["message"]
    assert mock_calls == []


# ============================================================
# 8. recommend_list 先于 recommend_download
# ============================================================

def test_fetch_calls_list_before_download(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc.returncode == 0
    # mock_calls 记录顺序
    assert len(mock_calls) == 2
    assert mock_calls[0]["tool"] == "recommend_list"
    assert mock_calls[1]["tool"] == "recommend_download"


# ============================================================
# 9. list 失败时绝不调用 download
# ============================================================

def test_fetch_list_failure_skips_download(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_lf"
    job = "lf_job"
    target = "2026-08-03_120000"
    # 建 run
    run_dir = tmp_path / eid / "runs" / target
    (run_dir / "process").mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
        "steps_done": ["jd"], "last_step": "jd",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "process" / "job_detail.json").write_text(
        json.dumps({"jobName": job, "encryptJobId": eid}, ensure_ascii=False),
        encoding="utf-8")

    mock_calls: list = []
    def _fake(tool, args, **kwargs):
        mock_calls.append(tool)
        if tool == "recommend_list":
            return FakeLegacyResult(returncode=20)  # 未 confirmed
        return FakeLegacyResult(returncode=0)
    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake)

    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    # list 失败 → 透传 rc 20
    assert proc.returncode == 20
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    # download 绝不被调
    assert mock_calls == ["recommend_list"]


# ============================================================
# 10. list 成功后才调用 download
# ============================================================

def test_fetch_download_only_after_list_success(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc.returncode == 0
    tools_called = [c["tool"] for c in mock_calls]
    assert tools_called.index("recommend_list") < tools_called.index("recommend_download")


# ============================================================
# 11. count 正确映射给两个旧脚本（都收 --max N）
# ============================================================

def test_fetch_count_maps_to_max_for_both(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_cm"
    job = "cm_job"
    target = "2026-08-03_120000"
    run_dir = tmp_path / eid / "runs" / target
    (run_dir / "process").mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
        "steps_done": ["jd"], "last_step": "jd",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "process" / "job_detail.json").write_text(
        json.dumps({"jobName": job, "encryptJobId": eid}, ensure_ascii=False),
        encoding="utf-8")

    mock_calls: list = []
    def _fake(tool, args, **kwargs):
        mock_calls.append({"tool": tool, "args": list(args)})
        if tool == "recommend_list":
            (run_dir / "process" / "recommend_geek_ids.json").write_text(
                json.dumps([{"encryptGeekId": "g1"}], ensure_ascii=False),
                encoding="utf-8")
            return FakeLegacyResult(returncode=0)
        return FakeLegacyResult(returncode=0)
    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake)

    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=7)
    assert proc.returncode == 0
    # 两个子脚本都收到 --max 7
    list_args = mock_calls[0]["args"]
    dl_args = mock_calls[1]["args"]
    assert "--max" in list_args
    # Listing buffers twice the requested count; download still caps at 7.
    assert list_args[list_args.index("--max") + 1] == "14"
    assert "--max" in dl_args
    assert dl_args[dl_args.index("--max") + 1] == "7"


# ============================================================
# 12. 成功统计 listed/downloaded/failed
# ============================================================

def test_fetch_success_stats(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "candidates_fetched"
    assert p["data"]["requested_count"] == 5
    assert p["data"]["listed_count"] == 5
    assert p["data"]["downloaded_count"] == 3
    assert p["data"]["failed_count"] == 2
    assert p["next_action"] == "score"


# ============================================================
# 13. 只读取当前 run 的产物
# ============================================================

def test_fetch_only_reads_current_run_files(fetch_mocks):
    tmp_path, eid, job, target, other, _ = fetch_mocks
    # 在 other run 也写一份 list 产物（诱饵）
    other_path = tmp_path / eid / "runs" / other / "process" / "recommend_geek_ids.json"
    other_path.parent.mkdir(parents=True, exist_ok=True)
    other_path.write_text(json.dumps([{"encryptGeekId": "诱饵_gid", "name": "诱饵"}],
                                       ensure_ascii=False), encoding="utf-8")
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    p = json.loads(_decode(proc.stdout))
    # 必须是 target run 的 5 个，不是 other run 的诱饵
    assert p["data"]["listed_count"] == 5
    assert p["data"]["downloaded_count"] == 3
    assert p["data"]["failed_count"] == 2
    # 关键：listed_count 来自 target run 文件（mock 写 target 5 个），不是 other


# ============================================================
# 14. 不读取 current_run.json
# ============================================================

def test_fetch_does_not_read_current_run_json(fetch_mocks):
    tmp_path, eid, job, target, other, _ = fetch_mocks
    # legacy state/current_run.json 指 other（latest）
    legacy = tmp_path / eid / "state" / "current_run.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"current_run_id": other}), encoding="utf-8")
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    p = json.loads(_decode(proc.stdout))
    assert p["run_id"] == target  # 不是 other


# ============================================================
# 15. 不扫描最新 run
# ============================================================

def test_fetch_does_not_pick_latest_run(fetch_mocks):
    tmp_path, eid, job, target, other, _ = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    p = json.loads(_decode(proc.stdout))
    assert p["run_id"] == target
    assert p["run_id"] != other


# ============================================================
# 16. 不从其他 run 借候选人
# ============================================================

def test_fetch_does_not_borrow_other_run(fetch_mocks):
    tmp_path, eid, job, target, other, _ = fetch_mocks
    # other run 已有诱饵 list + 诱饵 new_resumes
    other_process = tmp_path / eid / "runs" / other / "process"
    other_process.mkdir(parents=True, exist_ok=True)
    (other_process / "recommend_geek_ids.json").write_text(
        json.dumps([{"encryptGeekId": "诱饵_gid", "name": "诱饵"}], ensure_ascii=False),
        encoding="utf-8")
    (other_process / "new_resumes.json").write_text(
        json.dumps([{"ok": True, "name": "诱饵"}], ensure_ascii=False),
        encoding="utf-8")

    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    p = json.loads(_decode(proc.stdout))
    # 5 个 listed（target 的），不是 other 的诱饵
    assert p["data"]["listed_count"] == 5
    # 3 个 downloaded（target mock 写的），不是 other 诱饵
    assert p["data"]["downloaded_count"] == 3


# ============================================================
# 17. 不触发 prepare_scoring_inputs
# ============================================================

def test_fetch_does_not_trigger_prepare(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc.returncode == 0
    tools_called = [c["tool"] for c in mock_calls]
    assert "prepare_scoring_inputs" not in tools_called
    # scoring 目录不应被建
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    assert not scoring_dir.exists()


# ============================================================
# 18. 不触发 score
# ============================================================

def test_fetch_does_not_trigger_score(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc.returncode == 0
    tools_called = [c["tool"] for c in mock_calls]
    assert "score_resumes" not in tools_called
    assert "collect_llm_scores" not in tools_called
    # _llm_scores.json 不应被写
    llm_scores = tmp_path / eid / "runs" / target / "process" / "_llm_scores.json"
    assert not llm_scores.exists()
    # run.json.steps_done 不应新增 'score'
    run_json = json.loads(
        (tmp_path / eid / "runs" / target / "run.json").read_text(encoding="utf-8"))
    assert "score" not in run_json.get("steps_done", [])


# ============================================================
# 19. 不触发 report 或 greet
# ============================================================

def test_fetch_does_not_trigger_report_or_greet(fetch_mocks):
    tmp_path, eid, job, target, _other, mock_calls = fetch_mocks
    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc.returncode == 0
    tools_called = [c["tool"] for c in mock_calls]
    assert "generate_html_report" not in tools_called
    assert "auto_greet" not in tools_called
    # 不应有 HTML
    html_files = list((tmp_path / eid / "runs" / target).glob("*.html"))
    assert not html_files
    # 不应有 greet_log.json
    greet_log = tmp_path / eid / "runs" / target / "process" / "greet_log.json"
    assert not greet_log.exists()


# ============================================================
# 20. 不创建 spec
# ============================================================

def test_fetch_does_not_create_spec(tmp_path, monkeypatch):
    """验证 fetch_service / adapters / commands 任何地方都没用 spec 文件。

    实现：grep 源码 + 跑一次 fetch 看 tmp_path 下没 spec_*.json 出现。
    """
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_spec"
    job = "spec_job"
    target = "2026-08-03_120000"
    run_dir = tmp_path / eid / "runs" / target
    (run_dir / "process").mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
        "steps_done": ["jd"], "last_step": "jd",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "process" / "job_detail.json").write_text(
        json.dumps({"jobName": job, "encryptJobId": eid}, ensure_ascii=False),
        encoding="utf-8")

    def _fake(tool, args, **kwargs):
        if tool == "recommend_list":
            (run_dir / "process" / "recommend_geek_ids.json").write_text(
                json.dumps([{"encryptGeekId": "g1"}], ensure_ascii=False),
                encoding="utf-8")
        if tool == "recommend_download":
            (run_dir / "process" / "new_resumes.json").write_text("[]", encoding="utf-8")
            (run_dir / "process" / "failed_resumes.json").write_text("[]", encoding="utf-8")
        return FakeLegacyResult(returncode=0)
    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake)

    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=1)
    assert proc.returncode == 0
    # tmp_path 下不应出现 spec_*.json
    specs = list(tmp_path.rglob("spec_*.json"))
    assert not specs, f"fetch 不应创建 spec 文件，但发现：{specs}"


# ============================================================
# 21. 重复执行行为与旧脚本一致
# ============================================================

def test_fetch_repeat_run_consistency(fetch_mocks):
    tmp_path, eid, job, target, _other, _ = fetch_mocks
    proc1 = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc1.returncode == 0
    p1 = json.loads(_decode(proc1.stdout))
    # 第二次：list 覆盖（mock 总是写 5 个）；download 仍能算 3+2
    proc2 = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc2.returncode == 0
    p2 = json.loads(_decode(proc2.stdout))
    # 旧脚本重复跑会覆盖 list；mock 也覆盖所以两次结果一致
    assert p1["data"]["listed_count"] == p2["data"]["listed_count"]
    assert p1["data"]["downloaded_count"] == p2["data"]["downloaded_count"]


# ============================================================
# 22. 新旧产物核心内容等价
# ============================================================

def test_fetch_output_files_match_old(tmp_path, monkeypatch):
    """跑新 CLI 后产物的 JSON 字段（与旧脚本同款）应一致。"""
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_eq"
    job = "eq_job"
    target = "2026-08-03_120000"
    run_dir = tmp_path / eid / "runs" / target
    (run_dir / "process").mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
        "steps_done": ["jd"], "last_step": "jd",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "process" / "job_detail.json").write_text(
        json.dumps({"jobName": job, "encryptJobId": eid}, ensure_ascii=False),
        encoding="utf-8")

    # 模拟旧 list 写一份"完全符合旧 schema"的产物
    old_list = [
        {"encryptGeekId": f"gid_{i:03d}", "name": f"姓名{i}",
         "geekCard": {"encryptJobId": eid, "securityId": f"sec_{i}"}}
        for i in range(3)
    ]
    new_resumes = [{"ok": True, "name": f"姓名{i}",
                    "_meta": {"encrypt_geek_id": f"gid_{i:03d}"}} for i in range(3)]

    def _fake(tool, args, **kwargs):
        if tool == "recommend_list":
            (run_dir / "process" / "recommend_geek_ids.json").write_text(
                json.dumps(old_list, ensure_ascii=False), encoding="utf-8")
        if tool == "recommend_download":
            (run_dir / "process" / "new_resumes.json").write_text(
                json.dumps(new_resumes, ensure_ascii=False), encoding="utf-8")
            (run_dir / "process" / "failed_resumes.json").write_text("[]", encoding="utf-8")
        return FakeLegacyResult(returncode=0)
    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake)

    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=3)
    p = json.loads(_decode(proc.stdout))
    # data.report paths 应与实际文件路径一致
    assert p["data"]["candidate_list_file"].endswith("recommend_geek_ids.json")
    assert p["data"]["new_resumes_file"].endswith("new_resumes.json")
    assert p["data"]["failed_resumes_file"].endswith("failed_resumes.json")
    # 读出来验证内容
    assert json.loads(open(p["data"]["candidate_list_file"], encoding="utf-8").read()) == old_list
    assert json.loads(open(p["data"]["new_resumes_file"], encoding="utf-8").read()) == new_resumes


# ============================================================
# 23. list 成功、download 失败时保留 list 产物
# ============================================================

def test_fetch_list_success_download_failure_preserves_list(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_lsdf"
    job = "lsdf_job"
    target = "2026-08-03_120000"
    run_dir = tmp_path / eid / "runs" / target
    (run_dir / "process").mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
        "steps_done": ["jd"], "last_step": "jd",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (run_dir / "process" / "job_detail.json").write_text(
        json.dumps({"jobName": job, "encryptJobId": eid}, ensure_ascii=False),
        encoding="utf-8")

    list_path = run_dir / "process" / "recommend_geek_ids.json"
    def _fake(tool, args, **kwargs):
        if tool == "recommend_list":
            list_path.write_text(json.dumps([{"encryptGeekId": "g1"}], ensure_ascii=False),
                                 encoding="utf-8")
            return FakeLegacyResult(returncode=0)
        if tool == "recommend_download":
            return FakeLegacyResult(returncode=99)  # download 失败
        return FakeLegacyResult(returncode=0)
    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake)

    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    # download 失败 → 透传 rc 99
    assert proc.returncode == 99
    # 关键：list 产物保留
    assert list_path.is_file(), "list 成功产物不应被 download 失败回滚"
    # 内容应保留
    list_data = json.loads(list_path.read_text(encoding="utf-8"))
    assert list_data == [{"encryptGeekId": "g1"}]


# ============================================================
# 24. run.json 和 state 文件副作用与旧实现一致
# ============================================================

def test_fetch_run_json_and_state_side_effects(fetch_mocks):
    tmp_path, eid, job, target, _other, _ = fetch_mocks
    run_json_path = tmp_path / eid / "runs" / target / "run.json"
    before = json.loads(run_json_path.read_text(encoding="utf-8"))

    proc = _run_cli_inproc(job_name=job, encrypt_job_id=eid, run_id=target, count=5)
    assert proc.returncode == 0
    after = json.loads(run_json_path.read_text(encoding="utf-8"))

    # fetch 不写 run.json（list / download 也不写；mark_done 是 download 内部，mock 不调）
    # 关键：fetch 也不动 run.json.confirmed / steps_done
    assert after.get("confirmed") == before.get("confirmed") is True
    assert after.get("steps_done") == before.get("steps_done")
    # 验证 download 调过但没写 run.json（mock fake 不调 mark_done）
    assert "download" not in after.get("steps_done", [])
