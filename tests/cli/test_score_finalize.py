# -*- coding: utf-8 -*-
"""boss-hr score v2 finalize（全部完成 → scoring_complete）测试。

v2 协议下：所有 candidate output 合法 → finalize → scoring_complete。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest


_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent.parent
_CLI = _TOOLKIT_ROOT / "boss_hr" / "cli.py"
_SHARED = _TOOLKIT_ROOT / "shared"


def _good_scorecard(eid: str, jd: Dict[str, Any]) -> Dict[str, Any]:
    from boss_hr.scoring.scorecard import source_jd_hash
    from boss_hr.scoring.policy import load_policy, policy_hash as ph
    policy = load_policy()
    return {
        "schema_version": "2.0",
        "policy_version": policy["policy_version"],
        "policy_hash": ph(policy),
        "source_jd_hash": source_jd_hash(jd),
        "job_name": jd.get("jobName", "finalize_test_job"),
        "common_requirements": [
            {"id": "common_001", "description": "本科",
             "importance": "must", "source_evidence": "JD"},
        ],
        "alternative_tracks": [
            {"id": "braking", "name": "线控制动",
             "requirements": [
                 {"id": "braking_001", "description": "线控制动本体",
                  "importance": "must", "source_evidence": "JD"},
             ]},
            {"id": "steering", "name": "线控转向",
             "requirements": [
                 {"id": "steering_001", "description": "线控转向本体",
                  "importance": "must", "source_evidence": "JD"},
             ]},
        ],
        "education_requirements": {"minimum_degree": "本科及以上",
                                    "source_evidence": "JD"},
        "generated_at": "2026-08-05 12:00:00",
    }


def _install_mock_outputs(scoring_dir: str, manifest: Dict[str, Any],
                          scorecard: Dict[str, Any], eid: str) -> None:
    out_dir = os.path.join(scoring_dir, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    for c in manifest.get("candidates", []):
        gid = c["geek_id"]
        common_ass = [
            {"requirement_id": r["id"], "status": "met",
             "evidence": ["mock"], "reason": "mock"}
            for r in scorecard.get("common_requirements", [])
        ]
        track_ass = [
            {"track_id": t["id"], "requirement_id": r["id"],
             "status": "met", "evidence": ["mock"], "reason": "mock"}
            for t in scorecard.get("alternative_tracks", [])
            for r in t.get("requirements", [])
        ]
        payload = {
            "schema_version": "2.0",
            "policy_version": scorecard["policy_version"],
            "policy_hash": scorecard["policy_hash"],
            "job_scorecard_hash": scorecard["source_jd_hash"],
            "name": c["name"], "geek_id": gid, "job_id": eid,
            "common_assessments": common_ass,
            "track_assessments": track_ass,
            "track_results": {
                t["id"]: {"score": 90.0, "must_not_met": [],
                          "core_not_met": [], "core_partial": [],
                          "unverified_significant": []}
                for t in scorecard.get("alternative_tracks", [])
            },
            "best_track": scorecard.get("alternative_tracks", [{}])[0].get("id", "general"),
            "best_track_reason": "mock",
            "job_match_score": 90.0,
            "education_score": 70.0, "education_tier": "一本公办",
            "education_reason": "mock", "education_review_required": False,
            "education_hard_pass": True, "education_hard_reason": "",
            "final_score": 87.0, "decision": "B",
            "decision_label": "部分技能仍需确认，但适合进入面试",
            "decision_reasons": ["mock"], "highlights": [], "concerns": [],
            "verification_questions": ["mock"], "contact_recommended": True,
            "must_not_met": [], "core_not_met": [],
            "core_partial": [], "unverified_significant": [],
        }
        with open(os.path.join(out_dir, f"candidate_{gid}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_TOOLKIT_ROOT),
           "BOSS_HR_LLM_BASE_URL": "http://mock-llm.test/v1",
           "BOSS_HR_LLM_API_KEY": "mock-key",
           "BOSS_HR_LLM_MODEL": "mock-model",
           "BOSS_HR_LLM_TEST_HOOK": "tests.cli._mock_llm_hook",
           "BOSS_HR_LLM_TEST_REQ_IDS": "common_001,braking_001,steering_001",
           "BOSS_HR_LLM_TEST_SCENARIO": "eval",
           }
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), *args],
        capture_output=True, env=env, cwd=str(_TOOLKIT_ROOT), timeout=60,
    )


def _decode(b) -> str:
    if b is None: return ""
    if isinstance(b, bytes): return b.decode("utf-8", errors="replace")
    return b


def _make_run(root: Path, eid: str, rid: str, job_name: str,
              *, confirmed: bool = True,
              new_resumes: list | None = None,
              body_text: str = "要求线控制动或线控转向；本科及以上") -> Path:
    run_dir = root / eid / "runs" / rid
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "started_at": "2026-08-03 12:00:00",
        "confirmed": confirmed,
        "user_confirmed_at": "2026-08-03 12:01:00" if confirmed else None,
        "steps_done": ["jd", "download"], "last_step": "download",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (process_dir / "job_detail.json").write_text(json.dumps({
        "jobName": job_name, "encryptJobId": eid,
        "bodyText": body_text, "formValues": [],
    }, ensure_ascii=False), encoding="utf-8")
    if new_resumes is not None:
        (process_dir / "new_resumes.json").write_text(
            json.dumps(new_resumes, ensure_ascii=False), encoding="utf-8")
    return run_dir


def _make_resume(name: str, geek_id: str, *, school: str = "辽宁工业大学",
                 degree: str = "本科", eid: str = "test_eid_finalize") -> dict:
    return {
        "name": name, "degree": degree, "work_years": "3 年",
        "work_experience": [], "project_experience": [],
        "education": [{"school": school, "major": "车辆工程", "degree": degree, "start": "", "end": ""}],
        "_meta": {"encrypt_geek_id": geek_id, "encrypt_job_id": eid},
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_finalize"
    job_name = "finalize_test_job"
    target = "2026-08-03_120000"
    resumes = [
        _make_resume("张三", "gid_zhangsan_001", eid=eid),
        _make_resume("李四", "gid_lisi_002", eid=eid),
        _make_resume("王五", "gid_wangwu_003", eid=eid),
    ]
    run_dir = _make_run(tmp_path, eid, target, job_name,
                        confirmed=True, new_resumes=resumes)
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    jd = json.loads((run_dir / "process" / "job_detail.json").read_text(encoding="utf-8"))
    sc = _good_scorecard(eid, jd)
    os.makedirs(scoring_dir, exist_ok=True)
    (scoring_dir / "job_scorecard.json").write_text(
        json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path, eid, job_name, target, sc


def _prepare_with_outputs(tmp_path, eid, target, job_name, sc):
    """建 manifest + 预填所有 output。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(_SHARED)}
    proc = subprocess.run(
        [sys.executable, "-X", "utf8",
         str(_TOOLKIT_ROOT / "resume-screener" / "scripts" / "prepare_scoring_inputs.py"),
         "--job-name", job_name, "--encrypt-job-id", eid, "--run-id", target],
        capture_output=True, env=env, cwd=str(_TOOLKIT_ROOT), timeout=60,
    )
    assert proc.returncode == 0, f"prepare 失败：{proc.stderr.decode('utf-8', errors='replace')}"
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    manifest = json.loads((scoring_dir / "manifest.json").read_text(encoding="utf-8"))
    _install_mock_outputs(str(scoring_dir), manifest, sc, eid)
    return manifest


# ============================================================
# 1. finalize → scoring_complete
# ============================================================

def test_finalize_completes_after_all_scored(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    manifest = _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "scoring_complete"
    assert p["next_action"] == "report"
    assert p["data"]["scored"] == len(manifest["candidates"])
    assert "screening_results_file" in p["data"]


# ============================================================
# 2. finalize 写 v2 screening_results.json
# ============================================================

def test_finalize_writes_v2_screening_results(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    manifest = _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0

    screening_path = tmp_path / eid / "runs" / target / "process" / "screening_results.json"
    assert screening_path.is_file()
    data = json.loads(screening_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "2.0"
    assert data["policy_version"] == "abc-education-v1"
    assert data["run_id"] == target
    assert data["summary"]["total"] == len(manifest["candidates"])
    assert data["summary"]["A"] + data["summary"]["B"] + data["summary"]["C"] == len(manifest["candidates"])


# ============================================================
# 3. v2 排序：A→B→C
# ============================================================

def test_finalize_orders_a_b_c_desc(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0

    screening = json.loads(
        (tmp_path / eid / "runs" / target / "process" / "screening_results.json").read_text(encoding="utf-8"))
    order = [c["decision"] for c in screening["candidates"]]
    rank = {"A": 0, "B": 1, "C": 2}
    ranks = [rank.get(d, 99) for d in order]
    assert ranks == sorted(ranks), f"排序应 A→B→C，实际 {order}"


# ============================================================
# 4. 幂等：screening_results.json 已存在
# ============================================================

def test_finalize_idempotent_when_screening_exists(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    proc1 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    p1 = json.loads(_decode(proc1.stdout))
    assert p1["status"] == "scoring_complete"

    proc2 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    p2 = json.loads(_decode(proc2.stdout))
    assert p2["status"] == "scoring_complete"
    assert p2["data"]["scored"] == p1["data"]["scored"]


# ============================================================
# 5. 不触发 report
# ============================================================

def test_finalize_does_not_trigger_report(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    run_dir = tmp_path / eid / "runs" / target
    html_files = list(run_dir.glob("*.html"))
    assert not html_files


# ============================================================
# 6. 非法 v2 output → 视作未评分 → 重评
# ============================================================

def test_finalize_rejects_invalid_existing_output(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    manifest = _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    first_gid = manifest["candidates"][0]["geek_id"]
    # 把第一位 output 写成不合法
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    out = scoring_dir / "outputs" / f"candidate_{first_gid}.json"
    out.write_text(json.dumps({"completely": "wrong"}, ensure_ascii=False),
                   encoding="utf-8")

    # 第一次 score：把非法 output 视作未评分 → 重评该位 → candidate_scored
    proc1 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    p1 = json.loads(_decode(proc1.stdout))
    assert p1["status"] == "candidate_scored"
    assert p1["data"]["candidate_id"] == first_gid

    # 第二次 score：全部合法 → finalize
    proc2 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    p2 = json.loads(_decode(proc2.stdout))
    assert p2["status"] == "scoring_complete"
    assert p2["data"]["scored"] == len(manifest["candidates"])


# ============================================================
# 7. 合法 v2 output 不会再写（幂等跳过）
# ============================================================

def test_idempotent_skip_legal_output(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    manifest = _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    first_gid = manifest["candidates"][0]["geek_id"]
    out = scoring_dir / "outputs" / f"candidate_{first_gid}.json"
    payload0 = json.loads(out.read_text(encoding="utf-8"))
    final0 = payload0["final_score"]
    decision0 = payload0["decision"]

    # 第一次 score
    proc1 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    p1 = json.loads(_decode(proc1.stdout))
    assert p1["status"] == "scoring_complete"
    # 原 output 没被改
    payload_after = json.loads(out.read_text(encoding="utf-8"))
    assert payload_after["decision"] == decision0
    assert payload_after["final_score"] == final0


# ============================================================
# 8. finalize 不调旧 collect / score_resumes 子进程
# ============================================================

def test_finalize_does_not_invoke_legacy_subprocess(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "scoring_complete"
    llm_scores = tmp_path / eid / "runs" / target / "process" / "_llm_scores.json"
    assert not llm_scores.exists()


# ============================================================
# 9. 完整流程：score → scoring_complete → report
# ============================================================

def test_full_flow_score_to_report(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    manifest = _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "scoring_complete"
    assert p["next_action"] == "report"
