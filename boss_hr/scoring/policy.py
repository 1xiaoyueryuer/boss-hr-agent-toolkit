# -*- coding: utf-8 -*-
"""boss_hr.scoring.policy — 评分策略单一来源（v2）。

设计：
  - 整个工具包对评分权重 / 学历分档 / importance→status→分数映射
    只通过本模块读取；不允许在多脚本里硬编码重复。
  - 加载 `resume-screener/scoring-policy.json`；加载失败或权重之和不
    为 1 → 立即抛错。
  - 暴露 `load_policy()` 同步接口 + 模块级 `POLICY` 单例（首次访问时
    懒加载；测试用 monkeypatch 重置）。
  - `policy_hash()` 用 SHA-256 字典序序列化，保证跨平台一致。
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict

# 把工具包根 + shared/ 加进 sys.path，确保子进程 entry 也能 import
_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))


# 评分策略默认 JSON 路径
DEFAULT_POLICY_PATH = str(
    _TOOLKIT_ROOT / "resume-screener" / "scoring-policy.json"
)


def _read_policy_dict(path: str) -> Dict[str, Any]:
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"评分策略文件不存在：{path}。检查仓库是否完整。"
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"评分策略文件不是合法 JSON：{e}")
    if not isinstance(data, dict):
        raise ValueError("评分策略文件顶层必须是 JSON object")
    return data


def _validate_policy_dict(data: Dict[str, Any]) -> None:
    """校验策略内容：权重和、必备字段、importance/status 取值。"""
    required_top = (
        "schema_version",
        "policy_version",
        "job_match_weight",
        "education_weight",
        "unknown_school_score",
        "importance_weights",
        "status_values",
        "degree_bonus",
    )
    for k in required_top:
        if k not in data:
            raise ValueError(f"评分策略缺少字段：{k}")

    jm = data["job_match_weight"]
    ed = data["education_weight"]
    if not isinstance(jm, (int, float)) or not isinstance(ed, (int, float)):
        raise ValueError("job_match_weight / education_weight 必须是数字")
    if abs(jm + ed - 1.0) > 1e-6:
        raise ValueError(
            f"job_match_weight({jm}) + education_weight({ed}) 必须等于 1.0"
        )

    imp = data["importance_weights"]
    if set(imp.keys()) - {"must", "core", "preferred"}:
        raise ValueError(f"importance_weights 含未知键：{list(imp.keys())}")
    for k in ("must", "core", "preferred"):
        if k not in imp or not isinstance(imp[k], (int, float)):
            raise ValueError(f"importance_weights.{k} 必须是数字")
    if imp["must"] <= imp["core"] or imp["core"] <= imp["preferred"]:
        raise ValueError("importance_weights 必须满足 must > core > preferred")

    st = data["status_values"]
    if set(st.keys()) - {"met", "partial", "unverified", "not_met"}:
        raise ValueError(f"status_values 含未知键：{list(st.keys())}")
    for k in ("met", "partial", "unverified", "not_met"):
        if k not in st or not isinstance(st[k], (int, float)):
            raise ValueError(f"status_values.{k} 必须是数字")
    expected_order = {"met": 1.0, "partial": 0.6, "unverified": 0.5, "not_met": 0.0}
    for k, v in expected_order.items():
        if abs(float(st[k]) - v) > 1e-6:
            raise ValueError(
                f"status_values.{k} 期望 {v} 实际 {st[k]}（必须严格符合 v2 规范）"
            )

    deg = data["degree_bonus"]
    if not isinstance(deg, dict):
        raise ValueError("degree_bonus 必须是 dict")
    for k in ("硕士", "博士"):
        if k in deg and (not isinstance(deg[k], (int, float)) or deg[k] < 0):
            raise ValueError(f"degree_bonus.{k} 必须是非负数")

    unk = data["unknown_school_score"]
    if not isinstance(unk, (int, float)) or not (0 <= unk <= 100):
        raise ValueError("unknown_school_score 必须是 0-100 数字")


@lru_cache(maxsize=1)
def load_policy(path: str = DEFAULT_POLICY_PATH) -> Dict[str, Any]:
    """同步加载评分策略；命中 lru_cache → 进程内单例。"""
    data = _read_policy_dict(path)
    _validate_policy_dict(data)
    return data


def reset_policy_cache() -> None:
    """测试用：清掉 lru_cache 让下一次 load_policy 重新读盘。"""
    load_policy.cache_clear()


def policy_hash(policy: Dict[str, Any]) -> str:
    """对策略 dict 做 SHA-256 字典序序列化哈希（跨平台稳定）。"""
    canonical = json.dumps(
        policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def status_value(policy: Dict[str, Any], status: str) -> float:
    return float(policy["status_values"][status])


def importance_weight(policy: Dict[str, Any], importance: str) -> float:
    return float(policy["importance_weights"][importance])


__all__ = [
    "DEFAULT_POLICY_PATH",
    "load_policy",
    "reset_policy_cache",
    "policy_hash",
    "status_value",
    "importance_weight",
]


if __name__ == "__main__":
    # 冒烟：加载 + 打印 hash
    p = load_policy()
    print(json.dumps({
        "policy_version": p["policy_version"],
        "policy_hash": policy_hash(p),
        "importance_weights": p["importance_weights"],
        "status_values": p["status_values"],
        "job_match_weight": p["job_match_weight"],
        "education_weight": p["education_weight"],
    }, ensure_ascii=False, indent=2))
