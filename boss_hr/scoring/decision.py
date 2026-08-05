# -*- coding: utf-8 -*-
"""boss_hr.scoring.decision — A/B/C 确定性判定（v2）。

固定文案（三条中文完全正确）：
  A：各项都很合适，建议直接进入面试
  B：部分技能仍需确认，但适合进入面试
  C：完全不合适，不用考虑

C 不得生成软化建议。
B 必须生成 1-3 个具体 verification_questions。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


DECISION_LABELS = {
    "A": "各项都很合适，建议直接进入面试",
    "B": "部分技能仍需确认，但适合进入面试",
    "C": "完全不合适，不用考虑",
}


def _build_highlights(
    evaluation: Dict[str, Any],
    assessments_by_id: Dict[str, Dict[str, Any]],
) -> List[str]:
    """挑 met 的 evidence 作为亮点；最多 3 条。"""
    items: List[str] = []
    for a in assessments_by_id.values():
        if a.get("status") == "met":
            for ev in a.get("evidence", []) or []:
                ev = (ev or "").strip()
                if ev and ev not in items:
                    items.append(ev)
                    if len(items) >= 3:
                        return items
    return items


def _build_concerns(
    evaluation: Dict[str, Any],
    assessments_by_id: Dict[str, Dict[str, Any]],
) -> List[str]:
    """挑 partial / unverified 的 reason 作为顾虑；最多 3 条。"""
    items: List[str] = []
    for a in assessments_by_id.values():
        if a.get("status") in ("partial", "unverified"):
            r = (a.get("reason") or "").strip()
            if r and r not in items:
                items.append(r)
                if len(items) >= 3:
                    return items
    return items


def _build_verification_questions(
    evaluation: Dict[str, Any],
    assessments_by_id: Dict[str, Dict[str, Any]],
    scorecard: Dict[str, Any],
) -> List[str]:
    """为 partial / unverified 的 requirement 生成具体确认问题。

    来自 partial / unverified 的 reason；最多 3 条；不生成通用套话。
    """
    items: List[str] = []
    for rid, a in assessments_by_id.items():
        if a.get("status") not in ("partial", "unverified"):
            continue
        reason = (a.get("reason") or "").strip()
        if not reason:
            continue
        # 拼出"该 requirement 是否满足 / 经验深度"问题
        req_desc = ""
        reqs = scorecard.get("common_requirements", []) or []
        for r in reqs:
            if r.get("id") == rid:
                req_desc = r.get("description", "")
                break
        if not req_desc:
            for t in scorecard.get("alternative_tracks", []):
                for r in t.get("requirements", []):
                    if f"{t['id']}_{r['id']}" == rid:
                        req_desc = r.get("description", "")
                        break
                if req_desc:
                    break
        q = f"关于 {req_desc or rid}：{reason}"
        if q not in items:
            items.append(q)
            if len(items) >= 3:
                return items
    return items


def determine_decision(
    evaluation: Dict[str, Any],
    scorecard: Dict[str, Any],
    *,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """A/B/C 确定性判定。

    Args:
        evaluation: evaluator.finalize_evaluation() 的输出
        scorecard:  当前 run 的 scorecard dict
        policy:     策略 dict

    Returns dict，覆盖 candidate output 的 decision/label/reasons/contact。
    """
    must_not_met = list(evaluation.get("must_not_met") or [])
    job_match = float(evaluation.get("job_match_score") or 0.0)
    edu_hard_pass = bool(
        evaluation.get("education_hard_pass", True)
    )
    best_track = evaluation.get("best_track")

    # 组装 assessments_by_id（方便生成 highlights/concerns/questions）
    by_id: Dict[str, Dict[str, Any]] = {}
    for a in evaluation.get("common_assessments", []) or []:
        by_id[a["requirement_id"]] = a
    for a in evaluation.get("track_assessments", []) or []:
        by_id[a["requirement_id"]] = a

    reasons: List[str] = []

    if must_not_met:
        reasons.append(
            f"存在 must=not_met：{', '.join(must_not_met)}"
        )
        decision = "C"
    elif not best_track:
        reasons.append("无合理匹配方向")
        decision = "C"
    elif not edu_hard_pass:
        reasons.append("学历硬门槛失败")
        decision = "C"
    elif job_match < 60.0:
        reasons.append(f"job_match_score={job_match} < 60")
        decision = "C"
    elif job_match >= 85.0 and not must_not_met and edu_hard_pass:
        # 进一步要求 A：所有 must/core 都 met；无 significant unverified
        core_not_met = list(evaluation.get("core_not_met") or [])
        core_partial = list(evaluation.get("core_partial") or [])
        unverified_sig = list(evaluation.get("unverified_significant") or [])
        if core_not_met:
            reasons.append(f"存在 core=not_met：{', '.join(core_not_met)}")
            decision = "B"
        elif unverified_sig or core_partial:
            if unverified_sig:
                reasons.append(f"存在 must/core=unverified：{', '.join(unverified_sig)}")
            if core_partial:
                reasons.append(f"存在 core=partial：{', '.join(core_partial)}")
            decision = "B"
        else:
            reasons.append(f"job_match_score={job_match} ≥ 85 且所有 must/core=met")
            decision = "A"
    else:
        # 60 ≤ score < 85 → B
        if job_match < 85.0:
            reasons.append(f"job_match_score={job_match} < 85")
        decision = "B"

    label = DECISION_LABELS[decision]
    highlights = _build_highlights(evaluation, by_id)
    concerns = _build_concerns(evaluation, by_id)
    verification_questions: List[str] = []
    if decision == "B":
        verification_questions = _build_verification_questions(
            evaluation, by_id, scorecard,
        )
    # C 不得生成软化建议
    if decision == "C":
        verification_questions = []
        highlights = []
        concerns = []

    contact_recommended = (decision in ("A", "B"))

    return {
        "decision": decision,
        "decision_label": label,
        "decision_reasons": reasons,
        "highlights": highlights,
        "concerns": concerns,
        "verification_questions": verification_questions,
        "contact_recommended": contact_recommended,
    }


__all__ = [
    "DECISION_LABELS",
    "determine_decision",
]
