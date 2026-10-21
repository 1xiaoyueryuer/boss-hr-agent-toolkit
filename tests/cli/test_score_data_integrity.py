"""实际评分文件协议的回归：串人、重名、修正后重试，以及建议到 HTML。"""
import json
import os
from html import escape
from pathlib import Path
import subprocess
import sys

import pytest

from tests.cli.test_score_full_flow import (
    _decode, _make_resume, _make_run, _run_cli, _setup_outputs, _valid_score,
    workspace,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("changes,field", [
    ({"name": "别人的姓名"}, "name"),
    ({"geek_id": "wrong_gid"}, "geek_id"),
    ({"job_id": "wrong_job"}, "job_id"),
    ({"name": "别人的姓名", "geek_id": "wrong_gid", "job_id": "wrong_job"}, "geek_id"),
])
def test_wrong_identity_and_correction(workspace, changes, field):
    tmp_path, eid, job_name, target = workspace
    _setup_outputs(tmp_path, eid, target)
    process = tmp_path / eid / "runs" / target / "process"
    manifest = json.loads((process / "scoring" / "manifest.json").read_text(encoding="utf-8"))
    entry = manifest["candidates"][0]
    output = process / "scoring" / entry["output_path"]
    payload = _valid_score(entry["name"])
    payload.update(changes)
    output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    args = ("score", "--job-name", job_name, "--encrypt-job-id", eid, "--run-id", target)
    result = json.loads(_decode(_run_cli(*args).stdout))
    assert result["status"] == "waiting_llm"
    assert result["data"]["candidate_id"] == entry["geek_id"]
    assert field in " ".join(result["data"]["validation_error"])
    assert not (process / "screening_results.json").exists()
    assert not (tmp_path / eid / "state" / "scored_state.json").exists()

    output.write_text(json.dumps(_valid_score(entry["name"]), ensure_ascii=False), encoding="utf-8")
    result = json.loads(_decode(_run_cli(*args).stdout))
    assert result["status"] == "scoring_complete"
    scores = json.loads((process / "screening_results.json").read_text(encoding="utf-8"))
    assert {c["geek_id"] for c in scores["candidates"]} == {c["geek_id"] for c in manifest["candidates"]}


def test_same_name_candidates_cannot_exchange_output_files(tmp_path):
    eid, target, job_name = "same_name_job", "2026-08-03_120000", "重名测试"
    resumes = [_make_resume("杨先生", "gid_A"), _make_resume("杨先生", "gid_B")]
    _make_run(tmp_path, eid, target, job_name, new_resumes=resumes)
    args = ("score", "--job-name", job_name, "--encrypt-job-id", eid, "--run-id", target)
    _run_cli(*args)
    scoring = tmp_path / eid / "runs" / target / "process" / "scoring"
    entries = json.loads((scoring / "manifest.json").read_text(encoding="utf-8"))["candidates"]
    for entry, other in zip(entries, reversed(entries)):
        payload = {**_valid_score(entry["name"]), "geek_id": other["geek_id"]}
        (scoring / entry["output_path"]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    result = json.loads(_decode(_run_cli(*args).stdout))
    assert result["status"] == "waiting_llm"
    assert "geek_id" in " ".join(result["data"]["validation_error"])
    # 重名但 ID 正确时应保留两人各自的评分和状态。
    for i, entry in enumerate(entries):
        payload = {**_valid_score(entry["name"]), "geek_id": entry["geek_id"]}
        payload["dims"]["exp"] = 80 - 10 * i
        (scoring / entry["output_path"]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert json.loads(_decode(_run_cli(*args).stdout))["status"] == "scoring_complete"
    state = json.loads((tmp_path / eid / "state" / "scored_state.json").read_text(encoding="utf-8"))["items"]
    assert set(state) == {f"{eid}:gid_A", f"{eid}:gid_B"}
    assert state[f"{eid}:gid_A"]["total"] != state[f"{eid}:gid_B"]["total"]


def test_final_count_matches_report_after_history_dedup(workspace):
    tmp_path, eid, job_name, target = workspace
    _setup_outputs(tmp_path, eid, target)
    from job_resume_store import JobResumeStore
    store = JobResumeStore(job_name, encrypt_job_id=eid)
    store.mark_scored(eid, "gid_full_000", name="姓名0", total=80, tier="推荐", run_id="old_run")
    result = json.loads(_decode(_run_cli(
        "score", "--job-name", job_name, "--encrypt-job-id", eid, "--run-id", target).stdout))
    assert result["status"] == "scoring_complete"
    data = json.loads(Path(result["data"]["screening_results_file"]).read_text(encoding="utf-8"))
    assert result["data"]["scored"] == len(data["candidates"]) == 2
    assert "gid_full_000" not in {c["geek_id"] for c in data["candidates"]}


@pytest.mark.parametrize("value,bucket", [(90, "recommend"), (60, "pending")])
def test_advice_survives_score_to_html(workspace, value, bucket):
    tmp_path, eid, job_name, target = workspace
    _setup_outputs(tmp_path, eid, target)
    process = tmp_path / eid / "runs" / target / "process"
    entries = json.loads((process / "scoring" / "manifest.json").read_text(encoding="utf-8"))["candidates"]
    advice_by_gid = dict(zip([e["geek_id"] for e in entries], [
        "核实 CAN 总线调试职责，追问一次故障定位过程。",
        "确认 CATIA 曲面建模项目，沟通可接受的到岗日期。",
        "补充 C++ / A&B <模块>交付证据，再讨论岗位匹配。",
    ]))
    for entry in entries:
        payload = _valid_score(entry["name"])
        payload["advice"] = advice_by_gid[entry["geek_id"]]
        payload["dims"].update({k: value for k in ("exp", "skill", "proj", "major")})
        (process / "scoring" / entry["output_path"]).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    args = ("--job-name", job_name, "--encrypt-job-id", eid, "--run-id", target)
    result = json.loads(_decode(_run_cli("score", *args).stdout))
    assert result["status"] == "scoring_complete"
    data = json.loads((process / "screening_results.json").read_text(encoding="utf-8"))
    assert {c["geek_id"]: c["advice"] for c in data["candidates"]} == advice_by_gid
    assert len(data["actions"][bucket]) == len(entries)
    assert {a["name"]: a["action"] for a in data["actions"][bucket]} == {
        e["name"]: advice_by_gid[e["geek_id"]] for e in entries
    }
    report = json.loads(_decode(_run_cli("report", *args).stdout))
    html = Path(report["data"]["report_file"]).read_text(encoding="utf-8")
    for advice in advice_by_gid.values():
        assert escape(advice) in html


@pytest.mark.parametrize("rescore", [False, True])
def test_direct_scoring_rejects_unknown_id(workspace, rescore):
    tmp_path, eid, job_name, target = workspace
    process = tmp_path / eid / "runs" / target / "process"
    (process / "_llm_scores.json").write_text(json.dumps([
        {**_valid_score("姓名0"), "geek_id": "unknown_gid", "job_id": eid}
    ], ensure_ascii=False), encoding="utf-8")
    env = {**os.environ, "BOSS_HR_OUTPUT_DIR": str(tmp_path), "PYTHONUTF8": "1"}
    proc = subprocess.run([
        sys.executable, "-X", "utf8", str(ROOT / "resume-screener" / "scripts" / "score_resumes.py"),
        "--job-name", job_name, "--encrypt-job-id", eid, "--run-id", target,
        *(["--rescore"] if rescore else []),
    ], env=env, capture_output=True, timeout=30)
    assert proc.returncode == 26
    assert "unknown_gid" in _decode(proc.stdout)
    assert not (process / "screening_results.json").exists()
    assert not (tmp_path / eid / "state" / "scored_state.json").exists()
