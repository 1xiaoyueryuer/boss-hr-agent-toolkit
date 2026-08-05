# -*- coding: utf-8 -*-
"""HTML 报告 v2 schema 测试。"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import pytest

_GEN_PATH = r"C:\Users\yuyu\Desktop\boss-hr-agent-toolkit\html-report\scripts\generate_html_report.py"
import importlib.util
_gen_spec = importlib.util.spec_from_file_location(
    "generate_html_report", _GEN_PATH,
)
gen = importlib.util.module_from_spec(_gen_spec)
_gen_spec.loader.exec_module(gen)


def _scorecard_dict():
    return {
        "schema_version": "2.0",
        "policy_version": "abc-education-v1",
        "policy_hash": "x",
        "source_jd_hash": "x",
        "job_name": "线控底盘",
        "common_requirements": [
            {"id": "common_001", "description": "本科及以上",
             "importance": "must", "source_evidence": "JD"},
        ],
        "alternative_tracks": [
            {"id": "braking", "name": "线控制动",
             "requirements": [
                 {"id": "braking_001", "description": "本体方案",
                  "importance": "must", "source_evidence": "JD"},
             ]},
            {"id": "steering", "name": "线控转向",
             "requirements": [
                 {"id": "steering_001", "description": "本体方案",
                  "importance": "must", "source_evidence": "JD"},
             ]},
        ],
        "education_requirements": {"minimum_degree": "本科及以上",
                                     "source_evidence": "JD"},
        "generated_at": "2026-08-05 12:00:00",
    }


def _candidate(decision, name="x", final=80.0, jm=85.0, edu=70.0,
                best_track="braking", recommended=True):
    return {
        "rank": 1, "name": name, "geek_id": "g1",
        "decision": decision,
        "decision_label": f"label-{decision}",
        "decision_reasons": ["r1"],
        "job_match_score": jm, "education_score": edu,
        "education_tier": "一本公办", "education_reason": "x",
        "education_review_required": False,
        "education_hard_pass": True,
        "final_score": final,
        "best_track": best_track,
        "highlights": ["亮点"], "concerns": [],
        "verification_questions": [],
        "contact_recommended": recommended,
        "common_assessments": [], "track_assessments": [],
    }


def test_render_v2_dispatches_to_v2():
    data = {"schema_version": "2.0", "job_name": "x", "summary": {},
            "scorecard": _scorecard_dict(), "candidates": []}
    out = gen.render(data)
    assert "v2 协议" in out
    assert "A/B/C 面试结论" in out


def test_render_v2_groups_a_b_c():
    data = {
        "schema_version": "2.0", "job_name": "x", "run_id": "rid",
        "summary": {"total": 3, "A": 1, "B": 1, "C": 1, "contact_recommended": 2},
        "scorecard": _scorecard_dict(),
        "candidates": [
            _candidate("A", name="AAA", jm=90, final=89),
            _candidate("B", name="BBB", jm=75, final=72),
            _candidate("C", name="CCC", jm=30, final=40, recommended=False),
        ],
    }
    out = gen.render(data)
    # 三组标题都出现
    assert "✅ A：各项都很合适，建议直接进入面试" in out
    assert "📌 B：部分技能仍需确认，但适合进入面试" in out
    assert "❌ C：完全不合适，不用考虑" in out
    # 候选人姓名出现在卡片里
    assert "AAA" in out and "BBB" in out and "CCC" in out


def test_render_v2_shows_scorecard_summary():
    data = {
        "schema_version": "2.0", "job_name": "x",
        "summary": {"total": 0, "A": 0, "B": 0, "C": 0, "contact_recommended": 0},
        "scorecard": _scorecard_dict(),
        "candidates": [],
    }
    out = gen.render(data)
    assert "📋 岗位评分卡" in out
    assert "common_001" in out
    assert "braking_001" in out
    assert "本科及以上" in out


def test_render_v2_shows_verification_questions():
    data = {
        "schema_version": "2.0", "job_name": "x",
        "summary": {"total": 1, "A": 0, "B": 1, "C": 0, "contact_recommended": 1},
        "scorecard": _scorecard_dict(),
        "candidates": [
            {**_candidate("B", name="BB"),
             "verification_questions": ["是否做过 X？", "经验几年？"]},
        ],
    }
    out = gen.render(data)
    assert "面试确认问题" in out
    assert "是否做过 X？" in out
    assert "经验几年？" in out


def test_render_v2_shows_contact_recommended():
    data = {
        "schema_version": "2.0", "job_name": "x",
        "summary": {"total": 2, "A": 1, "B": 0, "C": 1, "contact_recommended": 1},
        "scorecard": _scorecard_dict(),
        "candidates": [
            _candidate("A", name="AA", recommended=True),
            _candidate("C", name="CC", recommended=False),
        ],
    }
    out = gen.render(data)
    assert "建议招呼" in out
    assert "不建议招呼" in out


def test_render_v2_does_not_use_legacy_threshold_labels():
    """v2 报告不应出现 v1 的"推荐 ≥70"等阈值标签。"""
    data = {
        "schema_version": "2.0", "job_name": "x",
        "summary": {"total": 1, "A": 1, "B": 0, "C": 0, "contact_recommended": 1},
        "scorecard": _scorecard_dict(),
        "candidates": [_candidate("A", name="AA")],
    }
    out = gen.render(data)
    assert "≥70" not in out
    assert "60-69" not in out
    assert "&lt;60" not in out


def test_render_v2_decision_reasons_visible():
    data = {
        "schema_version": "2.0", "job_name": "x",
        "summary": {"total": 1, "A": 0, "B": 0, "C": 1, "contact_recommended": 0},
        "scorecard": _scorecard_dict(),
        "candidates": [
            {**_candidate("C", name="CC", recommended=False),
             "decision_reasons": ["存在 must=not_met: braking_001", "job_match=20"]},
        ],
    }
    out = gen.render(data)
    assert "判定理由" in out
    assert "must=not_met" in out


def test_render_v1_still_works():
    data = {
        "job_name": "x",
        "meta": {"title": "T", "subtitle": "S", "job": {}},
        "summary": {"total": 1, "recommend": 0, "pending": 0, "reject": 0},
        "dimension_labels": ["学历", "工作经验", "专业技能", "项目经历", "专业匹配"],
        "candidates": [],
        "actions": {},
    }
    out = gen.render(data)
    # v1 报告含 "✅ 推荐面试" 等旧标签
    assert "✅ 推荐面试 (≥70)" in out
