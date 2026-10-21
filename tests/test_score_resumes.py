# -*- coding: utf-8 -*-
"""score_resumes 核心函数的单测

覆盖目标：
  1. calc_tier 边界值（70 / 69.9 / 60 / 59.9）
  2. calc_weighted / calc_total 公式正确性
  3. WEIGHTS 权重之和恒为 1.0
  4. _extract_school_name 智能拆分（多种分隔符）
  5. validate_score 的核心约束：
     - 有 school：edu 强制被 school_tier 覆盖
     - 有 school_name：优先于 school 字段
     - 表外学校：score=None, dims_edu_reason 标记缺失
     - 无学校字段：dims_edu_reason 标记缺失
  6. candidate_to_report / build_actions / build_meta schema 稳定
  7. CLI main() 端到端跑通（用临时 JSON）
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS = os.path.abspath(os.path.join(_HERE, "..", "resume-screener", "scripts"))
_SHARED = os.path.abspath(os.path.join(_HERE, "..", "shared"))
sys.path.insert(0, _SCRIPTS)
sys.path.insert(0, _SHARED)

import pytest  # noqa: E402

import score_resumes as sr  # noqa: E402


@pytest.fixture
def fake_run(tmp_path):
    """建一个最小合法 run 目录：runs/<run_id>/process/{new_resumes.json}。

    返回 (job_name, encrypt_job_id, run_id, run_dir)。
    用法：把 conftest 的 BOSS_HR_OUTPUT_DIR fixture（已把 OUTPUT_ROOT 指向 tmp_path）
    和这个 fixture 一起，就能直接 main() 跑完整 CLI。
    """
    job_name = "车架工程师"
    encrypt_job_id = "test_encrypt_job_id_abc123"
    run_id = "2026-01-01_000000"
    process_dir = tmp_path / encrypt_job_id / "runs" / run_id / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    # score_resumes.main() 跨 run 去重逻辑会从 process/ 读 *_resumes.json，
    # 用 name→geek_id 反查表兜底。空文件 → 任何 _llm_scores.json 都会被判
    # 「geek_id 不在当前 run 简历池」拒绝。写两份带 geek_id 的简历即可。
    (process_dir / "new_resumes.json").write_text(json.dumps([
        {
            "name": "测试 A",
            "_meta": {"encrypt_geek_id": "gid_A_abc", "encrypt_job_id": encrypt_job_id},
        },
        {
            "name": "测试 B",
            "_meta": {"encrypt_geek_id": "gid_B_def", "encrypt_job_id": encrypt_job_id},
        },
    ], ensure_ascii=False), encoding="utf-8")
    return job_name, encrypt_job_id, run_id, process_dir


# ============================================================
# 1. 权重 / 阈值常量不变量
# ============================================================

def test_weights_sum_to_one():
    """5 维度权重之和必须正好为 1.0，否则总分解释会失真"""
    assert abs(sum(sr.WEIGHTS.values()) - 1.0) < 1e-9
    assert set(sr.WEIGHTS.keys()) == {"edu", "exp", "skill", "proj", "major"}


def test_weights_pct_consistent_with_weights():
    for k, v in sr.WEIGHTS.items():
        assert sr.WEIGHTS_PCT[k] == round(v * 100), f"{k} 维度 WEIGHTS/WEIGHTS_PCT 不一致"


def test_tier_thresholds():
    assert sr.TIER_THRESHOLDS == {"推荐": 70, "待定": 60, "不推荐": 0}


# ============================================================
# 2. calc_tier 边界
# ============================================================

@pytest.mark.parametrize("total, expected", [
    (100, "推荐"),
    (70.0, "推荐"),
    (69.9, "待定"),
    (60.0, "待定"),
    (59.9, "不推荐"),
    (0, "不推荐"),
])
def test_calc_tier_boundaries(total, expected):
    assert sr.calc_tier(total) == expected


# ============================================================
# 3. calc_weighted / calc_total
# ============================================================

def test_calc_weighted_known_input():
    """SKILL.md 示例 A:edu=62, exp=80, skill=65, proj=60, major=100"""
    dims = {"edu": 62, "exp": 80, "skill": 65, "proj": 60, "major": 100}
    weighted = sr.calc_weighted(dims)
    assert weighted["edu"] == 15.5
    assert weighted["exp"] == 20.0
    assert weighted["skill"] == 16.25
    assert weighted["proj"] == 9.0
    assert weighted["major"] == 10.0


def test_calc_total_known_input():
    weighted = {"edu": 15.5, "exp": 20.0, "skill": 16.25, "proj": 9.0, "major": 10.0}
    # 15.5+20.0+16.25+9.0+10.0 = 70.75, round(_, 1) = 70.8
    assert sr.calc_total(weighted) == 70.8


def test_calc_total_all_zero():
    assert sr.calc_total({"edu": 0, "exp": 0, "skill": 0, "proj": 0, "major": 0}) == 0.0


def test_calc_total_all_hundred():
    weighted = sr.calc_weighted({"edu": 100, "exp": 100, "skill": 100, "proj": 100, "major": 100})
    assert sr.calc_total(weighted) == 100.0


# ============================================================
# 4. _extract_school_name
# ============================================================

@pytest.mark.parametrize("raw, expected", [
    ("辽宁工业大学",                     "辽宁工业大学"),
    ("辽宁工业大学/车辆工程/本科",        "辽宁工业大学"),
    ("辽宁工业大学 · 车辆工程 · 本科",    "辽宁工业大学"),
    ("辽宁工业大学(车辆工程)",            "辽宁工业大学"),
    ("辽宁工业大学（车辆工程）",          "辽宁工业大学"),
    ("江南大学/机械工程/硕士",            "江南大学"),
    ("",                                 ""),
    ("   ",                              ""),  # strip 后空
])
def test_extract_school_name_various_formats(raw, expected):
    assert sr._extract_school_name({"school": raw}) == expected


def test_extract_school_name_prefers_school_name_field():
    """school_name 字段非空时，优先级高于 school"""
    score = {"school_name": "江南大学", "school": "其他学校/计算机/本科"}
    assert sr._extract_school_name(score) == "江南大学"


def test_extract_school_name_empty_both():
    assert sr._extract_school_name({"school": "", "school_name": ""}) == ""


# ============================================================
# 5. validate_score —— 核心不变量
# ============================================================

def test_validate_score_school_in_table_overrides_edu():
    """LLM 即使给 edu 打 0/100，validate_score 也会被 school_tier 强制覆盖"""
    score = {
        "name": "张三",
        "school": "辽宁工业大学/车辆工程/本科",
        "dims": {"edu": 0, "exp": 80, "skill": 65, "proj": 60, "major": 100},
    }
    out = sr.validate_score(score)
    assert out["dims"]["edu"] == 62, "edu 应当被 school_tier 强制覆盖为 62（二本公办）"
    assert "辽宁工业大学" in out["dims_edu_reason"]


def test_validate_score_school_outside_table_marks_missing():
    score = {
        "name": "李四",
        "school": "野鸡大学/某专业/本科",
        "dims": {"edu": 75, "exp": 80, "skill": 65, "proj": 60, "major": 100},
    }
    out = sr.validate_score(score)
    # 表外学校：不信任 LLM 给的 edu，按 60 兜底并标"需复核"（见 SKILL.md）
    assert out["dims"]["edu"] == 60
    assert "缺失" in out["dims_edu_reason"]


def test_dedup_duplicate_name_not_falsely_skipped():
    """重名候选人（BOSS 的"杨先生""吕女士"匿名昵称）不能被误杀。

    回归：曾用 setdefault 建姓名→单个 ID 的映射，导致同名的第二人
    永远匹配到第一人的 ID，若第一人已评分则第二人被误判为"已评分"。
    """
    import tempfile
    sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "shared")))
    import output_manager
    from job_resume_store import JobResumeStore

    with tempfile.TemporaryDirectory() as tmpdir:
        _saved = output_manager.OUTPUT_ROOT
        output_manager.OUTPUT_ROOT = tmpdir
        try:
            store = JobResumeStore('测试重名Job', encrypt_job_id='job-1')
            # 两个同名不同 ID 的候选人，只有第一个评过分
            store.mark_scored('job-1', 'geek-A', name='杨先生', total=82.5,
                              tier='推荐', run_id='run-1')

            name_to_gids = {'杨先生': ['geek-A', 'geek-B']}

            def _unscored_gid(name):
                gids = name_to_gids.get(name)
                if not gids:
                    return ''
                for g in gids:
                    if not store.is_scored('job-1', g):
                        return g
                return None

            # geek-B 未评分 → 应放行并返回 geek-B（不是 geek-A）
            assert _unscored_gid('杨先生') == 'geek-B', '同名未评分者应被放行'

            # 两个都评过后才拦截
            store.mark_scored('job-1', 'geek-B', name='杨先生', total=80.5,
                              tier='推荐', run_id='run-2')
            assert _unscored_gid('杨先生') is None, '同名全部评过才应拦截'

            # 简历池里查无此人 → '' （与 None 区分，便于告警）
            assert _unscored_gid('查无此人') == ''
        finally:
            output_manager.OUTPUT_ROOT = _saved


def test_validate_score_no_school_field_marks_missing():
    score = {
        "name": "王五",
        "dims": {"edu": 80, "exp": 80, "skill": 65, "proj": 60, "major": 100},
    }
    out = sr.validate_score(score)
    assert "缺失" in out["dims_edu_reason"]
    # total/tier 仍按 5 维度计算
    assert "total" in out
    assert "tier" in out


def test_validate_score_recomputes_total_and_tier():
    """即使 LLM 给的 dims 完全正确，validate_score 也会重算 total + 判定 tier"""
    score = {
        "name": "赵六",
        "school": "辽宁工业大学/车辆工程/本科",
        "dims": {"edu": 62, "exp": 80, "skill": 65, "proj": 60, "major": 100},
    }
    out = sr.validate_score(score)
    # 15.5 + 20 + 16.25 + 9 + 10 = 70.75 → round 70.8
    assert out["total"] == 70.8
    assert out["tier"] == "推荐"
    assert set(out["weighted"].keys()) == {"edu", "exp", "skill", "proj", "major"}


def test_validate_score_uses_school_name_when_provided():
    score = {
        "name": "钱七",
        "school_name": "江南大学",
        "school": "江南大学/机械/本科",
        "dims": {"edu": 0, "exp": 80, "skill": 65, "proj": 60, "major": 100},
    }
    out = sr.validate_score(score)
    assert out["dims"]["edu"] == 85  # 211
    assert "江南大学" in out["dims_edu_reason"]


# ============================================================
# 6. candidate_to_report / build_actions / build_meta
# ============================================================

def _make_score(name, school, edu, exp, skill, proj, major, advice=""):
    s = {
        "name": name,
        "school": school,
        "dims": {"edu": edu, "exp": exp, "skill": skill, "proj": proj, "major": major},
        "highlights": ["亮点 A", "亮点 B"],
        "concerns": ["顾虑 X"],
        "advice": advice,
    }
    return sr.validate_score(s)


def test_candidate_to_report_shape():
    s = _make_score("测试1", "辽宁工业大学/车辆工程/本科", 62, 80, 65, 60, 100)
    c = sr.candidate_to_report(s, rank=1)
    assert c["rank"] == 1
    assert c["name"] == "测试1"
    assert c["advice"] == ""
    assert c["school"] == "辽宁工业大学/车辆工程/本科"
    assert c["tier"] in {"推荐", "待定", "不推荐"}
    assert isinstance(c["total"], (int, float))
    assert len(c["dimensions"]) == 5
    for d in c["dimensions"]:
        assert set(d.keys()) == {"pct", "weighted", "weight", "reason"}


def test_build_actions_buckets_correctly():
    candidates = [
        sr.candidate_to_report(
            _make_score("强力推荐", "清华大学/计算机/博士", 100, 95, 95, 95, 100, advice="尽快约面"),
            rank=1,
        ),
        sr.candidate_to_report(
            _make_score("待定哥", "辽宁工业大学/车辆工程/本科", 62, 60, 55, 55, 80, advice="问问项目"),
            rank=2,
        ),
        sr.candidate_to_report(
            _make_score("拒拒拒", "野鸡大学/X/本科", 40, 30, 25, 20, 30),
            rank=3,
        ),
    ]
    actions = sr.build_actions(candidates)
    assert set(actions.keys()) == {"recommend", "pending", "reject"}
    # 推荐/待定 桶里有 action 字段
    assert all("action" in item for item in actions["recommend"])
    assert all("action" in item for item in actions["pending"])
    assert actions["recommend"][0]["action"] == "尽快约面"
    assert actions["pending"][0]["action"] == "问问项目"
    # 不推荐 桶里有 concerns 字段
    assert all("concerns" in item for item in actions["reject"])


def test_build_meta_basic():
    meta = sr.build_meta("车架工程师", {"company": "XX 公司", "location": "宁波", "salary": "15-25K"})
    assert meta["title"] == "车架工程师 · 简历筛选报告"
    assert meta["job"]["name"] == "车架工程师"
    assert meta["job"]["company"] == "XX 公司"


# ============================================================
# 7. CLI main() 端到端
# ============================================================

def test_main_cli_end_to_end(tmp_path, fake_run):
    """模拟一个最小 LLM 评分 JSON 跑完整 CLI，验证输出 schema。

    不使用 capsys/capfd fixture（pytest 9.x 在某些环境 capture 不可用），
    直接验证 main() 写出的 result.json 文件结构。

    生产代码 --run-id 必填（数据边界），本测试必须显式传 --run-id。
    """
    job_name, encrypt_job_id, run_id, _run_dir = fake_run

    inp = tmp_path / "scores.json"
    out = tmp_path / "result.json"

    inp.write_text(json.dumps([
        {
            "name": "测试 A",
            "geek_id": "gid_A_abc",  # 显式 geek_id（fake_run 的 process/new_resumes.json 里已登记）
            "school": "辽宁工业大学/车辆工程/本科",
            "work_years": "3 年",
            "match_type": "结构设计",
            "dims": {"edu": 0, "exp": 80, "skill": 65, "proj": 60, "major": 100},
            "highlights": ["3 年 CATIA"],
            "concerns": ["非车架本体"],
            "advice": "电话沟通",
        },
        {
            "name": "测试 B",
            "geek_id": "gid_B_def",
            "school": "野鸡大学/X/本科",
            "work_years": "1 年",
            "match_type": "机械设计",
            "dims": {"edu": 0, "exp": 55, "skill": 50, "proj": 45, "major": 80},
            "highlights": ["会 SolidWorks"],
            "concerns": ["经验浅"],
            "advice": "不建议",
        },
    ], ensure_ascii=False), encoding="utf-8")

    # 备份 argv
    old_argv = sys.argv
    try:
        sys.argv = [
            "score_resumes.py",
            "--input", str(inp),
            "--output", str(out),
            "--job-name", job_name,
            "--encrypt-job-id", encrypt_job_id,
            "--run-id", run_id,  # 【必填】run_id 是数据边界
        ]
        sr.main()
    finally:
        sys.argv = old_argv

    assert out.exists()
    result = json.loads(out.read_text(encoding="utf-8"))
    assert result["job_name"] == job_name
    assert result["summary"]["total"] == 2
    # 两个人应该都被排进某个 tier
    for c in result["candidates"]:
        assert c["tier"] in {"推荐", "待定", "不推荐"}
    # actions 三段式必须存在
    assert set(result["actions"].keys()) == {"recommend", "pending", "reject"}


def test_main_cli_missing_run_id_exits(tmp_path, fake_run, capsys):
    """验证 main() 在 --run-id 缺失时立刻退出（生产代码的硬约束）。

    不降低生产代码 --run-id 必填的要求：本测试只验证 argparse 在缺失
    必填参数时立即抛 SystemExit(2)，而不是默默跑默认值。
    """
    _job_name, encrypt_job_id, _run_id, _run_dir = fake_run

    inp = tmp_path / "scores.json"
    out = tmp_path / "result.json"
    inp.write_text("[]", encoding="utf-8")

    old_argv = sys.argv
    try:
        sys.argv = [
            "score_resumes.py",
            "--input", str(inp),
            "--output", str(out),
            "--job-name", "车架工程师",
            "--encrypt-job-id", encrypt_job_id,
            # 故意不传 --run-id
        ]
        with pytest.raises(SystemExit) as exc_info:
            sr.main()
    finally:
        sys.argv = old_argv
    # argparse 退出码 = 2（与 v1.1 cli_runner / confirm_run 一致）
    assert exc_info.value.code == 2
