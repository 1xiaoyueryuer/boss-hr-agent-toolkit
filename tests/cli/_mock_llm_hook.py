# -*- coding: utf-8 -*-
"""测试 hook：mock LLM HTTP 响应（仅供 tests/ 加载）。

使用方式：
  - 环境变量 BOSS_HR_LLM_TEST_HOOK=tests.cli._mock_llm_hook
  - 可选 BOSS_HR_LLM_TEST_REQ_IDS=id1,id2,id3（逗号分隔）：
    mock evaluator 返回的 requirement_id 列表
  - 可选 BOSS_HR_LLM_TEST_SCENARIO=scorecard|eval|both：
    默认 both；scorecard 触发 scorecard 响应，eval 触发评估响应
"""
from __future__ import annotations
import json
import os
import re


def http_post_override(url, headers, payload, timeout):
    user_content = ""
    for m in payload.get("messages", []):
        if m.get("role") == "user":
            user_content = m.get("content", "")
            break

    scenario = os.environ.get("BOSS_HR_LLM_TEST_SCENARIO", "both")

    if "评估下面这位候选人" in user_content:
        return {"choices": [{"message": {"content": _mock_eval_response(user_content)}}]}

    if "生成评分卡" in user_content:
        if scenario in ("scorecard", "both"):
            return {"choices": [{"message": {"content": _mock_scorecard_response()}}]}

    return {"choices": [{"message": {"content": _mock_eval_response(user_content)}}]}


def _mock_scorecard_response():
    # 让 mock scorecard 兼容测试中已写的 requirement IDs：
    # 简单返回带 braking_001 + steering_001 + common_001 的多方向 scorecard
    return json.dumps({
        "job_name": "mock_job",
        "common_requirements": [
            {"id": "common_001", "description": "本科及以上",
             "importance": "must", "source_evidence": "JD"}
        ],
        "alternative_tracks": [
            {"id": "braking", "name": "线控制动",
             "requirements": [
                 {"id": "braking_001", "description": "线控制动本体",
                  "importance": "must", "source_evidence": "JD"}
             ]},
            {"id": "steering", "name": "线控转向",
             "requirements": [
                 {"id": "steering_001", "description": "线控转向本体",
                  "importance": "must", "source_evidence": "JD"}
             ]},
        ],
        "education_requirements": {"minimum_degree": "本科及以上",
                                     "source_evidence": "JD"},
    }, ensure_ascii=False)


def _mock_eval_response(user_content: str) -> str:
    """根据 BOSS_HR_LLM_TEST_REQ_IDS 或 user_content 抽 requirement_id。"""
    import sys
    explicit = os.environ.get("BOSS_HR_LLM_TEST_REQ_IDS", "").strip()
    if explicit:
        ids = [s.strip() for s in explicit.split(",") if s.strip()]
        sys.stderr.write(f"[mock_hook] explicit ids={ids}\n")
        sys.stderr.flush()
    else:
        ids = _extract_requirement_ids(user_content)
        sys.stderr.write(f"[mock_hook] extracted ids={ids} from content\n")
        sys.stderr.flush()
    if not ids:
        ids = ["common_001", "braking_001", "steering_001"]

    assessments = [
        {"requirement_id": rid, "status": "met",
         "evidence": ["候选人符合"], "reason": "符合"}
        for rid in ids
    ]
    return json.dumps({"assessments": assessments}, ensure_ascii=False)


def _extract_requirement_ids(text: str) -> list[str]:
    """从 JSON 块中抽 requirement id（id 字段紧跟 description/source_evidence 之前的）。"""
    ids: list[str] = []
    # 抽 common_requirements 段（到下一个顶层字段）
    m = re.search(r'"common_requirements"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if m:
        for rid in re.findall(r'"id"\s*:\s*"([^"]+)"', m.group(1)):
            ids.append(rid)
    # 抽所有 requirements 子数组的 id
    for req_block in re.findall(r'"requirements"\s*:\s*\[([^\[\]]*)\]', text, re.DOTALL):
        for rid in re.findall(r'"id"\s*:\s*"([^"]+)"', req_block):
            if rid not in ids:
                ids.append(rid)
    return ids
