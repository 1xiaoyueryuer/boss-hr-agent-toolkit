# -*- coding: utf-8 -*-
"""boss-hr greet 命令测试（tests/cli/）

策略：mock `boss_hr.adapters.legacy_runner.run_legacy_cli` 让 auto_greet
子脚本被替换为写真实 greet_log.json 的 fake；通过 inproc 调
boss_hr.commands.greet.run() 让 monkeypatch 生效。

⚠️ 不连真实 BOSS / 不启 patchright / 不写用户真实桌面目录
（tests/conftest.py 的 _isolate_output_root 已把 OUTPUT_ROOT 指到 tmp_path）。

用例覆盖：
  1-6.   正常招呼 + schema + 计数 + next_action
  7-9.   参数透传（only-names / threshold+max / dry-run）
  10-12. 缺必填参数（run-id / job-name / encrypt-job-id）
  13-14. run 不存在 → 23；岗位不匹配 → 24
  15-17. 无高分候选人 / greet_log 缺失 / greet_log 损坏
  18-19. 子进程失败透传 rc
  20-24. 不调其他命令 / 不自动 confirm / 不创建新 run / 不扫最新 run
  25-26. 新旧产物核心内容等价 + 走子进程不直接 import
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from collections import namedtuple
from pathlib import Path
from typing import Optional

import pytest


_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent.parent
_CLI = _TOOLKIT_ROOT / "boss_hr" / "cli.py"
_SHARED = _TOOLKIT_ROOT / "shared"


# ============================================================
# helpers
# ============================================================

class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _decode(b) -> str:
    if b is None:
        return ""
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return b


def _parse_args(args: list) -> dict:
    """从 auto_greet 的 args 提取参数。"""
    out: dict = {"flags": []}
    i = 0
    args = list(args)
    while i < len(args):
        a = args[i]
        if a in ("--job-name", "--encrypt-job-id", "--run-id",
                 "--only-names", "--threshold", "--max") and i + 1 < len(args):
            out[a.lstrip("-").replace("-", "_")] = args[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            out["flags"].append(a)
        i += 1
    return out


def _make_run(workspace: Path, eid: str, rid: str, jn: str,
              *, candidates: Optional[list] = None,
              confirmed: bool = True) -> Path:
    """建一个真实 run 目录（job_detail + run.json + screening_results）。"""
    run_dir = workspace / eid / "runs" / rid
    process = run_dir / "process"
    process.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "started_at": "2026-08-03 12:00:00",
        "confirmed": confirmed, "user_confirmed_at": "2026-08-03 12:01:00",
        "steps_done": ["jd", "download", "score", "report"],
        "last_step": "report", "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (process / "job_detail.json").write_text(json.dumps({
        "jobName": jn, "encryptJobId": eid,
        "_meta": {"run_id": rid, "saved_at": "2026-08-03 12:00:00"},
    }, ensure_ascii=False), encoding="utf-8")
    if candidates is None:
        candidates = [
            {"name": "高分甲", "total": 85.0, "tier": "推荐"},
            {"name": "高分乙", "total": 75.0, "tier": "推荐"},
            {"name": "低分丙", "total": 40.0, "tier": "不推荐"},
        ]
    (process / "screening_results.json").write_text(
        json.dumps({"candidates": candidates}, ensure_ascii=False),
        encoding="utf-8")
    return run_dir


def _write_greet_log(workspace: Path, eid: str, rid: str, jn: str,
                     *, greeted=3, unverified=0, not_found=0) -> Path:
    """写真实 greet_log.json（模拟 auto_greet 跑完）。

    v1.1.3：默认场景改为 complete（greeted=3 not_found=0）以匹配新
    partial_success 语义；旧测试期望 next_action=done 的场景
    （greeted=2 not_found=1）已迁到 partial_success 分支独立测试。
    """
    process = workspace / eid / "runs" / rid / "process"
    process.mkdir(parents=True, exist_ok=True)
    results = []
    for i in range(greeted):
        results.append({"name": f"高分{i}", "score": 85.0, "tier": "推荐",
                        "found": True, "clicked": True, "verified": True,
                        "dialog_closed": True, "status": "greeted"})
    for i in range(unverified):
        results.append({"name": f"未验证{i}", "found": True, "clicked": True,
                        "verified": False, "status": "clicked_unverified"})
    for i in range(not_found):
        results.append({"name": f"未找到{i}", "found": False,
                        "status": "not_found", "reason": "不在 list"})
    path = process / "greet_log.json"
    path.write_text(json.dumps({
        "job": jn, "run_id": rid, "score_threshold": 70,
        "started_at": "2026-08-03 12:10:00",
        "updated_at": "2026-08-03 12:12:00",
        "mode": "scan_and_greet_reverse", "positions_count": 12,
        "summary": {
            "greeted": greeted, "clicked_unverified": unverified,
            "not_found": not_found, "dry_run": 0, "scanned": 0,
            "total": len(results),
        },
        "results": results,
    }, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def greet_mocks(tmp_path, monkeypatch):
    """mock auto_greet 子进程：写真实 greet_log.json。

    覆盖 tests/cli/conftest.py 里给 fetch 用的 autouse mock。
    返回 (tmp_path, calls)。
    """
    calls: list[dict] = []

    def _fake(tool, args, *, timeout=60, **kwargs):
        calls.append({"tool": tool, "args": list(args), "timeout": timeout})
        if tool != "auto_greet":
            return _FakeProc(99, "", "")
        p = _parse_args(args)
        eid, rid, jn = p.get("encrypt_job_id"), p.get("run_id"), p.get("job_name")
        if not eid or not rid:
            return _FakeProc(2, "", "missing args")
        # dry-run 不写真实招呼结果
        if "--dry-run" in p["flags"]:
            _write_greet_log(tmp_path, eid, rid, jn, greeted=0,
                             unverified=0, not_found=0)
            return _FakeProc(0, "[DRY-RUN]\n", "")
        _write_greet_log(tmp_path, eid, rid, jn)
        return _FakeProc(0, "=== 完成：greeted=2 unverified=0 not_found=1 ===\n", "")

    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake)
    return tmp_path, calls


# ============================================================
# inproc runner
# ============================================================

def _run_inproc(*, jn=None, eid=None, rid=None, extra=None):
    import argparse
    from boss_hr.commands import greet as greet_cmd
    from boss_hr.cli import build_parser
    p = build_parser()
    argv = ["greet"]
    if jn is not None:
        argv += ["--job-name", jn]
    if eid is not None:
        argv += ["--encrypt-job-id", eid]
    if rid is not None:
        argv += ["--run-id", rid]
    if extra:
        argv += list(extra)
    ns = p.parse_args(argv)
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction) and ns.command in action.choices:
            ns._parser = action.choices[ns.command]
            break
    result = greet_cmd.run(ns)
    out = json.dumps(result.to_dict("greet"), ensure_ascii=False) + "\n"
    Proc = namedtuple("FakeProc", ["returncode", "stdout", "stderr"])
    return Proc(returncode=int(result.exit_code), stdout=out.encode("utf-8"), stderr=b"")


# ============================================================
# 1-6. 正常招呼
# ============================================================

def test_greet_success_schema(greet_mocks):
    tmp_path, _ = greet_mocks
    eid, rid, jn = "test_eid_g1", "2026-08-03_120000", "g1_job"
    _make_run(tmp_path, eid, rid, jn)
    proc = _run_inproc(jn=jn, eid=eid, rid=rid)
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is True
    assert p["command"] == "greet"
    assert p["status"] == "greet_complete"
    assert p["run_id"] == rid
    assert p["encrypt_job_id"] == eid
    assert p["job_name"] == jn


def test_greet_next_action_done(greet_mocks):
    tmp_path, _ = greet_mocks
    eid, rid, jn = "test_eid_g2", "2026-08-03_120000", "g2_job"
    _make_run(tmp_path, eid, rid, jn)
    p = json.loads(_decode(_run_inproc(jn=jn, eid=eid, rid=rid).stdout))
    assert p["next_action"] == "done"


def test_greet_counts_from_greet_log(greet_mocks):
    tmp_path, _ = greet_mocks
    eid, rid, jn = "test_eid_g3", "2026-08-03_120000", "g3_job"
    _make_run(tmp_path, eid, rid, jn)
    p = json.loads(_decode(_run_inproc(jn=jn, eid=eid, rid=rid).stdout))
    d = p["data"]
    # v1.1.3 fixture 默认 complete 场景（greeted=3 not_found=0）
    assert d["greeted"] == 3
    assert d["clicked_unverified"] == 0
    assert d["not_found"] == 0
    assert d["total"] == 3
    assert d["candidates_targeted"] == 3
    assert d["no_candidates"] is False
    assert d["partial_success_warnings"] is False


def test_greet_log_file_is_absolute_and_exists(greet_mocks):
    tmp_path, _ = greet_mocks
    eid, rid, jn = "test_eid_g4", "2026-08-03_120000", "g4_job"
    _make_run(tmp_path, eid, rid, jn)
    p = json.loads(_decode(_run_inproc(jn=jn, eid=eid, rid=rid).stdout))
    gl = p["data"]["greet_log_file"]
    assert os.path.isabs(gl)
    assert Path(gl).is_file()
    assert gl.replace("\\", "/").endswith(f"{rid}/process/greet_log.json")


def test_greet_calls_auto_greet_tool(greet_mocks):
    tmp_path, calls = greet_mocks
    eid, rid, jn = "test_eid_g5", "2026-08-03_120000", "g5_job"
    _make_run(tmp_path, eid, rid, jn)
    _run_inproc(jn=jn, eid=eid, rid=rid)
    assert [c["tool"] for c in calls] == ["auto_greet"]


def test_greet_passes_run_id_to_subprocess(greet_mocks):
    """run_id 必须原样透传给子脚本（数据边界）。"""
    tmp_path, calls = greet_mocks
    eid, rid, jn = "test_eid_g6", "2026-08-03_120000", "g6_job"
    _make_run(tmp_path, eid, rid, jn)
    _run_inproc(jn=jn, eid=eid, rid=rid)
    p = _parse_args(calls[0]["args"])
    assert p["run_id"] == rid
    assert p["encrypt_job_id"] == eid
    assert p["job_name"] == jn


# ============================================================
# 7-9. 参数透传
# ============================================================

def test_greet_only_names_passed_through(greet_mocks):
    tmp_path, calls = greet_mocks
    eid, rid, jn = "test_eid_g7", "2026-08-03_120000", "g7_job"
    _make_run(tmp_path, eid, rid, jn)
    proc = _run_inproc(jn=jn, eid=eid, rid=rid,
                       extra=["--only-names", "张三,李四"])
    assert proc.returncode == 0
    assert _parse_args(calls[0]["args"])["only_names"] == "张三,李四"


def test_greet_threshold_and_max_passed_through(greet_mocks):
    tmp_path, calls = greet_mocks
    eid, rid, jn = "test_eid_g8", "2026-08-03_120000", "g8_job"
    _make_run(tmp_path, eid, rid, jn)
    _run_inproc(jn=jn, eid=eid, rid=rid,
                extra=["--threshold", "80", "--max", "3"])
    p = _parse_args(calls[0]["args"])
    assert float(p["threshold"]) == 80.0
    assert int(p["max"]) == 3


def test_greet_dry_run_passed_through(greet_mocks):
    tmp_path, calls = greet_mocks
    eid, rid, jn = "test_eid_g9", "2026-08-03_120000", "g9_job"
    _make_run(tmp_path, eid, rid, jn)
    p = json.loads(_decode(_run_inproc(jn=jn, eid=eid, rid=rid,
                                       extra=["--dry-run"]).stdout))
    assert "--dry-run" in _parse_args(calls[0]["args"])["flags"]
    assert p["data"]["dry_run"] is True
    assert p["data"]["greeted"] == 0


def test_greet_defaults_threshold_70_max_10(greet_mocks):
    """不传 --threshold / --max 时用默认值 70 / 10。"""
    tmp_path, calls = greet_mocks
    eid, rid, jn = "test_eid_g9b", "2026-08-03_120000", "g9b_job"
    _make_run(tmp_path, eid, rid, jn)
    _run_inproc(jn=jn, eid=eid, rid=rid)
    p = _parse_args(calls[0]["args"])
    assert float(p["threshold"]) == 70.0
    assert int(p["max"]) == 10
    assert "--dry-run" not in p["flags"]


# ============================================================
# 10-12. 缺必填参数
# ============================================================

def test_greet_missing_run_id_argparse(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), "greet",
         "--job-name", "j", "--encrypt-job-id", "test_eid"],
        capture_output=True, env={**os.environ, "PYTHONUTF8": "1",
                                  "PYTHONIOENCODING": "utf-8",
                                  "PYTHONPATH": str(_SHARED),
                                  "BOSS_HR_OUTPUT_DIR": str(tmp_path)},
        cwd=str(_TOOLKIT_ROOT), timeout=20,
    )
    assert proc.returncode == 2
    assert "--run-id" in _decode(proc.stderr)
    assert _decode(proc.stdout).strip() == ""


def test_greet_missing_job_name_argparse(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), "greet",
         "--encrypt-job-id", "test_eid", "--run-id", "2026-08-03_120000"],
        capture_output=True, env={**os.environ, "PYTHONUTF8": "1",
                                  "PYTHONIOENCODING": "utf-8",
                                  "PYTHONPATH": str(_SHARED),
                                  "BOSS_HR_OUTPUT_DIR": str(tmp_path)},
        cwd=str(_TOOLKIT_ROOT), timeout=20,
    )
    assert proc.returncode == 2


def test_greet_missing_encrypt_job_id_argparse(tmp_path):
    """缺 --encrypt-job-id 且无 env → argparse rc=2（与 report/fetch 一致）。"""
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_SHARED), "BOSS_HR_OUTPUT_DIR": str(tmp_path)}
    env.pop("BOSS_HR_ENCRYPT_JOB_ID", None)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), "greet",
         "--job-name", "j", "--run-id", "2026-08-03_120000"],
        capture_output=True, env=env, cwd=str(_TOOLKIT_ROOT), timeout=20,
    )
    assert proc.returncode == 2
    assert "encrypt-job-id" in _decode(proc.stderr)


# ============================================================
# 13-14. run 不存在 / 岗位不匹配
# ============================================================

def test_greet_run_not_found_rc23(greet_mocks):
    """run 不存在 → 23（预校验拦截，不调子进程）。"""
    tmp_path, calls = greet_mocks
    eid, jn = "test_eid_g10", "g10_job"
    _make_run(tmp_path, eid, "2026-08-03_120000", jn)
    proc = _run_inproc(jn=jn, eid=eid, rid="9999-99-99_999999")
    assert proc.returncode == 23
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert p["error"]["code"] == "RUN_NOT_FOUND"
    assert calls == [], "run 不存在时不应调用子进程"


def test_greet_job_mismatch_rc24(greet_mocks):
    """run 的 encryptJobId 与当前岗位不符 → 24。"""
    tmp_path, calls = greet_mocks
    rid, jn = "2026-08-03_120000", "g11_job"
    # run 目录建在 eid_a 下，但 job_detail 里写 eid_other
    run_dir = _make_run(tmp_path, "test_eid_g11", rid, jn)
    (run_dir / "process" / "job_detail.json").write_text(json.dumps({
        "jobName": jn, "encryptJobId": "test_eid_other",
        "_meta": {"run_id": rid},
    }, ensure_ascii=False), encoding="utf-8")
    proc = _run_inproc(jn=jn, eid="test_eid_g11", rid=rid)
    assert proc.returncode == 24
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert p["error"]["code"] == "JOB_MISMATCH"
    assert calls == []


# ============================================================
# 15-17. 无候选人 / greet_log 缺失或损坏
# ============================================================

def test_greet_no_candidates_returns_ok_zero(monkeypatch, tmp_path):
    """无高分候选人：旧脚本 rc=0 且不写 greet_log → no_candidates=true。"""
    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli",
                        lambda *a, **kw: _FakeProc(0, "没有高分候选人，结束\n", ""))
    eid, rid, jn = "test_eid_g12", "2026-08-03_120000", "g12_job"
    _make_run(tmp_path, eid, rid, jn,
              candidates=[{"name": "低分", "total": 30.0, "tier": "不推荐"}])
    proc = _run_inproc(jn=jn, eid=eid, rid=rid)
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is True
    # v1.1.3 fix: no_candidates 路径返回独立 status
    assert p["status"] == "no_candidates"
    assert p["next_action"] == "done"
    d = p["data"]
    assert d["no_candidates"] is True
    assert d["greeted"] == 0
    assert d["total"] == 0
    assert d["greet_log_file"] is None


def test_greet_tolerates_pruned_run_dir(monkeypatch, tmp_path):
    """旧脚本 atexit prune_if_empty 删掉 run 目录后仍不崩。"""
    import shutil
    eid, rid, jn = "test_eid_g13", "2026-08-03_120000", "g13_job"
    run_dir = _make_run(tmp_path, eid, rid, jn)

    def _fake(tool, args, *, timeout=60, **kwargs):
        shutil.rmtree(run_dir)  # 模拟 prune_if_empty 删目录
        return _FakeProc(0, "没有高分候选人，结束\n", "")

    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake)
    proc = _run_inproc(jn=jn, eid=eid, rid=rid)
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is True
    assert p["data"]["no_candidates"] is True


def test_greet_corrupt_greet_log_treated_as_no_candidates(monkeypatch, tmp_path):
    """greet_log.json 损坏 → 不抛异常，按无产物处理。"""
    eid, rid, jn = "test_eid_g14", "2026-08-03_120000", "g14_job"
    _make_run(tmp_path, eid, rid, jn)

    def _fake(tool, args, *, timeout=60, **kwargs):
        p = tmp_path / eid / "runs" / rid / "process" / "greet_log.json"
        p.write_text("{ 这不是合法 JSON", encoding="utf-8")
        return _FakeProc(0, "", "")

    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", _fake)
    proc = _run_inproc(jn=jn, eid=eid, rid=rid)
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is True
    assert p["data"]["no_candidates"] is True


# ============================================================
# 18-19. 子进程失败透传
# ============================================================

def test_greet_passes_through_subprocess_rc(monkeypatch, tmp_path):
    """子脚本 rc=42（不在 ExitCode enum 里）→ 原样透传，不抛 ValueError。"""
    eid, rid, jn = "test_eid_g15", "2026-08-03_120000", "g15_job"
    _make_run(tmp_path, eid, rid, jn)
    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli",
                        lambda *a, **kw: _FakeProc(42, "boom\n", ""))
    proc = _run_inproc(jn=jn, eid=eid, rid=rid)
    assert proc.returncode == 42
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert p["error"]["subprocess_returncode"] == 42


def test_greet_cdp_login_failure_rc1(monkeypatch, tmp_path):
    """CDP 连不上 → 子脚本 rc=1 透传。"""
    eid, rid, jn = "test_eid_g16", "2026-08-03_120000", "g16_job"
    _make_run(tmp_path, eid, rid, jn)
    monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli",
                        lambda *a, **kw: _FakeProc(1, "", "connect ECONNREFUSED 9222\n"))
    proc = _run_inproc(jn=jn, eid=eid, rid=rid)
    assert proc.returncode == 1
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False


# ============================================================
# 20-24. 不调其他命令 / 不越权
# ============================================================

def test_greet_does_not_call_other_tools(greet_mocks):
    tmp_path, calls = greet_mocks
    eid, rid, jn = "test_eid_g17", "2026-08-03_120000", "g17_job"
    _make_run(tmp_path, eid, rid, jn)
    _run_inproc(jn=jn, eid=eid, rid=rid)
    tools = [c["tool"] for c in calls]
    for t in ("boss_jd", "confirm_run", "recommend_list", "recommend_download",
              "prepare_scoring_inputs", "collect_llm_scores", "score_resumes",
              "generate_html_report"):
        assert t not in tools, f"greet 不应调 {t}"


def test_greet_does_not_auto_confirm(greet_mocks):
    """greet 不改 run.json.confirmed。"""
    tmp_path, _ = greet_mocks
    eid, rid, jn = "test_eid_g18", "2026-08-03_120000", "g18_job"
    _make_run(tmp_path, eid, rid, jn, confirmed=False)
    run_json = tmp_path / eid / "runs" / rid / "run.json"
    before = run_json.read_text(encoding="utf-8")
    _run_inproc(jn=jn, eid=eid, rid=rid)
    assert run_json.read_text(encoding="utf-8") == before


def test_greet_does_not_create_new_run(greet_mocks):
    """greet 只在给定 run 内工作，不新建 run 目录。"""
    tmp_path, _ = greet_mocks
    eid, rid, jn = "test_eid_g19", "2026-08-03_120000", "g19_job"
    _make_run(tmp_path, eid, rid, jn)
    runs_dir = tmp_path / eid / "runs"
    before = sorted(p.name for p in runs_dir.iterdir())
    _run_inproc(jn=jn, eid=eid, rid=rid)
    assert sorted(p.name for p in runs_dir.iterdir()) == before


def test_greet_does_not_pick_latest_run(greet_mocks):
    """存在更新的 run 时，greet 仍只用显式传入的 run_id。"""
    tmp_path, calls = greet_mocks
    eid, jn = "test_eid_g20", "g20_job"
    target = "2026-08-03_120000"
    _make_run(tmp_path, eid, target, jn)
    _make_run(tmp_path, eid, "2026-08-03_180000", jn)  # 更新的 run
    p = json.loads(_decode(_run_inproc(jn=jn, eid=eid, rid=target).stdout))
    assert p["run_id"] == target
    assert _parse_args(calls[0]["args"])["run_id"] == target


def test_greet_does_not_read_current_run_json(greet_mocks):
    tmp_path, _ = greet_mocks
    eid, rid, jn = "test_eid_g21", "2026-08-03_120000", "g21_job"
    _make_run(tmp_path, eid, rid, jn)
    state = tmp_path / eid / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "current_run.json").write_text(
        json.dumps({"current_run_id": "9999-99-99_999999"}), encoding="utf-8")
    p = json.loads(_decode(_run_inproc(jn=jn, eid=eid, rid=rid).stdout))
    assert p["run_id"] == rid


# ============================================================
# 25-26. 等价性 + 架构约束
# ============================================================

def test_greet_output_equivalent_old_greet_log(greet_mocks):
    """新 CLI 的 data 计数与旧 greet_log.json 的 summary 完全一致。"""
    tmp_path, _ = greet_mocks
    eid, rid, jn = "test_eid_g22", "2026-08-03_120000", "g22_job"
    _make_run(tmp_path, eid, rid, jn)
    p = json.loads(_decode(_run_inproc(jn=jn, eid=eid, rid=rid).stdout))
    raw = json.loads((tmp_path / eid / "runs" / rid / "process"
                      / "greet_log.json").read_text(encoding="utf-8"))
    s = raw["summary"]
    d = p["data"]
    assert d["greeted"] == s["greeted"]
    assert d["clicked_unverified"] == s["clicked_unverified"]
    assert d["not_found"] == s["not_found"]
    assert d["total"] == s["total"]
    # 旧产物本身未被新 CLI 改写
    assert raw["run_id"] == rid
    assert raw["mode"] == "scan_and_greet_reverse"


def test_greet_service_does_not_import_auto_greet_directly():
    """架构约束：greet_service 必须走 cli_runner 子进程。

    auto_greet() 函数体引用 __main__ 才定义的全局 args，直接 import 调用会
    NameError，因此必须通过脚本 CLI 入口运行。

    用 AST 检查真实 import 语句，而不是裸文本匹配 —— 模块 docstring 里
    正好写了这条禁令的说明文字，文本匹配会误报。
    """
    import ast
    path = _TOOLKIT_ROOT / "boss_hr" / "application" / "greet_service.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
            imported += [f"{node.module or ''}.{a.name}" for a in node.names]

    forbidden = ("auto_greet", "patchright", "patchright.sync_api",
                 "human_interaction")
    for mod in imported:
        for bad in forbidden:
            assert not (mod == bad or mod.startswith(bad + ".")), \
                f"greet_service 不应 import {mod}（必须走子进程）"

    # 也不应出现 CDP / 浏览器调用（排除注释与 docstring 后再看代码）
    code_only = ast.unparse(tree)
    assert "sync_playwright" not in code_only
    assert "connect_over_cdp" not in code_only
    # 必须真的通过 legacy_runner 调子进程
    assert "run_legacy_cli" in code_only


def test_greet_registered_in_cli_commands():
    """greet 必须注册进 COMMANDS 且 --help 可用。"""
    from boss_hr.cli import COMMANDS, build_parser
    assert "greet" in COMMANDS
    p = build_parser()
    ns = p.parse_args(["greet", "--job-name", "j", "--encrypt-job-id", "e",
                       "--run-id", "2026-08-03_120000"])
    assert ns.command == "greet"
    assert ns.threshold == 70.0
    assert ns.max_count == 10
    assert ns.dry_run is False
    assert ns.only_names is None
