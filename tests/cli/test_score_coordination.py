# -*- coding: utf-8 -*-
"""boss-hr score v2（C1：候选协调 + 立即 LLM 评估）测试。

v2 协议：
  - boss-hr score 一次只处理一位候选人
  - LLM 在 CLI 内部调；父 agent 看不到 input_file / output_file
  - 一次 status="candidate_scored"，remaining--
  - 全部完成后 status="scoring_complete"

测试策略：
  - LLM 真实调用点（scorecard 生成 + 候选评估）都通过预置文件绕过：
    1. 预写 job_scorecard.json（hash 真实匹配）
    2. 预写 scoring/outputs/candidate_*.json（合法 v2）
  - 父 agent 与 LLM 均不连通真实服务；测试中 delete 一个 output 来
    触发"下一位候选人"分支。
"""
from __future__ import annotations
import hashlib
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
_SCORING_PKG = _TOOLKIT_ROOT / "boss_hr" / "scoring"


def _good_scorecard(eid: str, jd: Dict[str, Any]) -> Dict[str, Any]:
    from boss_hr.scoring.scorecard import normalize_jd, source_jd_hash
    from boss_hr.scoring.policy import load_policy, policy_hash as ph
    policy = load_policy()
    return {
        "schema_version": "2.0",
        "policy_version": policy["policy_version"],
        "policy_hash": ph(policy),
        "source_jd_hash": source_jd_hash(jd),
        "job_name": jd.get("jobName", "score_test_job"),
        "common_requirements": [
            {"id": "common_001", "description": "本科及以上学历",
             "importance": "must", "source_evidence": "JD 中说本科"},
        ],
        "alternative_tracks": [
            {"id": "braking", "name": "线控制动",
             "requirements": [
                 {"id": "braking_001", "description": "线控制动系统本体方案",
                  "importance": "must", "source_evidence": "JD 要求"},
             ]},
            {"id": "steering", "name": "线控转向",
             "requirements": [
                 {"id": "steering_001", "description": "线控转向系统本体方案",
                  "importance": "must", "source_evidence": "JD 要求"},
             ]},
        ],
        "education_requirements": {"minimum_degree": "本科及以上",
                                    "source_evidence": "JD 中"},
        "generated_at": "2026-08-05 12:00:00",
    }


def _install_mock_outputs(scoring_dir: str, manifest: Dict[str, Any],
                          scorecard: Dict[str, Any], eid: str) -> None:
    """为每位候选人预写合法 v2 output（让 score CLI 跳过 LLM 评估）。"""
    out_dir = os.path.join(scoring_dir, "outputs")
    os.makedirs(out_dir, exist_ok=True)
    for c in manifest.get("candidates", []):
        gid = c["geek_id"]
        common_ass = [
            {
                "requirement_id": r["id"],
                "status": "met",
                "evidence": ["mock evidence"],
                "reason": "mock met",
            }
            for r in scorecard.get("common_requirements", [])
        ]
        track_ass = [
            {
                "track_id": t["id"],
                "requirement_id": r["id"],
                "status": "met",
                "evidence": ["mock evidence"],
                "reason": "mock met",
            }
            for t in scorecard.get("alternative_tracks", [])
            for r in t.get("requirements", [])
        ]
        payload = {
            "schema_version": "2.0",
            "policy_version": scorecard["policy_version"],
            "policy_hash": scorecard["policy_hash"],
            "job_scorecard_hash": scorecard["source_jd_hash"],
            "name": c["name"],
            "geek_id": gid,
            "job_id": eid,
            "common_assessments": common_ass,
            "track_assessments": track_ass,
            "track_results": {
                t["id"]: {
                    "score": 90.0, "must_not_met": [],
                    "core_not_met": [], "core_partial": [],
                    "unverified_significant": [],
                }
                for t in scorecard.get("alternative_tracks", [])
            },
            "best_track": scorecard.get("alternative_tracks", [{}])[0].get("id", "general"),
            "best_track_reason": "mock",
            "job_match_score": 90.0,
            "education_score": 70.0,
            "education_tier": "一本公办",
            "education_reason": "mock",
            "education_review_required": False,
            "education_hard_pass": True,
            "education_hard_reason": "",
            "final_score": 87.0,
            "decision": "B",
            "decision_label": "部分技能仍需确认，但适合进入面试",
            "decision_reasons": ["mock"],
            "highlights": [], "concerns": [],
            "verification_questions": ["mock question"],
            "contact_recommended": True,
            "must_not_met": [],
            "core_not_met": [],
            "core_partial": [],
            "unverified_significant": [],
        }
        with open(os.path.join(out_dir, f"candidate_{gid}.json"),
                  "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    # 临时构造一个合法的 mock llm.env，并通过 BOSS_HR_LLM_CONFIG_PATH
    # 注入子进程，避免子进程意外读取开发者机器上真实的 ~/.boss-hr/llm.env。
    import tempfile as _tempfile
    _mock_env = _tempfile.NamedTemporaryFile(
        mode="w", suffix=".env", delete=False, encoding="utf-8",
        prefix="llm_mock_",
    )
    _mock_env.write(
        "BOSS_HR_LLM_BASE_URL=http://mock-llm.test/v1\n"
        "BOSS_HR_LLM_API_KEY=mock-key\n"
        "BOSS_HR_LLM_MODEL=mock-model\n"
        "BOSS_HR_LLM_TIMEOUT=60\n"
    )
    _mock_env.close()
    env = {**os.environ,
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_TOOLKIT_ROOT),  # 让 tests.cli._mock_llm_hook 可 import
           "BOSS_HR_LLM_BASE_URL": "http://mock-llm.test/v1",
           "BOSS_HR_LLM_API_KEY": "mock-key",
           "BOSS_HR_LLM_MODEL": "mock-model",
           "BOSS_HR_LLM_TIMEOUT": "60",
           "BOSS_HR_LLM_TEST_HOOK": "tests.cli._mock_llm_hook",
           "BOSS_HR_LLM_TEST_REQ_IDS": "common_001,braking_001,steering_001",
           "BOSS_HR_LLM_TEST_SCENARIO": "eval",
           "BOSS_HR_LLM_CONFIG_PATH": _mock_env.name,
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


def _file_hashes(root: Path) -> dict:
    out = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


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
        "steps_done": ["jd", "download"] if confirmed else ["jd"],
        "last_step": "download" if confirmed else "jd",
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
                 degree: str = "本科", eid: str = "test_eid_score") -> dict:
    return {
        "name": name, "degree": degree, "work_years": "3 年",
        "work_experience": [], "project_experience": [],
        "education": [{"school": school, "major": "车辆工程", "degree": degree, "start": "", "end": ""}],
        "_meta": {"encrypt_geek_id": geek_id, "encrypt_job_id": eid},
    }


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """target run + other run + v2 scorecard + 全部 output 预填。"""
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_score"
    job_name = "score_test_job"
    target = "2026-08-03_120000"
    other = "2026-08-02_120000"

    resumes = [
        _make_resume("张三", "gid_zhangsan_001", eid=eid),
        _make_resume("李四", "gid_lisi_002", eid=eid),
        _make_resume("王五", "gid_wangwu_003", eid=eid),
    ]
    run_dir = _make_run(tmp_path, eid, target, job_name,
              confirmed=True, new_resumes=resumes)
    _make_run(tmp_path, eid, other, job_name,
              confirmed=False, new_resumes=[_make_resume("other_a", "gid_other_a", eid=eid)])

    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    jd = json.loads((run_dir / "process" / "job_detail.json").read_text(encoding="utf-8"))
    sc = _good_scorecard(eid, jd)
    os.makedirs(scoring_dir, exist_ok=True)
    (scoring_dir / "job_scorecard.json").write_text(
        json.dumps(sc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path, eid, job_name, target, other, sc


def _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc):
    """调旧 prepare 子进程建 manifest + inputs + 预填所有 candidate output。"""
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


def _delete_one_output(tmp_path, eid, target, gid):
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    out = scoring_dir / "outputs" / f"candidate_{gid}.json"
    if out.exists():
        out.unlink()


# ============================================================
# 1. manifest 不存在时只调用一次 prepare
# ============================================================

def test_score_calls_prepare_when_manifest_missing(workspace):
    tmp_path, eid, job_name, target, _other, _sc = workspace
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    # 不预填 outputs，CLI 应自调用 prepare 建 manifest
    assert proc.returncode == 0
    manifest = tmp_path / eid / "runs" / target / "process" / "scoring" / "manifest.json"
    assert manifest.is_file()
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert len(m["candidates"]) == 3


# ============================================================
# 2. v2 协议：单次返回 candidate_scored
# ============================================================

def test_score_returns_candidate_scored_status(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    # 预填 manifest + 全部 outputs，再 delete 一位 → 该位重新走 candidate_scored
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    _delete_one_output(tmp_path, eid, target, manifest["candidates"][0]["geek_id"])

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "candidate_scored"
    assert p["data"]["candidate_id"] is not None
    assert p["data"]["name"] is not None
    # v2 不再暴露 input_file / output_file
    assert "input_file" not in p["data"]
    assert "output_file" not in p["data"]


# ============================================================
# 3. 不直接读 new_resumes 内容（只 stat）
# ============================================================

def test_score_does_not_read_full_new_resumes(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    _delete_one_output(tmp_path, eid, target, manifest["candidates"][0]["geek_id"])

    new_resumes_path = tmp_path / eid / "runs" / target / "process" / "new_resumes.json"
    mtime_before = new_resumes_path.stat().st_mtime_ns
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    mtime_after = new_resumes_path.stat().st_mtime_ns
    assert mtime_before == mtime_after


# ============================================================
# 4. 重复调用换下一位候选人
# ============================================================

def test_score_returns_next_candidate_after_first(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    # 只写第 0 个 output
    _delete_one_output(tmp_path, eid, target, manifest["candidates"][1]["geek_id"])
    _delete_one_output(tmp_path, eid, target, manifest["candidates"][2]["geek_id"])

    proc1 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    p1 = json.loads(_decode(proc1.stdout))
    first_gid = p1["data"]["candidate_id"]
    # 写回合法 output
    _install_mock_outputs(
        str(tmp_path / eid / "runs" / target / "process" / "scoring"),
        {"candidates": [c for c in manifest["candidates"] if c["geek_id"] == first_gid]},
        sc, eid,
    )
    proc2 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    p2 = json.loads(_decode(proc2.stdout))
    assert p2["data"]["candidate_id"] != first_gid


# ============================================================
# 5. 一次 score 写一个 v2 candidate output 文件
# ============================================================

def test_score_writes_v2_candidate_output(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    target_gid = manifest["candidates"][0]["geek_id"]
    _delete_one_output(tmp_path, eid, target, target_gid)

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["data"]["candidate_id"] == target_gid
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    out = scoring_dir / "outputs" / f"candidate_{target_gid}.json"
    assert out.is_file()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "2.0"
    assert payload["geek_id"] == target_gid
    assert payload["job_id"] == eid
    assert payload["policy_version"] == "abc-education-v1"
    assert payload["decision"] in ("A", "B", "C")


# ============================================================
# 6. run 不存在
# ============================================================

def test_score_run_not_found(workspace):
    tmp_path, eid, job_name, _target, _other, _sc = workspace
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", "9999-99-99_999999",
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 23
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert p["error"]["code"] == "RUN_NOT_FOUND"


# ============================================================
# 7. encrypt_job_id 不匹配
# ============================================================

def test_score_encrypt_job_id_mismatch(workspace):
    tmp_path, _eid, job_name, target, _other, _sc = workspace
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", "wrong_eid_xyz", "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 23
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False


# ============================================================
# 8. 未 confirmed
# ============================================================

def test_score_not_confirmed(workspace):
    tmp_path, eid, job_name, _target, other, _sc = workspace
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", other,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 20
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert p["error"]["code"] == "AWAITING_CONFIRMATION"


# ============================================================
# 9. 缺 new_resumes
# ============================================================

def test_score_missing_new_resumes(tmp_path, monkeypatch):
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    _make_run(tmp_path, "test_eid_score", "2026-08-03_120000", "no_new_resumes_job",
              confirmed=True, new_resumes=None)
    proc = _run_cli("score", "--job-name", "no_new_resumes_job",
                    "--encrypt-job-id", "test_eid_score", "--run-id", "2026-08-03_120000",
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 26
    p = json.loads(_decode(proc.stdout))
    assert p["ok"] is False
    assert "new_resumes" in p["error"]["message"]


# ============================================================
# 10. 不触发 collect_llm_scores / score_resumes 子进程
# ============================================================

def test_score_does_not_invoke_legacy_subprocess(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    _delete_one_output(tmp_path, eid, target, manifest["candidates"][0]["geek_id"])

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    llm_scores = tmp_path / eid / "runs" / target / "process" / "_llm_scores.json"
    assert not llm_scores.exists(), "v2 score 不应写 _llm_scores.json（v1 子协议）"


# ============================================================
# 11. 不触发 report
# ============================================================

def test_score_does_not_trigger_report(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    run_dir = tmp_path / eid / "runs" / target
    html_files = list(run_dir.glob("*.html"))
    assert not html_files, f"score 不应触发 report，但发现 HTML：{html_files}"


# ============================================================
# 12. 不修改 inputs/
# ============================================================

def test_score_does_not_modify_inputs(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    inputs_dir = scoring_dir / "inputs"
    before_inputs = _file_hashes(inputs_dir)

    for _ in range(3):
        _run_cli("score", "--job-name", job_name,
                 "--encrypt-job-id", eid, "--run-id", target,
                 env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})

    after_inputs = _file_hashes(inputs_dir)
    assert before_inputs == after_inputs, "score 不应修改 inputs/"


# ============================================================
# 13. 不扫描其他 run
# ============================================================

def test_score_does_not_borrow_other_run(workspace):
    tmp_path, eid, job_name, target, other, sc = workspace
    # 写诱饵 manifest 到 other run
    other_scoring = tmp_path / eid / "runs" / other / "process" / "scoring"
    other_scoring.mkdir(parents=True, exist_ok=True)
    (other_scoring / "manifest.json").write_text(json.dumps({
        "candidates": [{"geek_id": "诱饵_gid", "name": "诱饵",
                          "input_path": "inputs/candidate_诱饵_gid.json",
                          "output_path": "outputs/candidate_诱饵_gid.json",
                          "status": "pending"}],
    }, ensure_ascii=False), encoding="utf-8")

    # target run 预填所有合法 output → score 应直接 finalize
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["run_id"] == target
    # finalize 路径下没有 candidate_id
    assert p["status"] == "scoring_complete"


# ============================================================
# 14. 全部候选人评分完后 → scoring_complete
# ============================================================

def test_score_completes_after_all_candidates(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    # 全填好 → 一次 score 就 finalize
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "scoring_complete"
    assert p["data"]["scored"] == len(manifest["candidates"])
    assert "screening_results_file" in p["data"]
    assert p["next_action"] == "report"


# ============================================================
# 15. candidate_scored 时 next_action=repeat_score
# ============================================================

def test_score_candidate_scored_next_action(workspace):
    tmp_path, eid, job_name, target, _other, sc = workspace
    manifest = _prepare_workspace_with_outputs(tmp_path, eid, target, job_name, sc)
    _delete_one_output(tmp_path, eid, target, manifest["candidates"][0]["geek_id"])

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["next_action"] == "repeat_score"
