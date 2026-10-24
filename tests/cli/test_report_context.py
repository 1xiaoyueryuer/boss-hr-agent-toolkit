"""实际采集结构到报告：JD、简历事实，以及旧评分里的已有分析。"""
import json
from pathlib import Path
import sys
import pytest

from tests.cli.test_score_full_flow import _decode, _make_resume, _make_run, _run_cli

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "shared"))
from report_context import enrich_score, normalize_job_info, select_education


def raw_jd():
    description = "岗位职责\n负责制动系统开发\n任职要求\n基本要求\n学历：本科及以上\n经验：5年以上\n技能要求\n掌握 FMEA 与 CAN 总线"
    return {
        "jobName": "制动工程师",
        "bodyText": "公   司\n测试科技有限公司\n经验\n5-10年\n学历\n本科\n薪资范围\n15k\n20k\n13个月\n1k=1千元\n奖金绩效",
        "formValues": ["制动工程师", description, "底盘工程师", "宁波鄞州区测试路1号"],
    }


def test_raw_jd_maps_to_report_fields():
    result = normalize_job_info(raw_jd())
    assert result["company"] == "测试科技有限公司"
    assert result["location"] == "宁波鄞州区测试路1号"
    assert result["salary"] == "15k–20k · 13个月"
    assert result["experience_required"] == "5-10年"
    assert result["degree_required"] == "本科"
    assert result["core_requirements"] == ["学历：本科及以上", "经验：5年以上", "掌握 FMEA 与 CAN 总线"]
    assert result["type_judgment"]["type"] == "底盘工程师"


def test_education_uses_highest_completed_degree_at_run_date():
    resume = {"degree": "硕士", "education": [
        {"school": "研究生学校", "degree": "硕士", "end": "20270701"},
        {"school": "本科院校", "degree": "本科", "end": "20240701"},
    ]}
    assert select_education(resume, "2026-08-13")["school"] == "本科院校"
    assert select_education(resume, "2027-08-13")["school"] == "研究生学校"


def test_enrichment_keeps_current_llm_fields_and_original_summary():
    score = {"dims": {"exp": 70}, "school_name": "模型已选学校", "work_years": "模型已核实年限",
             "dims_exp_reason": "当前格式理由", "exp": {"reasons": ["旧理由"]},
             "summary": "原始结论", "advice": "针对该候选人的建议"}
    result = enrich_score(score, {"work_years": "原始年限"}, "2026-08-13")
    for key in ("school_name", "work_years", "dims_exp_reason", "summary", "advice"):
        assert result[key] == score[key]


def test_missing_job_analysis_is_not_replaced_with_fixed_technical_claim():
    result = normalize_job_info({})
    assert result["type_judgment"]["type"] == ""
    assert "结构设计" not in result["type_judgment"]["reason"]


@pytest.mark.parametrize("description, expected", [
    ("岗位职责\n负责数据分析\n任职资格：\n熟悉 SQL 与 Python", ["熟悉 SQL 与 Python"]),
    ("负责用户研究与产品设计\n熟悉原型工具，有完整产品交付经验", []),
])
def test_job_description_is_kept_with_other_requirement_headings(description, expected):
    jd = {"jobName": "产品分析师", "formValues": ["产品分析师", description, "产品经理", "上海浦东新区"]}
    result = normalize_job_info(jd)
    assert result["description"] == description
    assert result.get("core_requirements", []) == expected
    assert result["type_judgment"]["type"] == "产品经理"


@pytest.mark.parametrize("education", [
    [{"school": "清华大学", "degree": "本科", "end": "20230101"},
     {"school": "北京大学", "degree": "本科", "end": "20200101"}],
    [{"school": "清华大学", "degree": "硕士", "end": "20280101"}],
])
def test_unresolved_education_is_shown_without_guessing_scoring_school(education):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "resume-screener" / "scripts"))
    from score_resumes import validate_score
    result = enrich_score({"name": "候选人", "dims": {k: 80 for k in ("exp", "skill", "proj", "major")}},
                          {"education": education}, "2026-08-13")
    assert "清华大学" in result["school"]
    validate_score(result)
    assert result["dims"]["edu"] == 60
    assert "需复核" in result["dims_edu_reason"]


def test_raw_jd_and_legacy_score_flow_to_final_html(tmp_path):
    eid, rid, name = "context_job", "2026-08-13_083550", "制动工程师"
    resume = _make_resume("候选人甲", "context_gid")
    resume["education"] = [{"school": "清华大学", "major": "车辆工程", "degree": "本科", "end": "20240701"}]
    resume["work_experience"] = [{"position": "系统工程师", "start": "20240701"}]
    run = _make_run(tmp_path, eid, rid, name, new_resumes=[resume])
    jd = {**raw_jd(), "encryptJobId": eid}
    (run / "process" / "job_detail.json").write_text(json.dumps(jd, ensure_ascii=False), encoding="utf-8")
    args = ("--job-name", name, "--encrypt-job-id", eid, "--run-id", rid)
    first = json.loads(_decode(_run_cli("score", *args).stdout))
    score = {"name": "候选人甲", "dims": {k: 80 for k in ("exp", "skill", "proj", "major")},
             "exp": {"score": 80, "reasons": ["有制动开发经历", "完成一次量产交付"]},
             "summary": "具备**制动系统集成**经验，核实其量产职责。"}
    Path(first["data"]["output_file"]).write_text(json.dumps(score, ensure_ascii=False), encoding="utf-8")
    result = json.loads(_decode(_run_cli("score", *args).stdout))
    assert result["status"] == "scoring_complete"
    data = json.loads(Path(result["data"]["screening_results_file"]).read_text(encoding="utf-8"))
    candidate = data["candidates"][0]
    assert data["meta"]["job"]["company"] == "测试科技有限公司"
    assert data["meta"]["core_requirements"]
    assert candidate["school"] == "清华大学 · 车辆工程 · 本科"
    assert candidate["work_years"] == "3 年"
    assert candidate["current_role"] == "最近职位：系统工程师"
    assert candidate["dimensions"][0]["pct"] == 100
    assert candidate["dimensions"][1]["reason"] == "有制动开发经历；完成一次量产交付"
    assert candidate["summary"] == score["summary"]
    assert candidate["advice"] == ""
    assert data["actions"]["recommend"][0]["action"] == ""
    assert data["actions"]["recommend"][0]["assessment"] == score["summary"]
    assert "清华大学" in data["actions"]["recommend"][0]["background"]
    report = json.loads(_decode(_run_cli("report", *args).stdout))
    html = Path(report["data"]["report_file"]).read_text(encoding="utf-8")
    for content in ["测试科技有限公司", "掌握 FMEA 与 CAN 总线", "量产交付", "<strong>制动系统集成</strong>", "综合评价"]:
        assert content in html
    for internal_label in ("报告说明", "原始 LLM", "原始评分未提供", "复原版", "校准版"):
        assert internal_label not in html
