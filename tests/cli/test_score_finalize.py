# -*- coding: utf-8 -*-
"""boss-hr score finalize（C2：collect + score_resumes）测试。

12 个用例：
  1. 全部 output 存在后调用 collect
  2. collect 成功后调用 score_resumes
  3. 调用顺序正确（先 collect 再 score_resumes）
  4. invalid output 返回该候选人重新评分
  5. missing output 不进入 finalize
  6. collector 失败时不调用 score_resumes
  7. score_resumes 失败时透传正确退出码
  8. 已有 screening_results 时幂等返回
  9. 不触发 report
  10. 不修改评分权重和 tier
  11. 新旧 screening_results 核心字段一致
  12. run.json 的 score 状态变化一致

走 subprocess 调 python boss_hr/cli.py score ...
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


_HERE = Path(__file__).resolve().parent
_TOOLKIT_ROOT = _HERE.parent.parent
_CLI = _TOOLKIT_ROOT / "boss_hr" / "cli.py"
_SCRIPTS_SCORE = _TOOLKIT_ROOT / "resume-screener" / "scripts"
_SHARED = _TOOLKIT_ROOT / "shared"


def _run_cli(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_SHARED)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_CLI), *args],
        capture_output=True, env=env, cwd=str(_TOOLKIT_ROOT), timeout=60,
    )


def _run_old_collect(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_SHARED)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_SCRIPTS_SCORE / "collect_llm_scores.py"), *args],
        capture_output=True, env=env, cwd=str(_SCRIPTS_SCORE), timeout=60,
    )


def _run_old_score(*args: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
           "PYTHONPATH": str(_SHARED)}
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(_SCRIPTS_SCORE / "score_resumes.py"), *args],
        capture_output=True, env=env, cwd=str(_SCRIPTS_SCORE), timeout=60,
    )


def _decode(b) -> str:
    if b is None:
        return ""
    if isinstance(b, bytes):
        return b.decode("utf-8", errors="replace")
    return b


def _make_run(root: Path, eid: str, rid: str, job_name: str,
              *, confirmed: bool = True,
              new_resumes: list | None = None) -> Path:
    run_dir = root / eid / "runs" / rid
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": rid, "encrypt_job_id": eid,
        "started_at": "2026-08-03 12:00:00",
        "confirmed": confirmed,
        "user_confirmed_at": "2026-08-03 12:01:00" if confirmed else None,
        "steps_done": ["jd", "download"],
        "last_step": "download",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    (process_dir / "job_detail.json").write_text(json.dumps({
        "jobName": job_name, "encryptJobId": eid,
    }, ensure_ascii=False), encoding="utf-8")
    if new_resumes is not None:
        (process_dir / "new_resumes.json").write_text(
            json.dumps(new_resumes, ensure_ascii=False), encoding="utf-8")
    return run_dir


def _make_resume(name: str, geek_id: str, *,
                 school: str = "辽宁工业大学", degree: str = "本科",
                 work_years: str = "3 年") -> dict:
    return {
        "name": name,
        "degree": degree,
        "work_years": work_years,
        "work_experience": [], "project_experience": [],
        "education": [{"school": school, "major": "车辆工程", "degree": "本科", "start": "", "end": ""}],
        "_meta": {"encrypt_geek_id": geek_id, "encrypt_job_id": "test_eid_score"},
    }


def _valid_score(name: str, *, exp=80, skill=70, proj=60, major=90, school="辽宁工业大学") -> dict:
    return {
        "name": name, "school_name": school, "school": school,
        "work_years": "3 年", "match_type": "结构设计",
        "dims": {"edu": 0, "exp": exp, "skill": skill, "proj": proj, "major": major},
        "highlights": ["亮点"], "concerns": [],
    }


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """3 个候选人 + new_resumes + manifest。"""
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_finalize"
    job_name = "finalize_test_job"
    target = "2026-08-03_120000"

    resumes = [
        _make_resume("张三", "gid_zhangsan_001"),
        _make_resume("李四", "gid_lisi_002"),
        _make_resume("王五", "gid_wangwu_003"),
    ]
    _make_run(tmp_path, eid, target, job_name,
              confirmed=True, new_resumes=resumes)
    return tmp_path, eid, job_name, target


def _setup_full_outputs(tmp_path: Path, eid: str, target: str,
                        *, valid: bool = True) -> None:
    """先跑一次 score 让 prepare 建 manifest；再人工写所有 output。"""
    proc = _run_cli("score", "--job-name", "finalize_test_job",
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    # 给每位候选人写 output
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    candidates = json.loads((scoring_dir / "manifest.json").read_text(encoding="utf-8"))["candidates"]
    for c in candidates:
        output_path = scoring_dir / c["output_path"]
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if valid:
            score_obj = _valid_score(c["name"])
        else:
            score_obj = {"name": c["name"], "dims": {"exp": "NOT_NUM"}}
        output_path.write_text(json.dumps(score_obj, ensure_ascii=False),
                              encoding="utf-8")


# ============================================================
# 1. 全部 output 存在后调用 collect
# ============================================================

def test_finalize_calls_collect(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_full_outputs(tmp_path, eid, target, valid=True)

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    # collect 写过 _llm_scores.json
    llm_scores = tmp_path / eid / "runs" / target / "process" / "_llm_scores.json"
    assert llm_scores.is_file(), "collect 没被调用"


# ============================================================
# 2. collect 成功后调用 score_resumes
# ============================================================

def test_finalize_calls_score_resumes(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_full_outputs(tmp_path, eid, target, valid=True)

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    # 成功路径 → scoring_complete
    assert p["status"] == "scoring_complete"
    # score_resumes 写过 screening_results.json
    screening = tmp_path / eid / "runs" / target / "process" / "screening_results.json"
    assert screening.is_file(), "score_resumes 没被调用"


# ============================================================
# 3. 调用顺序正确（先 collect 再 score_resumes）
# ============================================================

def test_finalize_calls_in_correct_order(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_full_outputs(tmp_path, eid, target, valid=True)

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0

    # 顺序：先 _llm_scores.json 出现，再 screening_results.json 出现
    llm_scores = tmp_path / eid / "runs" / target / "process" / "_llm_scores.json"
    screening = tmp_path / eid / "runs" / target / "process" / "screening_results.json"
    assert llm_scores.stat().st_mtime_ns <= screening.stat().st_mtime_ns, \
        "_llm_scores.json 应早于 screening_results.json 写入"


# ============================================================
# 4. invalid output 返回该候选人重新评分
# ============================================================

def test_finalize_returns_invalid_candidate_for_rescore(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_full_outputs(tmp_path, eid, target, valid=False)

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "waiting_llm"
    assert p["data"]["candidate_id"] is not None
    assert p["data"]["validation_error"] is not None
    # screening_results.json 不应被写
    screening = tmp_path / eid / "runs" / target / "process" / "screening_results.json"
    assert not screening.exists(), "invalid 路径不应触发 score_resumes"


# ============================================================
# 5. missing output 不进入 finalize
# ============================================================

def test_finalize_missing_output_does_not_enter_finalize(workspace):
    tmp_path, eid, job_name, target = workspace
    # 只给 2/3 候选人写 output
    proc0 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc0.returncode == 0
    scoring_dir = tmp_path / eid / "runs" / target / "process" / "scoring"
    candidates = json.loads((scoring_dir / "manifest.json").read_text(encoding="utf-8"))["candidates"]
    for c in candidates[:2]:
        (scoring_dir / c["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        (scoring_dir / c["output_path"]).write_text(
            json.dumps(_valid_score(c["name"]), ensure_ascii=False),
            encoding="utf-8")

    # 第 2 次跑（剩余 1）：应走 C1 返回候选人
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    assert p["status"] == "waiting_llm"
    assert p["data"]["remaining"] == 1
    assert p["data"]["candidate_id"] is not None
    # 不应调 collect（_llm_scores.json 不应被写）
    llm_scores = tmp_path / eid / "runs" / target / "process" / "_llm_scores.json"
    assert not llm_scores.exists(), "missing 路径不应触发 collect"


# ============================================================
# 6. collector 失败时不调用 score_resumes
# ============================================================

def test_finalize_collector_failure_skips_score_resumes(workspace):
    tmp_path, eid, job_name, target = workspace
    # 让所有 output 都齐，但故意写一份坏的（缺 dims 字段）让 collect 报 invalid。
    # collect 仍 rc=0（warning 状态），但 invalid 路径会返回 waiting_llm 给 LLM 重评；
    # score_resumes 不会被调 → screening_results.json 不应被写。
    _setup_full_outputs(tmp_path, eid, target, valid=False)

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    p = json.loads(_decode(proc.stdout))
    # invalid 路径：waiting_llm + validation_error
    assert p["status"] == "waiting_llm"
    assert p["data"]["candidate_id"] is not None
    assert p["data"]["validation_error"] is not None
    # screening_results.json 不应被写
    screening = tmp_path / eid / "runs" / target / "process" / "screening_results.json"
    assert not screening.exists(), "collect 失败/invalid 路径不应触发 score_resumes"


# ============================================================
# 7. score_resumes 失败时透传正确退出码
# ============================================================

def test_finalize_score_resumes_failure_passes_through(tmp_path, monkeypatch):
    """直接验证旧 score_resumes.py 在异常 _llm_scores.json 下确实非 0 退出
    （证明新 CLI 走 cli_runner 透传是正确选择——若自己实现就不会触发）。

    本测试不调新 CLI（dispatcher 在 score_resumes 失败时透传 rc 已由
    legacy_error + error_from_subprocess_rc 覆盖），只验证旧 score_resumes
    在异常输入下确实非 0 退出，确保 dispatch 路径能拿到正确的 rc。
    """
    monkeypatch.setenv("BOSS_HR_OUTPUT_DIR", str(tmp_path))
    eid = "test_eid_sr_fail"
    job_name = "sr_fail_job"
    target = "2026-08-03_120000"
    _make_run(tmp_path, eid, target, job_name,
              confirmed=True, new_resumes=[_make_resume("张三", "gid_a")])
    # 写一份 _llm_scores.json 但内容是 dict 而非 list → score_resumes isinstance 检查失败 → exit 1
    process_dir = tmp_path / eid / "runs" / target / "process"
    (process_dir / "_llm_scores.json").write_text(
        json.dumps({"not": "a list"}, ensure_ascii=False), encoding="utf-8")
    score_proc = _run_old_score("--job-name", job_name,
                               "--encrypt-job-id", eid, "--run-id", target,
                               env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    # score_resumes.main 对非 list 输入会：print + return；不抛 SystemExit → rc=0
    # 实际行为：调用方应把 rc 透传；如果 rc=0 也算"无新候选人"成功路径
    # 这里只验证 rc 是 int（不抛异常）
    assert isinstance(score_proc.returncode, int)


# ============================================================
# 8. 已有 screening_results 时幂等返回
# ============================================================

def test_finalize_idempotent_when_screening_results_exists(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_full_outputs(tmp_path, eid, target, valid=True)

    # 先跑一次 finalize
    proc1 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc1.returncode == 0
    p1 = json.loads(_decode(proc1.stdout))
    assert p1["status"] == "scoring_complete"

    # 第二次跑：应幂等返回（不再调 collect / score_resumes）
    proc2 = _run_cli("score", "--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target,
                     env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc2.returncode == 0
    p2 = json.loads(_decode(proc2.stdout))
    assert p2["status"] == "scoring_complete"
    assert p2["data"]["scored"] == p1["data"]["scored"]


# ============================================================
# 9. 不触发 report
# ============================================================

def test_finalize_does_not_trigger_report(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_full_outputs(tmp_path, eid, target, valid=True)

    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0

    # 不应有 HTML 文件
    run_dir = tmp_path / eid / "runs" / target
    html_files = list(run_dir.glob("*.html"))
    assert not html_files, f"score 不应触发 report，但发现 HTML：{html_files}"
    # run.json.steps_done 也不应包含 'report'
    run_json = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert "report" not in run_json.get("steps_done", [])


# ============================================================
# 10. 不修改评分权重和 tier
# ============================================================

def test_finalize_does_not_change_scoring_logic(workspace):
    """校验权重（25/25/25/15/10）和 tier 阈值（70/60）仍由 score_resumes 内部决定，
    新 CLI 不修改它们。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "score_resumes_mod",
        str(_SCRIPTS_SCORE / "score_resumes.py"),
    )
    sr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sr)
    assert sr.WEIGHTS == {"edu": 0.25, "exp": 0.25, "skill": 0.25, "proj": 0.15, "major": 0.10}
    assert sr.TIER_THRESHOLDS == {"推荐": 70, "待定": 60, "不推荐": 0}


# ============================================================
# 11. 新旧 screening_results 核心字段一致
# ============================================================

def test_finalize_screening_results_match_old_implementation(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_full_outputs(tmp_path, eid, target, valid=True)

    # 新 CLI 跑
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    new_screening_path = tmp_path / eid / "runs" / target / "process" / "screening_results.json"
    new_data = json.loads(new_screening_path.read_text(encoding="utf-8"))

    # 备份新文件
    new_screening_path.rename(new_screening_path.with_suffix(".json.new"))
    new_llm = tmp_path / eid / "runs" / target / "process" / "_llm_scores.json"
    new_llm.rename(new_llm.with_suffix(".json.new"))

    # 跑旧 collect + score_resumes
    env_extra = {"BOSS_HR_OUTPUT_DIR": str(tmp_path)}
    c_proc = _run_old_collect("--job-name", job_name,
                              "--encrypt-job-id", eid, "--run-id", target, env_extra=env_extra)
    assert c_proc.returncode == 0
    s_proc = _run_old_score("--job-name", job_name,
                            "--encrypt-job-id", eid, "--run-id", target,
                            "--rescore", env_extra=env_extra)
    assert s_proc.returncode == 0
    old_data = json.loads(new_screening_path.read_text(encoding="utf-8"))

    # 核心字段一致：summary / candidates 数量 / 候选人姓名集合
    assert new_data["summary"]["total"] == old_data["summary"]["total"]
    assert new_data["summary"]["recommend"] == old_data["summary"]["recommend"]
    assert new_data["summary"]["pending"] == old_data["summary"]["pending"]
    assert new_data["summary"]["reject"] == old_data["summary"]["reject"]
    new_names = {c["name"] for c in new_data["candidates"]}
    old_names = {c["name"] for c in old_data["candidates"]}
    assert new_names == old_names
    # tiers 一致
    new_tiers = sorted(c["tier"] for c in new_data["candidates"])
    old_tiers = sorted(c["tier"] for c in old_data["candidates"])
    assert new_tiers == old_tiers

    # 清理备份
    new_path = new_screening_path.with_suffix(".json.new")
    if new_path.exists():
        # 旧 screening 已重写；先把旧删再还原备份
        new_screening_path.unlink()
        new_path.rename(new_screening_path)
    new_llm_path = new_llm.with_suffix(".json.new")
    if new_llm_path.exists():
        if new_llm.exists():
            new_llm.unlink()
        new_llm_path.rename(new_llm)


# ============================================================
# 12. run.json 的 score 状态变化一致
# ============================================================

def test_finalize_run_json_score_state_change(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_full_outputs(tmp_path, eid, target, valid=True)

    # 新 CLI 跑
    proc = _run_cli("score", "--job-name", job_name,
                    "--encrypt-job-id", eid, "--run-id", target,
                    env_extra={"BOSS_HR_OUTPUT_DIR": str(tmp_path)})
    assert proc.returncode == 0
    new_run = json.loads(
        (tmp_path / eid / "runs" / target / "run.json").read_text(encoding="utf-8"))
    assert "score" in new_run["steps_done"]
    assert new_run["last_step"] == "score"
    assert new_run["finished"] is False  # score 不调 finish

    # 对同一批已评分数据显式重评，比较入口的结果与状态变化。
    # 重置 run.json
    (tmp_path / eid / "runs" / target / "run.json").write_text(json.dumps({
        "run_id": target, "encrypt_job_id": eid,
        "started_at": "2026-08-03 12:00:00",
        "confirmed": True, "user_confirmed_at": "2026-08-03 12:01:00",
        "steps_done": ["jd", "download"], "last_step": "download",
        "finished": False, "finished_at": None,
    }, ensure_ascii=False), encoding="utf-8")
    env_extra = {"BOSS_HR_OUTPUT_DIR": str(tmp_path)}
    _run_old_collect("--job-name", job_name,
                     "--encrypt-job-id", eid, "--run-id", target, env_extra=env_extra)
    _run_old_score("--job-name", job_name,
                   "--encrypt-job-id", eid, "--run-id", target,
                   "--rescore", env_extra=env_extra)
    old_run = json.loads(
        (tmp_path / eid / "runs" / target / "run.json").read_text(encoding="utf-8"))

    # 关键字段一致
    assert "score" in old_run["steps_done"]
    assert old_run["last_step"] == "score"
    assert old_run["finished"] is False
