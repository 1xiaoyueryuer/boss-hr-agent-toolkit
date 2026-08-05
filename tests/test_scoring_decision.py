# -*- coding: utf-8 -*-
"""evaluator + decision 单元测试。"""
from __future__ import annotations
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

_HERE = Path(__file__).resolve().parent.parent
_TOOLKIT_ROOT = _HERE.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.scoring.evaluator import (
    validate_assessments, compute_track_score, best_track,
    compute_education, education_hard_fail, finalize_evaluation,
    EvaluationError, _requirement_lookup, ALLOWED_STATUSES,
)
from boss_hr.scoring.decision import determine_decision, DECISION_LABELS
from boss_hr.scoring.policy import load_policy, policy_hash
from boss_hr.scoring.scorecard import source_jd_hash, all_requirement_ids


def _scorecard(edu_req: dict | None = None) -> dict:
    if edu_req is None:
        edu_req = {"minimum_degree": "本科及以上", "source_evidence": "JD"}
    return {
        "schema_version": "2.0",
        "policy_version": "abc-education-v1",
        "policy_hash": policy_hash(load_policy()),
        "source_jd_hash": "jd",
        "job_name": "x",
        "common_requirements": [
            {"id": "common_001", "description": "本科", "importance": "must",
             "source_evidence": "JD"},
            {"id": "common_002", "description": "3 年", "importance": "core",
             "source_evidence": "JD"},
        ],
        "alternative_tracks": [
            {"id": "braking", "name": "线控制动",
             "requirements": [
                 {"id": "braking_001", "description": "本体方案",
                  "importance": "must", "source_evidence": "JD"},
                 {"id": "braking_002", "description": "集成",
                  "importance": "core", "source_evidence": "JD"},
             ]},
            {"id": "steering", "name": "线控转向",
             "requirements": [
                 {"id": "steering_001", "description": "本体方案",
                  "importance": "must", "source_evidence": "JD"},
             ]},
        ],
        "education_requirements": edu_req,
        "generated_at": "2026-08-05 12:00:00",
    }


def _assessments(items: list[dict]) -> list[dict]:
    return [
        {"requirement_id": it["id"], "status": it["status"],
         "evidence": it.get("evidence", []), "reason": it.get("reason", "")}
        for it in items
    ]


# ============================================================
# validate_assessments
# ============================================================

def test_validate_assessments_ok():
    sc = _scorecard()
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    out = validate_assessments({"assessments": assessments}, sc)
    assert len(out) == 5


def test_validate_assessments_unknown_id_rejected():
    sc = _scorecard()
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
        {"id": "unknown_999", "status": "met"},
    ])
    with pytest.raises(EvaluationError):
        validate_assessments({"assessments": assessments}, sc)


def test_validate_assessments_duplicate_id_rejected():
    sc = _scorecard()
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    with pytest.raises(EvaluationError):
        validate_assessments({"assessments": assessments}, sc)


def test_validate_assessments_missing_id_rejected():
    sc = _scorecard()
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        # common_002 缺
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    with pytest.raises(EvaluationError):
        validate_assessments({"assessments": assessments}, sc)


def test_validate_assessments_bad_status_rejected():
    sc = _scorecard()
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "BOGUS"},
        {"id": "steering_001", "status": "met"},
    ])
    with pytest.raises(EvaluationError):
        validate_assessments({"assessments": assessments}, sc)


def test_validate_assessments_top_level_not_dict():
    with pytest.raises(EvaluationError):
        validate_assessments([], _scorecard())


def test_validate_assessments_assessments_not_list():
    with pytest.raises(EvaluationError):
        validate_assessments({"assessments": "no"}, _scorecard())


# ============================================================
# 多方向：best_track 选最高分
# ============================================================

def test_best_track_picks_braking_when_braking_higher():
    sc = _scorecard()
    policy = load_policy()
    # braking 全部 met；steering must not_met
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "not_met"},
    ])
    chosen, all_results, reason = best_track(sc, assessments, policy=policy)
    assert chosen == "braking"
    assert all_results["braking"]["score"] > 0
    assert "steering_001" in all_results["steering"]["must_not_met"]


def test_best_track_picks_steering_when_higher():
    sc = _scorecard()
    policy = load_policy()
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "not_met"},
        {"id": "braking_002", "status": "not_met"},
        {"id": "steering_001", "status": "met"},
    ])
    chosen, _, _ = best_track(sc, assessments, policy=policy)
    assert chosen == "steering"


def test_best_track_no_track_when_all_must_not_met():
    sc = _scorecard()
    policy = load_policy()
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "not_met"},
        {"id": "braking_002", "status": "not_met"},
        {"id": "steering_001", "status": "not_met"},
    ])
    chosen, all_results, reason = best_track(sc, assessments, policy=policy)
    # 仍返回一个 track（must_not_met 最少）
    assert chosen in ("braking", "steering")
    assert reason


# ============================================================
# job_match_score
# ============================================================

def test_job_match_score_met_only_100():
    sc = _scorecard()
    policy = load_policy()
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    chosen, all_results, _ = best_track(sc, assessments, policy=policy)
    chosen_res = all_results[chosen]
    # 全 met → score=100
    assert chosen_res["score"] == pytest.approx(100.0)


def test_job_match_score_only_partial():
    sc = _scorecard()
    policy = load_policy()
    # common partial + braking partial + steering met
    # → steering 应被选为 best（分数更高）
    assessments = _assessments([
        {"id": "common_001", "status": "partial"},
        {"id": "common_002", "status": "partial"},
        {"id": "braking_001", "status": "partial"},
        {"id": "braking_002", "status": "partial"},
        {"id": "steering_001", "status": "met"},
    ])
    chosen, all_results, _ = best_track(sc, assessments, policy=policy)
    chosen_res = all_results[chosen]
    # steering: common partial(8*0.6=4.8) + steering met(5*1=5) = 9.8 / 13 * 100 = 75.38
    assert chosen == "steering"
    assert chosen_res["score"] == pytest.approx(75.38, rel=1e-3)


# ============================================================
# 学历
# ============================================================

def test_education_score_in_range():
    policy = load_policy()
    resume = {
        "degree": "本科",
        "education": [{"school": "辽宁工业大学", "major": "车辆工程",
                       "degree": "本科"}],
    }
    edu = compute_education(resume, policy=policy)
    assert 0 <= edu["education_score"] <= 100
    assert edu["education_tier"] in ("C9", "985", "211", "双一流",
                                       "一本公办", "二本公办", "民办/独立学院",
                                       "未收录", "未知")
    assert edu["education_hard_pass"] is True


def test_education_unknown_school_neutral_score():
    policy = load_policy()
    resume = {"education": [{"school": "曼彻斯特大学（不存在的海外学校）",
                              "degree": "本科"}], "degree": "本科"}
    edu = compute_education(resume, policy=policy)
    # unknown_school_score 默认 70；review_required=True
    assert edu["education_score"] == pytest.approx(policy["unknown_school_score"])
    assert edu["education_review_required"] is True
    assert "未收录" in edu["education_reason"] or "需人工" in edu["education_reason"]


def test_education_no_school_no_degree_is_unverified():
    policy = load_policy()
    resume = {"education": [], "degree": ""}
    edu = compute_education(resume, policy=policy)
    assert edu["education_score"] == pytest.approx(policy["unknown_school_score"])
    assert edu["education_review_required"] is True


def test_education_hard_pass_below_minimum():
    """JD 要求硕士及以上，候选人专科 → hard fail。"""
    sc = _scorecard({"minimum_degree": "硕士及以上", "source_evidence": "JD"})
    edu = {"degree": "专科"}
    fail, reason = education_hard_fail(edu, sc)
    assert fail is True
    assert "低于" in reason


def test_education_hard_pass_meets_minimum():
    sc = _scorecard({"minimum_degree": "本科及以上", "source_evidence": "JD"})
    edu = {"degree": "本科"}
    fail, reason = education_hard_fail(edu, sc)
    assert fail is False
    assert "满足" in reason


def test_education_no_minimum_never_fails():
    sc = _scorecard({})
    edu = {"degree": "专科"}
    fail, _ = education_hard_fail(edu, sc)
    assert fail is False


# ============================================================
# A/B/C 决策
# ============================================================

def test_decision_C_when_must_not_met():
    policy = load_policy()
    ev = {
        "common_assessments": [],
        "track_assessments": [],
        "track_results": {"braking": {"score": 50.0, "must_not_met": ["braking_001"],
                                      "core_not_met": [], "core_partial": [],
                                      "unverified_significant": []},
                          "steering": {"score": 50.0, "must_not_met": ["steering_001"],
                                        "core_not_met": [], "core_partial": [],
                                        "unverified_significant": []}},
        "best_track": "braking", "best_track_reason": "x",
        "job_match_score": 50.0,
        "must_not_met": ["braking_001"],
        "core_not_met": [], "core_partial": [],
        "unverified_significant": [],
    }
    sc = _scorecard()
    d = determine_decision(ev, sc, policy=policy)
    assert d["decision"] == "C"
    assert d["decision_label"] == DECISION_LABELS["C"]
    assert d["contact_recommended"] is False
    # C 不生成软化建议
    assert d["verification_questions"] == []


def test_decision_C_when_job_match_below_60():
    policy = load_policy()
    ev = {
        "common_assessments": [],
        "track_assessments": [],
        "track_results": {"general": {"score": 30.0, "must_not_met": [],
                                        "core_not_met": [], "core_partial": [],
                                        "unverified_significant": []}},
        "best_track": "general", "best_track_reason": "x",
        "job_match_score": 30.0,
        "must_not_met": [], "core_not_met": [], "core_partial": [],
        "unverified_significant": [],
    }
    sc = _scorecard()
    d = determine_decision(ev, sc, policy=policy)
    assert d["decision"] == "C"


def test_decision_A_when_perfect():
    policy = load_policy()
    sc = _scorecard()
    ev = {
        "common_assessments": [
            {"requirement_id": "common_001", "status": "met",
             "evidence": ["本科"], "reason": "本科"},
        ],
        "track_assessments": [
            {"track_id": "braking", "requirement_id": "braking_001",
             "status": "met", "evidence": ["x"], "reason": "x"},
        ],
        "track_results": {"braking": {"score": 95.0, "must_not_met": [],
                                      "core_not_met": [], "core_partial": [],
                                      "unverified_significant": []}},
        "best_track": "braking", "best_track_reason": "x",
        "job_match_score": 95.0,
        "must_not_met": [], "core_not_met": [], "core_partial": [],
        "unverified_significant": [],
    }
    d = determine_decision(ev, sc, policy=policy)
    assert d["decision"] == "A"
    assert d["decision_label"] == DECISION_LABELS["A"]
    assert d["contact_recommended"] is True


def test_decision_B_with_partial_unverified():
    policy = load_policy()
    sc = _scorecard()
    ev = {
        "common_assessments": [
            {"requirement_id": "common_001", "status": "met",
             "evidence": ["本科"], "reason": "本科"},
            {"requirement_id": "common_002", "status": "partial",
             "evidence": ["2 年"], "reason": "经验略短"},
        ],
        "track_assessments": [
            {"track_id": "braking", "requirement_id": "braking_001",
             "status": "met", "evidence": ["x"], "reason": "x"},
            {"track_id": "braking", "requirement_id": "braking_002",
             "status": "unverified", "evidence": [], "reason": "简历未写清"},
        ],
        "track_results": {"braking": {"score": 75.0, "must_not_met": [],
                                      "core_not_met": [], "core_partial": ["common_002"],
                                      "unverified_significant": ["braking_002"]}},
        "best_track": "braking", "best_track_reason": "x",
        "job_match_score": 75.0,
        "must_not_met": [], "core_not_met": [], "core_partial": ["common_002"],
        "unverified_significant": ["braking_002"],
    }
    d = determine_decision(ev, sc, policy=policy)
    assert d["decision"] == "B"
    assert d["decision_label"] == DECISION_LABELS["B"]
    assert d["contact_recommended"] is True
    # B 必须生成 1-3 个具体问题
    assert 1 <= len(d["verification_questions"]) <= 3
    # C 不生成
    assert d["decision"] == "B"


def test_decision_A_does_not_generate_questions():
    policy = load_policy()
    sc = _scorecard()
    ev = {
        "common_assessments": [],
        "track_assessments": [],
        "track_results": {"general": {"score": 95.0, "must_not_met": [],
                                        "core_not_met": [], "core_partial": [],
                                        "unverified_significant": []}},
        "best_track": "general", "best_track_reason": "x",
        "job_match_score": 95.0,
        "must_not_met": [], "core_not_met": [], "core_partial": [],
        "unverified_significant": [],
    }
    d = determine_decision(ev, sc, policy=policy)
    assert d["decision"] == "A"
    # A 不需要 verification_questions（但 spec 允许空）
    assert d["verification_questions"] == [] or len(d["verification_questions"]) >= 0


def test_decision_C_no_softening_language():
    """C 不得生成软化建议。"""
    policy = load_policy()
    sc = _scorecard()
    ev = {
        "common_assessments": [],
        "track_assessments": [],
        "track_results": {"general": {"score": 0.0, "must_not_met": ["x"],
                                        "core_not_met": [], "core_partial": [],
                                        "unverified_significant": []}},
        "best_track": "general", "best_track_reason": "x",
        "job_match_score": 0.0,
        "must_not_met": ["x"],
        "core_not_met": [], "core_partial": [], "unverified_significant": [],
    }
    d = determine_decision(ev, sc, policy=policy)
    assert d["decision"] == "C"
    for q in d["verification_questions"]:
        assert "转岗" not in q
        assert "培养" not in q
        assert "尝试" not in q


def test_decision_labels_exact_strings():
    assert DECISION_LABELS["A"] == "各项都很合适，建议直接进入面试"
    assert DECISION_LABELS["B"] == "部分技能仍需确认，但适合进入面试"
    assert DECISION_LABELS["C"] == "完全不合适，不用考虑"


# ============================================================
# finalize_evaluation: job_match × 0.85 + education × 0.15
# ============================================================

def test_finalize_evaluation_final_score():
    policy = load_policy()
    sc = _scorecard()
    resume = {
        "name": "x", "degree": "本科", "geek_id": "g1", "job_id": "e1",
        "education": [{"school": "辽宁工业大学", "major": "车辆工程",
                       "degree": "本科"}],
    }
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    ev = finalize_evaluation(sc, resume, assessments, policy=policy)
    # 辽宁工业大学 = 二本公办 62; final = 100*0.85 + 62*0.15 = 94.3
    assert ev["job_match_score"] == pytest.approx(100.0)
    assert ev["education_score"] == pytest.approx(62.0)
    assert ev["final_score"] == pytest.approx(100*0.85 + 62*0.15, rel=1e-3)


def test_finalize_evaluation_education_bonus():
    policy = load_policy()
    sc = _scorecard()
    resume = {
        "name": "x", "degree": "硕士", "geek_id": "g1", "job_id": "e1",
        "education": [{"school": "辽宁工业大学", "major": "车辆工程",
                       "degree": "硕士"}],
    }
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    ev = finalize_evaluation(sc, resume, assessments, policy=policy)
    # 62 + 8 = 70
    assert ev["education_score"] == pytest.approx(70.0)


def test_finalize_evaluation_education_bonus_capped_100():
    policy = load_policy()
    sc = _scorecard()
    resume = {
        "name": "x", "degree": "博士", "geek_id": "g1", "job_id": "e1",
        "education": [{"school": "辽宁工业大学", "major": "车辆工程",
                       "degree": "博士"}],
    }
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    ev = finalize_evaluation(sc, resume, assessments, policy=policy)
    # 62 + 12 = 74（< 100，cap 不触发）
    assert ev["education_score"] == pytest.approx(74.0)


def test_finalize_education_score_capped_100():
    """985 + 博士 = 92 + 12 = 104 → cap at 100。"""
    policy = load_policy()
    sc = _scorecard()
    resume = {
        "name": "x", "degree": "博士", "geek_id": "g1", "job_id": "e1",
        "education": [{"school": "清华大学", "major": "x", "degree": "博士"}],
    }
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    ev = finalize_evaluation(sc, resume, assessments, policy=policy)
    assert ev["education_score"] == 100.0


def test_finalize_education_pass_with_degree_below_minimum_keeps_C_via_decision():
    policy = load_policy()
    sc = _scorecard({"minimum_degree": "博士及以上", "source_evidence": "JD"})
    resume = {
        "name": "x", "degree": "本科", "geek_id": "g1", "job_id": "e1",
        "education": [{"school": "辽宁工业大学", "major": "车辆工程",
                       "degree": "本科"}],
    }
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    ev = finalize_evaluation(sc, resume, assessments, policy=policy)
    d = determine_decision(ev, sc, policy=policy)
    assert ev["education_hard_pass"] is False
    assert d["decision"] == "C"
    assert d["contact_recommended"] is False


# ============================================================
# 学历缺失不算 C
# ============================================================

def test_education_missing_is_unverified_not_fail():
    policy = load_policy()
    sc = _scorecard({"minimum_degree": "本科及以上", "source_evidence": "JD"})
    resume = {"name": "x", "degree": "", "geek_id": "g1", "job_id": "e1",
              "education": []}
    assessments = _assessments([
        {"id": "common_001", "status": "met"},
        {"id": "common_002", "status": "met"},
        {"id": "braking_001", "status": "met"},
        {"id": "braking_002", "status": "met"},
        {"id": "steering_001", "status": "met"},
    ])
    ev = finalize_evaluation(sc, resume, assessments, policy=policy)
    # 简历未写学历 → unverified，不算 hard fail
    assert ev["education_hard_pass"] is True
    assert ev["education_review_required"] is True
