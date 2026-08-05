# -*- coding: utf-8 -*-
"""boss_hr.scoring.scorecard — JD 评分卡（v2）。

设计：
  - 每个 run 首次 `score` 时生成一次；写 `process/scoring/job_scorecard.json`。
  - 复用条件：scorecard 文件存在 + schema_version 合法 + policy_hash 匹配
    当前策略 + source_jd_hash 与当前 JD 规范化后一致。
  - 多方向 JD：`common_requirements`（两个方向共同）+ `alternative_tracks`
    （每个方向独立）。LLM 候选评估对所有 track 输出证据；Python 选 best。
  - 单方向 JD：生成一个 `general` track。
  - importance 只允许 must/core/preferred。
  - requirement ID 在 `common_*` 和 `<track_id>_*` 命名空间下必须唯一。

不依赖任何外部 SDK；纯 Python + JSON + 字符串哈希。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from .policy import load_policy, policy_hash
from .llm_client import (
    LLMClientError, build_messages, complete, load_config_from_env,
)


SCORECARD_SCHEMA_VERSION = "2.0"
ALLOWED_IMPORTANCE = ("must", "core", "preferred")
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


# ============================================================
# JD 规范化 + hash
# ============================================================

def normalize_jd(jd: Dict[str, Any]) -> Dict[str, Any]:
    """从 job_detail.json 抽规范化字段，去重排序保证 hash 稳定。"""
    if not isinstance(jd, dict):
        return {}
    out = {
        "jobName": str(jd.get("jobName") or jd.get("name") or "").strip(),
        "encryptJobId": str(jd.get("encryptJobId") or jd.get("job_id") or "").strip(),
        "bodyText": str(jd.get("bodyText") or jd.get("description") or "").strip(),
        "formValues": _normalize_form_values(jd.get("formValues")),
    }
    # 任何其他字段保留（按 key 排序后）
    for k in sorted(jd.keys()):
        if k in out:
            continue
        v = jd[k]
        if isinstance(v, (str, int, float, bool)) or v is None:
            out[k] = v
    return out


def _normalize_form_values(form_values: Any) -> List[Any]:
    """formValues 可能是 list / dict / str；统一为 list（dict 转 items）。"""
    if form_values is None:
        return []
    if isinstance(form_values, list):
        return form_values
    if isinstance(form_values, dict):
        return [dict(sorted(form_values.items()))]
    return [form_values]


def source_jd_hash(jd: Dict[str, Any]) -> str:
    norm = normalize_jd(jd)
    canonical = json.dumps(norm, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ============================================================
# Scorecard 合法性校验
# ============================================================

class ScorecardError(Exception):
    """scorecard 校验失败。"""


def _is_id(s: Any) -> bool:
    return isinstance(s, str) and bool(ID_PATTERN.match(s))


def _check_requirement(req: Dict[str, Any], *, where: str) -> None:
    if not isinstance(req, dict):
        raise ScorecardError(f"{where}：requirement 不是 dict")
    rid = req.get("id")
    if not _is_id(rid):
        raise ScorecardError(f"{where}：requirement.id 非法 {rid!r}")
    if not isinstance(req.get("description"), str) or not req["description"].strip():
        raise ScorecardError(f"{where}：{rid} 缺 description")
    if req.get("importance") not in ALLOWED_IMPORTANCE:
        raise ScorecardError(
            f"{where}：{rid} importance={req.get('importance')!r} 不在 {ALLOWED_IMPORTANCE}"
        )
    src = req.get("source_evidence")
    if not isinstance(src, str) or not src.strip():
        raise ScorecardError(f"{where}：{rid} 缺 source_evidence")


def validate_scorecard(card: Dict[str, Any], *, expected_policy_hash: str) -> None:
    """校验 scorecard JSON 合法 + 与当前策略匹配。"""
    if not isinstance(card, dict):
        raise ScorecardError("scorecard 不是 dict")
    if card.get("schema_version") != SCORECARD_SCHEMA_VERSION:
        raise ScorecardError(
            f"schema_version 期望 {SCORECARD_SCHEMA_VERSION} 实际 {card.get('schema_version')!r}"
        )
    if card.get("policy_version") is None:
        raise ScorecardError("缺 policy_version")
    if card.get("policy_hash") != expected_policy_hash:
        raise ScorecardError(
            f"policy_hash 不匹配（scorecard={card.get('policy_hash')!r} "
            f"current={expected_policy_hash!r}）；旧 scorecard 不得复用"
        )
    common = card.get("common_requirements")
    tracks = card.get("alternative_tracks")
    if not isinstance(common, list):
        raise ScorecardError("common_requirements 必须是 list")
    if not isinstance(tracks, list) or not tracks:
        raise ScorecardError("alternative_tracks 必须是非空 list")

    seen_ids: set[str] = set()
    for i, req in enumerate(common):
        _check_requirement(req, where=f"common[{i}]")
        if req["id"] in seen_ids:
            raise ScorecardError(f"重复 requirement id: {req['id']}")
        seen_ids.add(req["id"])

    track_ids: set[str] = set()
    for ti, t in enumerate(tracks):
        if not isinstance(t, dict):
            raise ScorecardError(f"tracks[{ti}] 不是 dict")
        if not _is_id(t.get("id")):
            raise ScorecardError(f"tracks[{ti}].id 非法 {t.get('id')!r}")
        if t["id"] in track_ids:
            raise ScorecardError(f"tracks[{ti}] 重复 id={t['id']}")
        track_ids.add(t["id"])
        if not isinstance(t.get("name"), str) or not t["name"].strip():
            raise ScorecardError(f"tracks[{ti}] 缺 name")
        reqs = t.get("requirements")
        if not isinstance(reqs, list):
            raise ScorecardError(f"tracks[{ti}].requirements 必须是 list")
        for ri, req in enumerate(reqs):
            _check_requirement(req, where=f"tracks[{t['id']}][{ri}]")
            if req["id"] in seen_ids:
                raise ScorecardError(f"重复 requirement id: {req['id']}")
            seen_ids.add(req["id"])

    if "education_requirements" in card and not isinstance(
        card["education_requirements"], dict
    ):
        raise ScorecardError("education_requirements 必须是 dict")
    if not card.get("source_jd_hash"):
        raise ScorecardError("缺 source_jd_hash")


def all_requirement_ids(card: Dict[str, Any]) -> List[str]:
    """返回所有 requirement ID（最终 ID，与 _requirement_lookup 一致）。

    scorecard 写法约定：track 内的 requirement 已经带 track_id 前缀
    （如 braking_001）；这里不再二次拼前缀。
    """
    ids: List[str] = []
    for req in card.get("common_requirements", []):
        ids.append(req["id"])
    for t in card.get("alternative_tracks", []):
        for req in t.get("requirements", []):
            ids.append(req["id"])
    return ids


# ============================================================
# 路径 + 复用
# ============================================================

def scorecard_path(scoring_dir: str) -> str:
    return os.path.join(scoring_dir, "job_scorecard.json")


def load_existing_scorecard(scoring_dir: str) -> Optional[Dict[str, Any]]:
    path = scorecard_path(scoring_dir)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# ============================================================
# 生成 scorecard
# ============================================================

_SCORECARD_SYSTEM_PROMPT = """你是 boss-hr 评分系统的 JD 评分卡生成器。

任务：把一段岗位 JD 转成结构化评分卡 JSON，**仅**输出一个合法 JSON object。

要求：
  1. **common_requirements**：两个（或多）方向都共同要求的能力。
  2. **alternative_tracks**：每个岗位方向单独列；id 用英文小写下划线。
     - 多方向 JD（如"线控制动或线控转向"）：生成 2+ 个 track。
     - 单方向 JD：生成 1 个 `general` track。
  3. 每条 requirement 必须有：
       - id：英文小写 + 数字 + 下划线，scope 内唯一。
         common 用 `common_001` 之类；track 内用 `<track>_001`，
         最终 ID 是 `<track_id>_<req_id>`。
       - description：一句话描述要求。
       - importance：`must` / `core` / `preferred` 之一。
         - `must` 只能来自 JD 明确写出"必须 / 硬性 / 最低 / 必备 / 要求"等措辞；
           **不得**因为"行业里一般很重要"就升为 must。
         - `core` 是完成岗位主体工作需要的能力。
         - `preferred` 是加分项。
       - source_evidence：JD 中对应的原文片段或短摘要，方便反查。
  4. education_requirements（可选）：如 JD 明确写出最低学历，记录
     {"minimum_degree": "本科及以上", "source_evidence": "..."}。
  5. **不得**读取 `jobs.json` 或历史 run；只用本次传入的 JD。
  6. **不得**虚构 JD 没写的硬门槛。
  7. 必须是合法 JSON object；用 ```json``` 包裹或直接输出。
"""


def build_scorecard_user_prompt(jd: Dict[str, Any]) -> str:
    norm = normalize_jd(jd)
    body = json.dumps(norm, ensure_ascii=False, indent=2)
    return (
        "请根据以下 JD 生成评分卡 JSON。\n"
        "JD 内容（已规范化）：\n"
        "```json\n" + body + "\n```\n\n"
        "输出 schema（字段名严格按此）：\n"
        "{\n"
        '  "job_name": "<岗位名>",\n'
        '  "common_requirements": [ {"id": "common_001", "description": "...", '
        '"importance": "must|core|preferred", "source_evidence": "..."}, ... ],\n'
        '  "alternative_tracks": [\n'
        '    { "id": "<track_id>", "name": "<方向名>", "requirements": [\n'
        '        {"id": "<req_id>", "description": "...", '
        '"importance": "...", "source_evidence": "..."}\n'
        '      ]\n'
        '    }\n'
        '  ],\n'
        '  "education_requirements": { "minimum_degree": "...", '
        '"source_evidence": "..." }  # 可选\n'
        "}\n"
    )


def _build_scorecard_skeleton(jd: Dict[str, Any]) -> Dict[str, Any]:
    """模型输出 + 本地补全：job_name / source_jd_hash / policy_version /
    policy_hash / schema_version / generated_at。"""
    return {
        "job_name": str(jd.get("jobName") or jd.get("name") or "").strip(),
        "source_jd_hash": source_jd_hash(jd),
    }


def _merge_skeleton(card: Dict[str, Any], skeleton: Dict[str, Any],
                    policy: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(card)
    out.setdefault("job_name", skeleton["job_name"])
    out["source_jd_hash"] = skeleton["source_jd_hash"]
    out["policy_version"] = policy["policy_version"]
    out["policy_hash"] = policy_hash(policy)
    out["schema_version"] = SCORECARD_SCHEMA_VERSION
    out["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return out


def generate_scorecard(
    jd: Dict[str, Any],
    *,
    policy: Dict[str, Any],
    config=None,  # LLMConfig 或 None
    sleep_fn=None,  # for tests
    http_post=None,  # for tests
) -> Dict[str, Any]:
    """调 LLM 生成 scorecard；用 policy + source_jd_hash 填骨架；返回完整 dict。

    不写盘（写盘由 caller 控制原子写入）。抛 LLMClientError。
    """
    if config is None:
        config = load_config_from_env()
    skeleton = _build_scorecard_skeleton(jd)
    msgs = build_messages(
        _SCORECARD_SYSTEM_PROMPT,
        build_scorecard_user_prompt(jd),
    )
    kwargs = {}
    if sleep_fn is not None:
        kwargs["sleep_fn"] = sleep_fn
    if http_post is not None:
        kwargs["http_post"] = http_post
    raw = complete(msgs, config, **kwargs)
    from .llm_client import extract_json  # 延迟 import 避免循环
    parsed = extract_json(raw)
    merged = _merge_skeleton(parsed, skeleton, policy)
    return merged


# ============================================================
# 读取 / 写入 / ensure
# ============================================================

def ensure_scorecard(
    jd: Dict[str, Any],
    *,
    scoring_dir: str,
    policy: Dict[str, Any],
    config=None,
    sleep_fn=None,
    http_post=None,
    write_fn=None,  # for tests; signature: write_fn(path, data)
) -> Tuple[Dict[str, Any], bool]:
    """确保当前 run 有合法 scorecard。

    返回 (card, generated_now)。
      - generated_now=True 表示本次新建。
      - generated_now=False 表示复用。
    抛 ScorecardError 表示旧 scorecard 存在但与当前 JD/policy 不一致。
    """
    if write_fn is None:
        from .llm_client import atomic_write_json
        write_fn = atomic_write_json
    expected_ph = policy_hash(policy)
    jd_hash_now = source_jd_hash(jd)
    existing = load_existing_scorecard(scoring_dir)
    if existing is not None:
        # hash 不一致 → 拒绝
        if existing.get("source_jd_hash") != jd_hash_now:
            raise ScorecardError(
                f"scorecard 来自旧 JD（source_jd_hash={existing.get('source_jd_hash')!r}），"
                f"与当前 JD hash={jd_hash_now!r} 不一致；不得复用"
            )
        if existing.get("policy_hash") != expected_ph:
            raise ScorecardError(
                f"scorecard policy_hash={existing.get('policy_hash')!r} "
                f"与当前策略 {expected_ph!r} 不一致；不得复用"
            )
        # 完整性二次校验
        validate_scorecard(existing, expected_policy_hash=expected_ph)
        return existing, False

    # 生成新
    card = generate_scorecard(
        jd,
        policy=policy,
        config=config,
        sleep_fn=sleep_fn,
        http_post=http_post,
    )
    # 本地补全 + 校验
    card = _merge_skeleton(card, _build_scorecard_skeleton(jd), policy)
    validate_scorecard(card, expected_policy_hash=expected_ph)
    write_fn(scorecard_path(scoring_dir), card)
    return card, True


__all__ = [
    "SCORECARD_SCHEMA_VERSION",
    "ALLOWED_IMPORTANCE",
    "ID_PATTERN",
    "ScorecardError",
    "normalize_jd",
    "source_jd_hash",
    "validate_scorecard",
    "all_requirement_ids",
    "scorecard_path",
    "load_existing_scorecard",
    "generate_scorecard",
    "ensure_scorecard",
]
