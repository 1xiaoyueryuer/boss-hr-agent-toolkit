# -*- coding: utf-8 -*-
"""boss-hr score v2 完整流程 + 损坏输出测试。

v2 协议：
  - 全部 output 合法 → finalize → scoring_complete
  - 任意 output 不合法 → 视作未评分 → finalize 跳过它
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

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
        "job_name": jd.get("jobName", "full_flow_job"),
        "common_requirements": [
            {"id": "common_001", "description": "本科",
             "importance": "must", "source_evidence": "JD"},
        ],
        "alternative_tracks": [
            {"id": "general", "name": "通用",
             "requirements": [
                 {"id": "general_001", "description": "通用要求",
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
            "best_track": "general", "best_track_reason": "mock",
            "job_match_score": 90.0, "education_score": 70.0,
            "education_tier": "一本公办", "education_reason": "mock",
            "education_review_required": False,
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
           "BOSS_HR_LLM_TEST_REQ_IDS": "common_001,general_001",
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
                 degree: str = "本科", eid: str = "test_eid_full_flow") -> dict:
    return {
        "name": name, "degree": degree, "work_years": "3 年",
        "work_experience": [], "project_experience": [],
        "education": [{"school": school, "major": "车辆工程", "degree": degree, "start": "", "end": ""}],
        "_meta": {"encrypt_geek_id": geek_id, "encrypt_job_id": eid},
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_full_flow"
    job_name = "full_flow_job"
    target = "2026-08-03_120000"
    resumes = [_make_resume(f"姓名{i}", f"gid_full_{i:03d}", eid=eid) for i in range(3)]
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
# 1. 全部 output 存在 → scoring_complete
# ============================================================

def test_full_flow_last_candidate_to_scoring_complete(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "scoring_complete"


# ============================================================
# 2. 损坏 JSON output → finalize 跳过它 → 走重评
# ============================================================

def test_corrupt_json_output_skipped_in_finalize(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    manifest = _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    first_gid = manifest["candidates"][0]["geek_id"]
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    out = scoring_dir / "outputs" / f"candidate_{first_gid}.json"
    out.write_text("{ broken json {", encoding="utf-8")

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    # 损坏 → 重评同一位
    assert p["status"] == "candidate_scored"
    assert p["data"]["candidate_id"] == first_gid


# ============================================================
# 3. 空 output → 视作未评分
# ============================================================

def test_empty_output_treated_as_not_scored(workspace):
    tmp_path, eid, job_name, target, sc = workspace
    manifest = _prepare_with_outputs(tmp_path, eid, target, job_name, sc)
    first_gid = manifest["candidates"][0]["geek_id"]
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    out = scoring_dir / "outputs" / f"candidate_{first_gid}.json"
    out.write_text("", encoding="utf-8")

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "candidate_scored"
    assert p["data"]["candidate_id"] == first_gid


# ============================================================
# 4. 双 finalize 幂等
# ============================================================

def test_double_finalize_idempotent(workspace):
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
