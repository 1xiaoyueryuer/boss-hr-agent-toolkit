# -*- coding: utf-8 -*-
"""boss_hr.scoring.evaluator — 候选人证据评估 + Python 确定性评分（v2）。

职责：
  1. 用 LLM 把每位候选人对每个 requirement 映射到 met / partial /
     unverified / not_met 之一 + 证据 + 理由。
  2. Python 算 job_match_score（按 track，分别算分）。
  3. Python 选 best_track：得分最高且无 must 硬伤。
  4. 学历（school_tier + degree_bonus + unknown fallback）。
  5. final_score = job_match × 0.85 + education × 0.15。

LLM 输入只包含 1 位候选人 + 当前 scorecard，**无状态**。所有权重 /
status → 数值映射都来自 policy。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from .policy import (
    load_policy, policy_hash, status_value, importance_weight,
)
from .llm_client import (
    LLMClientError, build_messages, complete, load_config_from_env,
    extract_json, atomic_write_json,
)
from .scorecard import all_requirement_ids


# ============================================================
# 候选人 prompt
# ============================================================

EVALUATOR_SYSTEM_PROMPT = """你是 boss-hr 评分系统的候选人证据评估器。

任务：把"岗位 requirement"与"候选人简历"做**纯证据映射**。

要求：
  1. **不**输出 0-100 自由分；**不**替候选人做 A/B/C 决定。
  2. 对 scorecard 里的**每一条** requirement 输出一个 assessment：
       - requirement_id：与 scorecard 完全一致。
       - status：`met` / `partial` / `unverified` / `not_met` 之一。
       - evidence：简历中支持 / 反驳的**具体事实**（公司 / 岗位 / 项目 / 职责 / 技能 / 时间段）。
         若完全无相关信息，可写空 list，但**不得**编造。
       - reason：为什么给这个 status（一两句话）。禁止套话，禁止"建议考虑转岗"等软化话术。
  3. **严格语义**：
       - met：简历有**直接、具体、可核对**的经历证据能证明具备。
         仅有"参与 / 协同 / 了解"或关键词命中**不能**算 met。
       - partial：覆盖了要求的一部分，但深度 / 范围 / 角色 / 成果不足。
       - unverified：简历**没有写清楚**——既不能证明具备也不能断言不会。
         遵循"没写 ≠ 不会"；这通常意味着 B 类候选人的面试问题。
       - not_met：现有经历**明确**显示方向不匹配 / 能力不符。
  4. **禁止**：
       - 用公司名 / 学校名替代能力证据；
       - 用岗位名关键词直接判定 met；
       - 复制粘贴相同 reason 给不同 requirement；
       - 输出"建议考虑转岗 / 可以培养 / 可以尝试"等脱离当前岗位的软化建议；
       - 编造简历中不存在的事实。
  5. 对每条 requirement 都必须评估，**一个都不能少**；每个 requirement_id
     **只出现一次**；不得有未知 ID。
  6. 输出 JSON 顶层 `assessments` list，**只**输出这一个字段。
     用 ```json``` 包裹或直接输出。
"""


def build_evaluator_user_prompt(
    scorecard: Dict[str, Any],
    resume: Dict[str, Any],
) -> str:
    sc = {
        "job_name": scorecard.get("job_name"),
        "common_requirements": scorecard.get("common_requirements", []),
        "alternative_tracks": [
            {"id": t.get("id"), "name": t.get("name"),
             "requirements": t.get("requirements", [])}
            for t in scorecard.get("alternative_tracks", [])
        ],
    }
    return (
        "请评估下面这位候选人对 scorecard 中每条 requirement 的 status。\n\n"
        "### 当前岗位 scorecard\n```json\n"
        + json.dumps(sc, ensure_ascii=False, indent=2)
        + "\n```\n\n"
        "### 候选人简历\n```json\n"
        + json.dumps(resume, ensure_ascii=False, indent=2)
        + "\n```\n\n"
        "输出 schema：\n"
        "{\n"
        '  "assessments": [\n'
        '    {\n'
        '      "requirement_id": "<id>",\n'
        '      "status": "met|partial|unverified|not_met",\n'
        '      "evidence": ["..."],\n'
        '      "reason": "..."\n'
        '    }, ...\n'
        "  ]\n"
        "}\n"
    )


# ============================================================
# 评估校验
# ============================================================

ALLOWED_STATUSES = ("met", "partial", "unverified", "not_met")


class EvaluationError(Exception):
    """evaluator 校验失败。"""


def validate_assessments(
    parsed: Dict[str, Any],
    scorecard: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """校验模型输出 + 返回规范化 assessments list。

    校验：
      - 顶层有 `assessments` list；
      - 每个 assessment 必含 requirement_id / status / evidence / reason；
      - status 在 ALLOWED_STATUSES；
      - requirement_id 都在 scorecard 中（不允许未知 / 重复 / 缺失）；
      - evidence / reason 类型正确。
    """
    if not isinstance(parsed, dict):
        raise EvaluationError("LLM 输出不是 dict")
    assessments = parsed.get("assessments")
    if not isinstance(assessments, list):
        raise EvaluationError("assessments 必须是 list")
    allowed = set(all_requirement_ids(scorecard))
    seen: set[str] = set()
    out: List[Dict[str, Any]] = []
    for i, a in enumerate(assessments):
        if not isinstance(a, dict):
            raise EvaluationError(f"assessments[{i}] 不是 dict")
        rid = a.get("requirement_id")
        if not isinstance(rid, str) or not rid:
            raise EvaluationError(f"assessments[{i}].requirement_id 缺失")
        if rid not in allowed:
            raise EvaluationError(f"assessments[{i}].requirement_id={rid!r} 不在 scorecard")
        if rid in seen:
            raise EvaluationError(f"assessments[{i}].requirement_id={rid!r} 重复")
        seen.add(rid)
        status = a.get("status")
        if status not in ALLOWED_STATUSES:
            raise EvaluationError(
                f"assessments[{i}] status={status!r} 不在 {ALLOWED_STATUSES}"
            )
        evidence = a.get("evidence")
        if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
            raise EvaluationError(f"assessments[{i}].evidence 必须是 str list")
        reason = a.get("reason")
        if not isinstance(reason, str):
            raise EvaluationError(f"assessments[{i}].reason 必须是 str")
        out.append({
            "requirement_id": rid,
            "status": status,
            "evidence": evidence,
            "reason": reason,
        })
    missing = allowed - seen
    if missing:
        raise EvaluationError(f"缺少 requirement 评估：{sorted(missing)}")
    return out


# ============================================================
# Job match score
# ============================================================

def _requirement_lookup(scorecard: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """requirement_id → {importance, scope, ...}。

    scorecard 中 `common_requirements[i].id` 已经是最终 ID（e.g. common_001）。
    `alternative_tracks[j].requirements[k].id` 已是最终 ID（e.g. braking_001），
    不要再加 track_id 前缀。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for req in scorecard.get("common_requirements", []):
        out[req["id"]] = {
            "importance": req["importance"],
            "description": req.get("description", ""),
            "source_evidence": req.get("source_evidence", ""),
            "scope": "common",
        }
    for t in scorecard.get("alternative_tracks", []):
        for req in t.get("requirements", []):
            out[req["id"]] = {
                "importance": req["importance"],
                "description": req.get("description", ""),
                "source_evidence": req.get("source_evidence", ""),
                "scope": "track",
                "track_id": t["id"],
            }
    return out


def compute_track_score(
    scorecard: Dict[str, Any],
    assessments: List[Dict[str, Any]],
    *,
    track_id: Optional[str],
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """对一条 track 算分。

    Args:
        track_id:  要算的 track；None 表示只算 common_requirements。

    Returns:
        {
            "score": float,         # 0-100
            "weighted_sum": float,
            "weight_sum": float,
            "must_not_met": [requirement_id, ...],
            "core_not_met": [requirement_id, ...],
            "core_partial": [...],
            "unverified_significant": [...],  # must/core 中 unverified
            "items": [{requirement_id, status, importance, weight, score}]
        }
    """
    lookup = _requirement_lookup(scorecard)
    ass_map = {a["requirement_id"]: a for a in assessments}

    items: List[Dict[str, Any]] = []
    weighted_sum = 0.0
    weight_sum = 0.0
    must_not_met: List[str] = []
    core_not_met: List[str] = []
    core_partial: List[str] = []
    unverified_significant: List[str] = []

    for rid, meta in lookup.items():
        if meta["scope"] == "track" and meta["track_id"] != track_id:
            continue
        a = ass_map.get(rid)
        if a is None:
            continue
        w = importance_weight(policy, meta["importance"])
        s = status_value(policy, a["status"])
        weighted_sum += w * s
        weight_sum += w
        items.append({
            "requirement_id": rid,
            "status": a["status"],
            "importance": meta["importance"],
            "weight": w,
            "score": s,
        })
        if meta["importance"] == "must" and a["status"] == "not_met":
            must_not_met.append(rid)
        if meta["importance"] == "core":
            if a["status"] == "not_met":
                core_not_met.append(rid)
            elif a["status"] == "partial":
                core_partial.append(rid)
        if meta["importance"] in ("must", "core") and a["status"] == "unverified":
            unverified_significant.append(rid)

    score = (weighted_sum / weight_sum * 100.0) if weight_sum > 0 else 0.0
    return {
        "score": round(score, 2),
        "weighted_sum": weighted_sum,
        "weight_sum": weight_sum,
        "must_not_met": must_not_met,
        "core_not_met": core_not_met,
        "core_partial": core_partial,
        "unverified_significant": unverified_significant,
        "items": items,
    }


def best_track(
    scorecard: Dict[str, Any],
    assessments: List[Dict[str, Any]],
    *,
    policy: Dict[str, Any],
) -> Tuple[Optional[str], Dict[str, Dict[str, Any]], str]:
    """选 best_track：

      1. 必须不存在 must=not_met；
      2. score 最高；
      3. 同分时按 track id 字典序稳定排序。

    Returns (best_track_id, all_track_results, reason)。
    """
    tracks = scorecard.get("alternative_tracks", [])
    results: Dict[str, Dict[str, Any]] = {}
    for t in tracks:
        results[t["id"]] = compute_track_score(
            scorecard, assessments, track_id=t["id"], policy=policy,
        )
    # 候选：must_not_met 为空的所有 track
    candidates = [
        (tid, r) for tid, r in results.items() if not r["must_not_met"]
    ]
    if not candidates:
        # 所有 track 都有 must 硬伤 → 选 must_not_met 最少的
        sorted_all = sorted(
            results.items(),
            key=lambda kv: (len(kv[1]["must_not_met"]), -kv[1]["score"], kv[0]),
        )
        chosen_id, chosen = sorted_all[0]
        reason = (
            f"所有方向均存在 must 硬伤；按 must_not_met 最少且分数最高选择 "
            f"{chosen_id}（must_not_met={len(chosen['must_not_met'])}）"
        )
        return chosen_id, results, reason
    # 无硬伤时取最高分
    candidates.sort(key=lambda kv: (-kv[1]["score"], kv[0]))
    chosen_id, chosen = candidates[0]
    reason = (
        f"按 job_match_score 最高且无 must 硬伤选择 {chosen_id}（score={chosen['score']}）"
    )
    return chosen_id, results, reason


# ============================================================
# 学历分
# ============================================================

def _school_tier_lookup(school_name: str) -> Dict[str, Any]:
    """调 resume-screener.school_tier.lookup；缺失返回 {"score": None, "tier": None}。"""
    try:
        # school_tier 在 resume-screener/scripts/，不在标准 sys.path
        import importlib.util
        import os as _os
        _school_tier_path = _os.path.join(
            _TOOLKIT_ROOT, "resume-screener", "scripts", "school_tier.py"
        )
        if not _os.path.isfile(_school_tier_path):
            return {"score": None, "tier": None, "matched": ""}
        _spec = importlib.util.spec_from_file_location(
            "resume_screener_school_tier", _school_tier_path
        )
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _lookup = _mod.lookup
    except Exception:
        return {"score": None, "tier": None, "matched": ""}
    try:
        info = _lookup(school_name or "")
    except Exception:
        return {"score": None, "tier": None, "matched": ""}
    if not isinstance(info, dict):
        return {"score": None, "tier": None, "matched": ""}
    return {
        "score": info.get("score"),
        "tier": info.get("tier"),
        "matched": info.get("matched") or school_name or "",
    }


def _extract_school_name_from_resume(resume: Dict[str, Any]) -> str:
    """从候选人简历的 education[] 里挑第一个非空校名。"""
    edu = resume.get("education") or []
    if not isinstance(edu, list):
        return ""
    for e in edu:
        if isinstance(e, dict):
            s = (e.get("school") or "").strip()
            if s:
                return s
    return ""


def _extract_degree(resume: Dict[str, Any]) -> str:
    return str(resume.get("degree") or "").strip()


def compute_education(
    resume: Dict[str, Any],
    *,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """算 education_score + 学历硬门槛。

    返回：
      {
        "education_score": 0-100,
        "education_tier": "C9/985/211/双一流/一本公办/二本公办/民办/<unmatched>/<unknown>",
        "education_reason": "...",
        "education_review_required": bool,
        "education_hard_pass": bool,
        "school_matched": str,
        "degree": str,
      }

    学历硬门槛由 scorecard.education_requirements.minimum_degree 决定。
    """
    school = _extract_school_name_from_resume(resume)
    degree = _extract_degree(resume)
    info = _school_tier_lookup(school)

    score: float
    tier: str
    reason: str
    review_required = False

    if info["score"] is None:
        score = float(policy["unknown_school_score"])
        if school:
            tier = "未收录"
            reason = f"学校「{school}」未收录，按中性分 {score} 计；需人工确认"
        else:
            tier = "未知"
            reason = "简历未写学校，按中性分计；需人工确认"
        review_required = True
    else:
        score = float(info["score"])
        tier = str(info["tier"] or "未分档")
        reason = f"{tier}（school_tier 查询：{info['matched']}）"

    # 层次加分（硕士 / 博士）；封顶 100
    bonus = policy.get("degree_bonus", {}) or {}
    bonus_val = 0
    if degree:
        for k, v in bonus.items():
            if isinstance(v, (int, float)) and k in degree:
                bonus_val = max(bonus_val, int(v))
    if bonus_val:
        score = min(100.0, score + bonus_val)
        reason += f" + {degree} 加 {bonus_val}"

    # 学历硬门槛：从 scorecard.education_requirements 抽 minimum_degree
    # 调用方负责传入 scorecard；这里仅算"是否有学校名 / 是否有 degree"
    has_school = bool(school)
    has_degree = bool(degree)
    if not has_school and not has_degree:
        hard_pass = True  # unverified: 不直接判 fail
    else:
        hard_pass = True  # 默认通过；硬门槛判定在 decision.py 配 scorecard

    return {
        "education_score": round(score, 2),
        "education_tier": tier,
        "education_reason": reason,
        "education_review_required": review_required,
        "education_hard_pass": hard_pass,
        "school_matched": info["matched"] or "",
        "degree": degree,
        "has_school": has_school,
        "has_degree": has_degree,
    }


def _degree_level(deg: str) -> int:
    """中文层次字符串 → 数字级别（越高越强）。未知返回 -1。"""
    if "博士" in deg:
        return 3
    if "硕士" in deg:
        return 2
    if "本科" in deg or "学士" in deg:
        return 1
    if "专科" in deg or "大专" in deg:
        return 0
    return -1


def _required_level(req: str) -> int:
    """'博士及以上'/'硕士及以上'/'本科及以上' → 数字级别。"""
    if "博士" in req:
        return 3
    if "硕士" in req:
        return 2
    if "本科" in req or "学士" in req:
        return 1
    if "专科" in req or "大专" in req:
        return 0
    return -1


def education_hard_fail(
    edu: Dict[str, Any], scorecard: Dict[str, Any]
) -> Tuple[bool, str]:
    """根据 scorecard.education_requirements 判断是否学历硬门槛失败。

    规则：
      - scorecard 没要求最低学历 → 永不算 hard fail。
      - JD 写"X 及以上"：候选人 degree level >= X level → pass。
      - 候选人未写明 degree → unverified（不算 fail）。
    """
    req = (scorecard.get("education_requirements") or {}).get("minimum_degree") \
        or ""
    if not req:
        return False, "JD 未规定最低学历"
    req = str(req).strip()

    deg = edu.get("degree", "")
    if not deg:
        return False, f"JD 要求 {req}；简历未写学历（unverified）"

    req_level = _required_level(req)
    deg_level = _degree_level(deg)
    if req_level < 0 or deg_level < 0:
        return False, f"JD 要求 {req}；简历层次无法识别（unverified）"

    if deg_level >= req_level:
        return False, f"JD 要求 {req}；候选人 {deg}（满足）"
    return True, f"JD 要求 {req}；候选人 {deg}（低于要求）"


# ============================================================
# LLM 评估 + Python 确定性合成
# ============================================================

def evaluate_candidate(
    scorecard: Dict[str, Any],
    resume: Dict[str, Any],
    *,
    policy: Dict[str, Any],
    config=None,
    sleep_fn=None,
    http_post=None,
) -> Dict[str, Any]:
    """调 LLM 评估 + Python 合成最终分数结构。返回 dict（不写盘）。

    抛出 LLMClientError / EvaluationError。
    """
    if config is None:
        config = load_config_from_env()
    msgs = build_messages(
        EVALUATOR_SYSTEM_PROMPT,
        build_evaluator_user_prompt(scorecard, resume),
    )
    kwargs = {}
    if sleep_fn is not None:
        kwargs["sleep_fn"] = sleep_fn
    if http_post is not None:
        kwargs["http_post"] = http_post
    raw = complete(msgs, config, **kwargs)
    parsed = extract_json(raw)
    assessments = validate_assessments(parsed, scorecard)
    return finalize_evaluation(scorecard, resume, assessments, policy=policy)


def finalize_evaluation(
    scorecard: Dict[str, Any],
    resume: Dict[str, Any],
    assessments: List[Dict[str, Any]],
    *,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """Python 确定性合成：选 best_track / 算分 / 算学历 / final_score。

    不再让 LLM 决定 best_track（v2 规范）。
    """
    chosen_id, all_track_results, reason = best_track(
        scorecard, assessments, policy=policy
    )
    chosen = all_track_results[chosen_id] if chosen_id else {
        "score": 0.0, "must_not_met": [], "core_not_met": [],
        "core_partial": [], "unverified_significant": [], "items": [],
    }
    edu = compute_education(resume, policy=policy)
    hard_fail, hard_reason = education_hard_fail(edu, scorecard)
    edu["education_hard_pass"] = not hard_fail
    edu["education_hard_reason"] = hard_reason

    jm = float(policy["job_match_weight"])
    ew = float(policy["education_weight"])
    final = chosen["score"] * jm + edu["education_score"] * ew

    lookup = _requirement_lookup(scorecard)
    common_assessments: List[Dict[str, Any]] = []
    track_assessments: List[Dict[str, Any]] = []
    for a in assessments:
        rid = a["requirement_id"]
        meta = lookup.get(rid)
        if meta is None:
            continue
        entry = {
            "requirement_id": rid,
            "status": a["status"],
            "evidence": a["evidence"],
            "reason": a["reason"],
        }
        if meta["scope"] == "common":
            common_assessments.append(entry)
        elif meta["scope"] == "track":
            entry_with_track = dict(entry)
            entry_with_track["track_id"] = meta["track_id"]
            track_assessments.append(entry_with_track)

    return {
        "common_assessments": common_assessments,
        "track_assessments": track_assessments,
        "track_results": all_track_results,
        "best_track": chosen_id,
        "best_track_reason": reason,
        "job_match_score": round(chosen["score"], 2),
        "education_score": edu["education_score"],
        "education_tier": edu["education_tier"],
        "education_reason": edu["education_reason"],
        "education_review_required": edu["education_review_required"],
        "education_hard_pass": edu["education_hard_pass"],
        "education_hard_reason": hard_reason,
        "final_score": round(final, 2),
        "must_not_met": chosen["must_not_met"],
        "core_not_met": chosen["core_not_met"],
        "core_partial": chosen["core_partial"],
        "unverified_significant": chosen["unverified_significant"],
    }


# ============================================================
# candidate output 写盘
# ============================================================

def write_candidate_output(
    scorecard: Dict[str, Any],
    resume: Dict[str, Any],
    evaluation: Dict[str, Any],
    *,
    policy: Dict[str, Any],
    output_path: str,
) -> Dict[str, Any]:
    """把 Python 合成结果包装成 candidate_<geek_id>.json schema v2 并原子写盘。

    返回写入的 dict。
    """
    candidate_id = str(
        resume.get("geek_id") or resume.get("encrypt_geek_id") or ""
    )
    job_id = str(
        resume.get("job_id") or resume.get("encrypt_job_id") or ""
    )
    payload = {
        "schema_version": "2.0",
        "policy_version": policy["policy_version"],
        "policy_hash": policy_hash(policy),
        "job_scorecard_hash": scorecard.get("source_jd_hash", ""),
        "name": resume.get("name", ""),
        "geek_id": candidate_id,
        "job_id": job_id,
        "common_assessments": evaluation["common_assessments"],
        "track_assessments": evaluation["track_assessments"],
        "track_results": {
            tid: {
                "score": r["score"],
                "must_not_met": r["must_not_met"],
                "core_not_met": r["core_not_met"],
                "core_partial": r["core_partial"],
                "unverified_significant": r["unverified_significant"],
            }
            for tid, r in evaluation["track_results"].items()
        },
        "best_track": evaluation["best_track"],
        "best_track_reason": evaluation["best_track_reason"],
        "job_match_score": evaluation["job_match_score"],
        "education_score": evaluation["education_score"],
        "education_tier": evaluation["education_tier"],
        "education_reason": evaluation["education_reason"],
        "education_review_required": evaluation["education_review_required"],
        "education_hard_pass": evaluation["education_hard_pass"],
        "education_hard_reason": evaluation.get("education_hard_reason", ""),
        "final_score": evaluation["final_score"],
        "decision": None,         # 由 decision.py 决定（覆盖 LLM 任何 free decision）
        "decision_label": None,
        "decision_reasons": [],
        "highlights": [],
        "concerns": [],
        "verification_questions": [],
        "contact_recommended": None,
        "must_not_met": evaluation["must_not_met"],
        "core_not_met": evaluation["core_not_met"],
        "core_partial": evaluation["core_partial"],
        "unverified_significant": evaluation["unverified_significant"],
    }
    atomic_write_json(output_path, payload)
    return payload


# ============================================================
# v2 output 校验（读盘时用）
# ============================================================

def validate_candidate_output(
    payload: Dict[str, Any],
    *,
    scorecard: Dict[str, Any],
    expected_policy_hash: str,
    expected_geek_id: str,
    expected_job_id: str,
) -> List[str]:
    """返回错误列表；空列表表示通过。"""
    errs: List[str] = []
    if not isinstance(payload, dict):
        return ["payload 不是 dict"]
    if payload.get("schema_version") != "2.0":
        errs.append("schema_version 必须为 2.0")
    if payload.get("policy_version") != scorecard.get("policy_version"):
        errs.append("policy_version 与 scorecard 不一致")
    if payload.get("policy_hash") != expected_policy_hash:
        errs.append("policy_hash 与当前策略不一致")
    if payload.get("job_scorecard_hash") != scorecard.get("source_jd_hash"):
        errs.append("job_scorecard_hash 与当前 scorecard 不一致")
    if payload.get("geek_id") != expected_geek_id:
        errs.append(f"geek_id={payload.get('geek_id')!r} 与 manifest {expected_geek_id!r} 不一致")
    if payload.get("job_id") != expected_job_id:
        errs.append(f"job_id={payload.get('job_id')!r} 与当前 {expected_job_id!r} 不一致")

    for k in (
        "common_assessments", "track_assessments", "best_track",
        "best_track_reason", "job_match_score", "education_score",
        "education_tier", "education_reason", "education_review_required",
        "education_hard_pass", "final_score",
        "decision", "decision_label", "decision_reasons",
        "highlights", "concerns", "verification_questions",
        "contact_recommended",
    ):
        if k not in payload:
            errs.append(f"缺字段 {k}")

    allowed = set(all_requirement_ids(scorecard))
    for i, a in enumerate(payload.get("common_assessments", []) or []):
        if not isinstance(a, dict):
            errs.append(f"common_assessments[{i}] 不是 dict"); continue
        rid = a.get("requirement_id")
        if rid not in allowed:
            errs.append(f"common_assessments[{i}].requirement_id={rid!r} 未知")
        if a.get("status") not in ALLOWED_STATUSES:
            errs.append(f"common_assessments[{i}].status 非法")
    seen_track_ass: set[str] = set()
    for i, a in enumerate(payload.get("track_assessments", []) or []):
        if not isinstance(a, dict):
            errs.append(f"track_assessments[{i}] 不是 dict"); continue
        rid = a.get("requirement_id")
        if rid not in allowed:
            errs.append(f"track_assessments[{i}].requirement_id={rid!r} 未知")
        if rid in seen_track_ass:
            errs.append(f"track_assessments[{i}].requirement_id={rid!r} 重复")
        seen_track_ass.add(rid)
        if a.get("status") not in ALLOWED_STATUSES:
            errs.append(f"track_assessments[{i}].status 非法")

    if not isinstance(payload.get("decision_reasons"), list):
        errs.append("decision_reasons 必须是 list")
    if not isinstance(payload.get("highlights"), list):
        errs.append("highlights 必须是 list")
    if not isinstance(payload.get("concerns"), list):
        errs.append("concerns 必须是 list")
    if not isinstance(payload.get("verification_questions"), list):
        errs.append("verification_questions 必须是 list")

    return errs


__all__ = [
    "EVALUATOR_SYSTEM_PROMPT",
    "build_evaluator_user_prompt",
    "ALLOWED_STATUSES",
    "EvaluationError",
    "validate_assessments",
    "compute_track_score",
    "best_track",
    "compute_education",
    "education_hard_fail",
    "evaluate_candidate",
    "finalize_evaluation",
    "write_candidate_output",
    "validate_candidate_output",
]
