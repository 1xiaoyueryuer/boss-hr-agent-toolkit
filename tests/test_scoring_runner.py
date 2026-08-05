# -*- coding: utf-8 -*-
"""boss-hr score v2 runner 单元测试。"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_HERE = Path(__file__).resolve().parent.parent
_TOOLKIT_ROOT = _HERE.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.scoring.runner import (
    run_score_v2, _load_jd, _load_manifest, _candidates,
    _has_legal_output, _build_screening_results_v2,
)
from boss_hr.scoring.scorecard import source_jd_hash
from boss_hr.scoring.policy import load_policy, policy_hash as ph, reset_policy_cache


@pytest.fixture(autouse=True)
def _reset_policy_cache():
    reset_policy_cache()
    yield
    reset_policy_cache()


def _good_scorecard(jd: dict) -> dict:
    p = load_policy()
    return {
        "schema_version": "2.0",
        "policy_version": p["policy_version"],
        "policy_hash": ph(p),
        "source_jd_hash": source_jd_hash(jd),
        "job_name": jd.get("jobName", "x"),
        "common_requirements": [
            {"id": "common_001", "description": "本科", "importance": "must",
             "source_evidence": "JD"}
        ],
        "alternative_tracks": [
            {"id": "braking", "name": "b",
             "requirements": [{"id": "braking_001", "description": "x",
                              "importance": "must", "source_evidence": "x"}]},
            {"id": "steering", "name": "s",
             "requirements": [{"id": "steering_001", "description": "x",
                              "importance": "must", "source_evidence": "x"}]},
        ],
        "education_requirements": {"minimum_degree": "本科及以上",
                                     "source_evidence": "JD"},
        "generated_at": "2026-08-05 12:00:00",
    }


def _good_candidate_output(gid: str, name: str, decision: str = "B",
                            final: float = 80.0, jm: float = 85.0,
                            edu: float = 70.0, eid: str = "eid1") -> dict:
    p = load_policy()
    return {
        "schema_version": "2.0",
        "policy_version": p["policy_version"],
        "policy_hash": ph(p),
        "job_scorecard_hash": "ignored_for_unit_test",
        "name": name,
        "geek_id": gid,
        "job_id": eid,
        "common_assessments": [
            {"requirement_id": "common_001", "status": "met",
             "evidence": ["x"], "reason": "x"}
        ],
        "track_assessments": [
            {"track_id": "braking", "requirement_id": "braking_001",
             "status": "met", "evidence": ["x"], "reason": "x"},
        ],
        "track_results": {
            "braking": {"score": jm, "must_not_met": [],
                          "core_not_met": [], "core_partial": [],
                          "unverified_significant": []}
        },
        "best_track": "braking", "best_track_reason": "x",
        "job_match_score": jm,
        "education_score": edu, "education_tier": "一本公办",
        "education_reason": "x", "education_review_required": False,
        "education_hard_pass": True, "education_hard_reason": "",
        "final_score": final,
        "decision": decision,
        "decision_label": f"label-{decision}",
        "decision_reasons": ["x"],
        "highlights": [], "concerns": [],
        "verification_questions": [], "contact_recommended": decision in ("A", "B"),
        "must_not_met": [], "core_not_met": [],
        "core_partial": [], "unverified_significant": [],
    }


# ============================================================
# run_score_v2 入口守卫
# ============================================================

def test_run_score_v2_missing_eid():
    out = run_score_v2(job_name="x", encrypt_job_id="", run_id="2026-08-05_120000")
    assert out["ok"] is False
    assert "encrypt_job_id" in out["error"]["message"]


def test_run_score_v2_missing_run_id():
    out = run_score_v2(job_name="x", encrypt_job_id="eid1", run_id="")
    assert out["ok"] is False


def test_run_score_v2_run_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    out = run_score_v2(job_name="x", encrypt_job_id="eid_nope",
                       run_id="2026-08-05_120000")
    assert out["ok"] is False
    assert out["error"]["code"] == "RUN_NOT_FOUND"


def test_run_score_v2_awaits_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "eid_test_aw"
    target = "2026-08-05_120000"
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": False,
    }, ensure_ascii=False), encoding="utf-8")
    out = run_score_v2(job_name="x", encrypt_job_id=eid, run_id=target)
    assert out["ok"] is False
    assert out["error"]["code"] == "AWAITING_CONFIRMATION"


def test_run_score_v2_missing_new_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "eid_test_nr"
    target = "2026-08-05_120000"
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
    }, ensure_ascii=False), encoding="utf-8")
    (process_dir / "job_detail.json").write_text(json.dumps({
        "jobName": "x", "encryptJobId": eid, "bodyText": "x", "formValues": []
    }, ensure_ascii=False), encoding="utf-8")
    out = run_score_v2(job_name="x", encrypt_job_id=eid, run_id=target)
    assert out["ok"] is False
    assert "new_resumes" in out["error"]["message"]


def test_run_score_v2_policy_load_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "eid_test_pf"
    target = "2026-08-05_120000"
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
    }, ensure_ascii=False), encoding="utf-8")
    (process_dir / "job_detail.json").write_text(json.dumps({
        "jobName": "x", "encryptJobId": eid, "bodyText": "x", "formValues": []
    }, ensure_ascii=False), encoding="utf-8")
    (process_dir / "new_resumes.json").write_text(
        json.dumps([{"name": "x"}], ensure_ascii=False), encoding="utf-8")
    # monkeypatch load_policy 直接抛错
    from boss_hr.scoring import runner
    def bad_load(path=None):
        raise FileNotFoundError(f"no policy at {path}")
    monkeypatch.setattr(runner, "load_policy", bad_load)
    out = run_score_v2(job_name="x", encrypt_job_id=eid, run_id=target)
    assert out["ok"] is False


# ============================================================
# 全部合法 → finalize
# ============================================================

def test_run_score_v2_finalize_when_all_legal(tmp_path, monkeypatch):
    reset_policy_cache()  # 防御性
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    # 显式 import + 加 sys.path，让 shared modules 走 tmp
    # 关键：JobOutputManager 走 JobRegistry → JOBS_REGISTRY_PATH 在 import 时固化
    # conftest 已 patch output_manager.OUTPUT_ROOT；我们这里直接复用
    # 看看 tmp_path 实际写到哪
    print("DEBUG tmp_path:", tmp_path, file=__import__('sys').stderr)
    print("DEBUG BOSS_HR_OUTPUT_DIR:", os.environ.get("BOSS_HR_OUTPUT_DIR"), file=__import__('sys').stderr)
    print("DEBUG output_manager.OUTPUT_ROOT:", __import__('output_manager', fromlist=['OUTPUT_ROOT']).OUTPUT_ROOT, file=__import__('sys').stderr)
    print("DEBUG job_registry.JOBS_REGISTRY_PATH:", __import__('job_registry', fromlist=['JOBS_REGISTRY_PATH']).JOBS_REGISTRY_PATH, file=__import__('sys').stderr)
    eid = "eid_test_final"
    target = "2026-08-05_120000"
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
    }, ensure_ascii=False), encoding="utf-8")
    jd = {"jobName": "x", "encryptJobId": eid, "bodyText": "本科", "formValues": []}
    (process_dir / "job_detail.json").write_text(json.dumps(jd, ensure_ascii=False),
                                                  encoding="utf-8")
    (process_dir / "new_resumes.json").write_text(json.dumps([
        {"name": f"姓名{i}", "_meta": {"encrypt_geek_id": f"g{i:03d}",
                                         "encrypt_job_id": eid},
         "degree": "本科"}
        for i in range(3)
    ], ensure_ascii=False), encoding="utf-8")
    sc_dir = process_dir / "scoring"
    sc_dir.mkdir(parents=True, exist_ok=True)
    sc = _good_scorecard(jd)
    (sc_dir / "job_scorecard.json").write_text(
        json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 建 manifest
    manifest = {
        "candidates": [
            {"geek_id": f"g{i:03d}", "name": f"姓名{i}",
             "input_path": f"inputs/candidate_g{i:03d}.json",
             "output_path": f"outputs/candidate_g{i:03d}.json",
             "status": "pending"}
            for i in range(3)
        ],
    }
    (sc_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    # 预填 candidate outputs
    out_dir = sc_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions = ["A", "B", "C"]
    for i in range(3):
        c = manifest["candidates"][i]
        p = _good_candidate_output(c["geek_id"], c["name"],
                                     decision=decisions[i], final=80.0 - i,
                                     eid=eid)
        p["job_scorecard_hash"] = sc["source_jd_hash"]
        (out_dir / f"candidate_{c['geek_id']}.json").write_text(
            json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    out = run_score_v2(job_name="x", encrypt_job_id=eid, run_id=target)
    assert out["ok"] is True
    assert out["status"] == "scoring_complete"
    assert out["data"]["scored"] == 3
    assert (process_dir / "screening_results.json").is_file()
    screening = json.loads((process_dir / "screening_results.json").read_text(encoding="utf-8"))
    assert screening["summary"]["total"] == 3
    assert screening["summary"]["A"] == 1
    assert screening["summary"]["B"] == 1
    assert screening["summary"]["C"] == 1
    # 排序：A 优先于 B 优先于 C
    order = [c["decision"] for c in screening["candidates"]]
    assert order == ["A", "B", "C"]


# ============================================================
# 非法的 output 被跳过
# ============================================================

def test_run_score_v2_invalid_output_triggers_rescore(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "eid_test_inv"
    target = "2026-08-05_120000"
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
    }, ensure_ascii=False), encoding="utf-8")
    jd = {"jobName": "x", "encryptJobId": eid, "bodyText": "本科", "formValues": []}
    (process_dir / "job_detail.json").write_text(json.dumps(jd, ensure_ascii=False),
                                                  encoding="utf-8")
    (process_dir / "new_resumes.json").write_text(json.dumps([
        {"name": "姓名0", "_meta": {"encrypt_geek_id": "g000",
                                       "encrypt_job_id": eid},
         "degree": "本科"},
    ], ensure_ascii=False), encoding="utf-8")
    sc_dir = process_dir / "scoring"
    sc_dir.mkdir(parents=True, exist_ok=True)
    sc = _good_scorecard(jd)
    (sc_dir / "job_scorecard.json").write_text(
        json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sc_dir / "manifest.json").write_text(json.dumps({
        "candidates": [{"geek_id": "g000", "name": "姓名0",
                         "input_path": "inputs/candidate_g000.json",
                         "output_path": "outputs/candidate_g000.json",
                         "status": "pending"}],
    }, ensure_ascii=False), encoding="utf-8")
    # 非法 output
    out_dir = sc_dir / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "candidate_g000.json").write_text(json.dumps({"bogus": True}, ensure_ascii=False),
                                                  encoding="utf-8")
    # 净化输入
    in_dir = sc_dir / "inputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    (in_dir / "candidate_g000.json").write_text(json.dumps({
        "name": "姓名0", "geek_id": "g000", "job_id": eid,
        "degree": "本科",
        "education": [{"school": "辽宁工业大学", "major": "x", "degree": "本科"}],
    }, ensure_ascii=False), encoding="utf-8")
    # 但 LLM 配置已预填（通过 env var），不让它真调
    from boss_hr.scoring import llm_client
    from boss_hr.scoring.llm_client import LLMConfig
    fake_cfg = LLMConfig(base_url="http://x", api_key="k", model="m")
    def fake_post(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": json.dumps({
            "assessments": [
                {"requirement_id": "common_001", "status": "met",
                 "evidence": ["x"], "reason": "x"},
                {"requirement_id": "braking_001", "status": "met",
                 "evidence": ["x"], "reason": "x"},
                {"requirement_id": "steering_001", "status": "met",
                 "evidence": ["x"], "reason": "x"},
            ]
        }, ensure_ascii=False)}}]}
    out = run_score_v2(
        job_name="x", encrypt_job_id=eid, run_id=target,
        llm_config=fake_cfg, http_post=fake_post,
    )
    import sys as _s
    _s.stderr.write(f"\nDEBUG invalid_output out: {out}\n")
    _s.stderr.flush()
    assert out["ok"] is True
    # 非法 output 被重评 → candidate_scored
    assert out["status"] == "candidate_scored"
    assert out["data"]["candidate_id"] == "g000"


# ============================================================
# candidate_scored → repeat_score
# ============================================================

def test_candidate_scored_next_action_repeat_score(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "eid_test_repeat"
    target = "2026-08-05_120000"
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
    }, ensure_ascii=False), encoding="utf-8")
    jd = {"jobName": "x", "encryptJobId": eid, "bodyText": "本科", "formValues": []}
    (process_dir / "job_detail.json").write_text(json.dumps(jd, ensure_ascii=False),
                                                  encoding="utf-8")
    (process_dir / "new_resumes.json").write_text(json.dumps([
        {"name": "姓名0", "_meta": {"encrypt_geek_id": "g000",
                                       "encrypt_job_id": eid},
         "degree": "本科"},
    ], ensure_ascii=False), encoding="utf-8")
    sc_dir = process_dir / "scoring"
    sc_dir.mkdir(parents=True, exist_ok=True)
    sc = _good_scorecard(jd)
    (sc_dir / "job_scorecard.json").write_text(
        json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sc_dir / "manifest.json").write_text(json.dumps({
        "candidates": [{"geek_id": "g000", "name": "姓名0",
                         "input_path": "inputs/candidate_g000.json",
                         "output_path": "outputs/candidate_g000.json",
                         "status": "pending"}],
    }, ensure_ascii=False), encoding="utf-8")
    # inputs 也得建（不然 _score_one 找不到 input）
    (sc_dir / "inputs").mkdir(parents=True, exist_ok=True)
    (sc_dir / "inputs" / "candidate_g000.json").write_text(json.dumps({
        "name": "姓名0", "geek_id": "g000", "job_id": eid,
        "degree": "本科",
        "education": [{"school": "辽宁工业大学", "major": "x", "degree": "本科"}],
    }, ensure_ascii=False), encoding="utf-8")

    from boss_hr.scoring.llm_client import LLMConfig
    fake_cfg = LLMConfig(base_url="http://x", api_key="k", model="m")

    def fake_post(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": json.dumps({
            "assessments": [
                {"requirement_id": "common_001", "status": "met",
                 "evidence": ["x"], "reason": "x"},
                {"requirement_id": "braking_001", "status": "met",
                 "evidence": ["x"], "reason": "x"},
                {"requirement_id": "steering_001", "status": "met",
                 "evidence": ["x"], "reason": "x"},
            ]
        }, ensure_ascii=False)}}]}

    out = run_score_v2(
        job_name="x", encrypt_job_id=eid, run_id=target,
        llm_config=fake_cfg, http_post=fake_post,
    )
    assert out["ok"] is True
    assert out["status"] == "candidate_scored"
    assert out["next_action"] == "repeat_score"
    assert out["data"]["decision"] in ("A", "B", "C")
    assert "output_file" not in out["data"]


# ============================================================
# screening_results 排序：同类 final_score 倒序
# ============================================================

def test_screening_results_sort_by_final_score():
    p = load_policy()
    sc = {
        "source_jd_hash": "x",
        "policy_version": p["policy_version"],
        "alternative_tracks": [{"id": "g", "name": "g", "requirements": []}],
    }
    outputs = [
        _good_candidate_output("g1", "A", decision="A", final=90.0),
        _good_candidate_output("g2", "A2", decision="A", final=95.0),
        _good_candidate_output("g3", "B", decision="B", final=70.0),
        _good_candidate_output("g4", "B2", decision="B", final=75.0),
        _good_candidate_output("g5", "C", decision="C", final=30.0),
    ]
    res = _build_screening_results_v2(
        sc, outputs, run_id="rid", job_name="x", policy=p,
    )
    cands = res["candidates"]
    # A→B→C
    assert [c["decision"] for c in cands] == ["A", "A", "B", "B", "C"]
    # A 内：final_score 倒序
    assert cands[0]["final_score"] >= cands[1]["final_score"]
    assert cands[2]["final_score"] >= cands[3]["final_score"]


# ============================================================
# _has_legal_output: 严格 hash / 字段校验
# ============================================================

def test_has_legal_output_wrong_policy_hash(tmp_path):
    p = load_policy()
    sc = {
        "source_jd_hash": "h",
        "policy_version": p["policy_version"],
        "alternative_tracks": [],
    }
    target = tmp_path / "candidate.json"
    target.write_text(json.dumps(_good_candidate_output("g", "x")), encoding="utf-8")
    # 错 policy hash
    err_hash = "0" * 64
    assert not _has_legal_output(
        str(target), scorecard=sc,
        expected_policy_hash=err_hash,
        expected_geek_id="g", expected_job_id="eid1",
    )


def test_has_legal_output_wrong_geek_id(tmp_path):
    p = load_policy()
    sc = {
        "source_jd_hash": "h",
        "policy_version": p["policy_version"],
        "alternative_tracks": [],
    }
    target = tmp_path / "candidate.json"
    out = _good_candidate_output("g", "x")
    out["policy_hash"] = ph(p)
    target.write_text(json.dumps(out), encoding="utf-8")
    # 错 geek_id
    assert not _has_legal_output(
        str(target), scorecard=sc,
        expected_policy_hash=ph(p),
        expected_geek_id="other", expected_job_id="eid1",
    )


def test_has_legal_output_missing_file(tmp_path):
    assert not _has_legal_output(
        str(tmp_path / "no.json"), scorecard={},
        expected_policy_hash="h", expected_geek_id="g", expected_job_id="eid",
    )


# ============================================================
# 候选 input 缺 geek_id → 错误
# ============================================================

def test_score_one_rejects_geek_id_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "eid_test_mismatch"
    target = "2026-08-05_120000"
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
    }, ensure_ascii=False), encoding="utf-8")
    jd = {"jobName": "x", "encryptJobId": eid, "bodyText": "本科", "formValues": []}
    (process_dir / "job_detail.json").write_text(json.dumps(jd, ensure_ascii=False),
                                                  encoding="utf-8")
    (process_dir / "new_resumes.json").write_text(json.dumps([
        {"name": "x", "_meta": {"encrypt_geek_id": "g000", "encrypt_job_id": eid}},
    ], ensure_ascii=False), encoding="utf-8")
    sc_dir = process_dir / "scoring"
    sc_dir.mkdir(parents=True, exist_ok=True)
    sc = _good_scorecard(jd)
    (sc_dir / "job_scorecard.json").write_text(
        json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sc_dir / "manifest.json").write_text(json.dumps({
        "candidates": [{"geek_id": "g000", "name": "x",
                         "input_path": "inputs/candidate_g000.json",
                         "output_path": "outputs/candidate_g000.json"}],
    }, ensure_ascii=False), encoding="utf-8")
    in_dir = sc_dir / "inputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    # 故意把 input 里的 geek_id 写错
    (in_dir / "candidate_g000.json").write_text(json.dumps({
        "name": "x", "geek_id": "WRONG_ID", "job_id": eid,
    }, ensure_ascii=False), encoding="utf-8")

    from boss_hr.scoring.llm_client import LLMConfig
    out = run_score_v2(
        job_name="x", encrypt_job_id=eid, run_id=target,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        http_post=lambda *a, **kw: {"choices": [{"message": {"content": "{}"}}]},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "GEEK_ID_MISMATCH"


def test_score_one_rejects_job_id_mismatch(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "eid_test_jm"
    target = "2026-08-05_120000"
    run_dir = tmp_path / eid / "runs" / target
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid, "confirmed": True,
    }, ensure_ascii=False), encoding="utf-8")
    jd = {"jobName": "x", "encryptJobId": eid, "bodyText": "本科", "formValues": []}
    (process_dir / "job_detail.json").write_text(json.dumps(jd, ensure_ascii=False),
                                                  encoding="utf-8")
    (process_dir / "new_resumes.json").write_text(json.dumps([
        {"name": "x", "_meta": {"encrypt_geek_id": "g000", "encrypt_job_id": eid}},
    ], ensure_ascii=False), encoding="utf-8")
    sc_dir = process_dir / "scoring"
    sc_dir.mkdir(parents=True, exist_ok=True)
    sc = _good_scorecard(jd)
    (sc_dir / "job_scorecard.json").write_text(
        json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sc_dir / "manifest.json").write_text(json.dumps({
        "candidates": [{"geek_id": "g000", "name": "x",
                         "input_path": "inputs/candidate_g000.json",
                         "output_path": "outputs/candidate_g000.json"}],
    }, ensure_ascii=False), encoding="utf-8")
    in_dir = sc_dir / "inputs"
    in_dir.mkdir(parents=True, exist_ok=True)
    (in_dir / "candidate_g000.json").write_text(json.dumps({
        "name": "x", "geek_id": "g000", "job_id": "WRONG_EID",
        "degree": "本科",
        "education": [{"school": "辽宁工业大学", "major": "x", "degree": "本科"}],
    }, ensure_ascii=False), encoding="utf-8")

    from boss_hr.scoring.llm_client import LLMConfig
    out = run_score_v2(
        job_name="x", encrypt_job_id=eid, run_id=target,
        llm_config=LLMConfig(base_url="http://x", api_key="k", model="m"),
        http_post=lambda *a, **kw: {"choices": [{"message": {"content": "{}"}}]},
    )
    assert out["ok"] is False
    assert out["error"]["code"] == "JOB_ID_MISMATCH"
