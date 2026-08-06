# -*- coding: utf-8 -*-
"""evidence mapping 校准 — 离线测试。

两类：

A. Prompt contract（提示词契约）测试：
   - 不发 HTTP 请求，只检查 build_messages 返回的 system/user 文本包含
     关键校准规则。不做"整段提示词完全相等"断言，避免脆弱。

B. Python 确定性评分测试：
   - 手工构造合法的 LLM evidence mapping JSON（不调 LLM），喂给
     finalize_evaluation / determine_decision，断言 score / decision。
   - 离线测试**不能**证明 MiniMax 一定遵守新提示词；只能证明
     1) 校准后的状态语义在 Python 端算分正确；
     2) 60 分阈值天然阻止"全 unverified 空简历"进入 B。
"""
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
    EVALUATOR_SYSTEM_PROMPT,
    build_evaluator_user_prompt,
    build_messages,
    validate_assessments,
    finalize_evaluation,
    EvaluationError,
    _requirement_lookup,
    ALLOWED_STATUSES,
)
from boss_hr.scoring.llm_client import build_messages as llm_build_messages
from boss_hr.scoring.decision import determine_decision, DECISION_LABELS
from boss_hr.scoring.policy import load_policy, policy_hash
from boss_hr.scoring.scorecard import all_requirement_ids


# ============================================================
# 公共 fixture
# ============================================================

POLICY = load_policy()
PHASH = policy_hash(POLICY)


def _sc_pybackend() -> dict:
    """合成 Python 后端 JD 的 scorecard（与真实 smoke 产物一致：9 个 requirement）。"""
    return {
        "schema_version": "2.0",
        "policy_version": "abc-education-v1",
        "policy_hash": PHASH,
        "source_jd_hash": "jd_smoke_pybackend",
        "job_name": "smoke-pybackend",
        "common_requirements": [],
        "alternative_tracks": [
            {"id": "general", "name": "通用方向",
             "requirements": [
                 {"id": "general_001", "description": "3年以上 Python 后端开发经验",
                  "importance": "must", "source_evidence": "3 年以上 Python 后端经验"},
                 {"id": "general_002", "description": "具备 FastAPI 生产环境开发经验",
                  "importance": "must", "source_evidence": "FastAPI 生产经验"},
                 {"id": "general_003", "description": "熟练掌握 SQL",
                  "importance": "must", "source_evidence": "熟练 SQL"},
                 {"id": "general_004", "description": "掌握 Docker 容器化技术",
                  "importance": "core", "source_evidence": "Docker"},
                 {"id": "general_005", "description": "掌握 Redis 缓存技术",
                  "importance": "core", "source_evidence": "Redis"},
                 {"id": "general_006", "description": "熟悉 REST API 设计与开发",
                  "importance": "core", "source_evidence": "REST API"},
                 {"id": "general_007", "description": "具备 Linux 服务部署能力",
                  "importance": "core", "source_evidence": "Linux 部署"},
                 {"id": "general_008", "description": "熟悉 Kubernetes 容器编排",
                  "importance": "preferred", "source_evidence": "Kubernetes"},
                 {"id": "general_009", "description": "熟悉消息队列（如 Kafka/RabbitMQ）",
                  "importance": "preferred", "source_evidence": "消息队列"},
             ]},
        ],
        "education_requirements": {"minimum_degree": "本科及以上",
                                    "source_evidence": "JD"},
    }


def _resume(degree: str, school: str, name: str) -> dict:
    return {
        "name": name,
        "degree": degree,
        "education": [{"school": school, "degree": degree,
                        "major": "CS", "start": "2018-09", "end": "2022-06"}],
        "_meta": {"encrypt_geek_id": f"gid_{name}", "encrypt_job_id": "eid"},
    }


def _assess(rid: str, status: str, evidence=None, reason: str = "") -> dict:
    return {
        "requirement_id": rid,
        "status": status,
        "evidence": evidence or [],
        "reason": reason,
    }


# ============================================================
# A. Prompt contract 测试（不发 HTTP）
# ============================================================


def test_prompt_contract_build_messages_stateless():
    """build_messages 每次返回新 list，不共享上下文。"""
    sc = _sc_pybackend()
    resume = _resume("本科", "辽宁工业大学", "X")
    p1 = build_evaluator_user_prompt(sc, resume)
    p2 = build_evaluator_user_prompt(sc, resume)
    # 每次 build_messages 返回新 list
    m1 = llm_build_messages("sys", p1)
    m2 = llm_build_messages("sys", p2)
    assert isinstance(m1, list) and len(m1) == 2
    assert m1 is not m2
    assert m1[0] is not m2[0]
    # 不在候选人之间共享聊天上下文
    p_other = build_evaluator_user_prompt(sc, _resume("硕士", "清华", "Y"))
    m_other = llm_build_messages("sys", p_other)
    assert m_other[1]["content"] != m1[1]["content"]


def test_prompt_contract_contains_no_extra_jd_standard_rule():
    """R1：只按 scorecard requirement 原文判断，不得添加 JD 外标准。"""
    must_have = [
        "只按 scorecard 中 requirement 的原文判断",
        "不得添加 JD 未写明的标准",
        "复杂查询 / 慢 SQL 治理 / 索引优化 / 性能调优案例",  # 反例
        "Nginx / CI/CD / 监控 / 系统服务管理",  # 反例
        "逐年职责明细 / 职位字段 / 责任字段",  # 反例
    ]
    for s in must_have:
        assert s in EVALUATOR_SYSTEM_PROMPT, (
            f"提示词缺少约束片段：{s!r}"
        )


def test_prompt_contract_contains_screening_stage_definition():
    """第一部分：当前阶段是简历初筛，不是面试/背景调查。"""
    must_have = [
        "简历初筛",
        "面试级证据",
        "QPS、上线规模、并发量",
        "不得",
    ]
    for s in must_have:
        assert s in EVALUATOR_SYSTEM_PROMPT, (
            f"提示词缺少初筛阶段定义：{s!r}"
        )


def test_prompt_contract_contains_multi_source_aggregation():
    """R2：综合读取简历所有已有字段；不得因某字段为空忽略其他直接证据。"""
    must_have = [
        "个人优势",
        "工作经历",
        "项目经历",
        "某一个字段为空",
        "忽略其他字段中的直接证据",
    ]
    for s in must_have:
        assert s in EVALUATOR_SYSTEM_PROMPT, f"提示词缺少综合多源约束：{s!r}"


def test_prompt_contract_contains_four_status_definitions():
    """R3/R4 + 第三部分：met / partial / unverified / not_met 四种定义。"""
    # 必含 4 个 status 关键词
    for s in ALLOWED_STATUSES:
        # 提示词里以反引号或纯文本形式提到该 status
        assert f"`{s}`" in EVALUATOR_SYSTEM_PROMPT, (
            f"提示词未以状态字面量定义 status={s!r}"
        )

    must_have = [
        "未提及",
        "明确反证",
        "单纯没有提及不能判",  # not_met 不是"未提及"
        "不掌握 / 仅会基础 / 无相关经验",  # 反证举例
        "简历完全未提某技术",  # unverified 路径
    ]
    for s in must_have:
        assert s in EVALUATOR_SYSTEM_PROMPT, f"提示词缺少状态语义约束：{s!r}"


def test_prompt_contract_contains_judgment_examples():
    """第四部分：判断示例 E1-E6。"""
    must_have = [
        "至少 3 年某技术",
        "简历明确写" + '"' + "5 年相关经验" + '"',
        "调优 / 主导 / 长期使用",
        "仅会基础操作 / 仅了解",
        "不兼容方向 Y",
        "JD 要求核心方向 X",
    ]
    for s in must_have:
        assert s in EVALUATOR_SYSTEM_PROMPT, f"提示词缺少示例：{s!r}"


def test_prompt_contract_keeps_output_schema_and_identity():
    """不破坏：output schema / 字段名 / 不替候选人做 A/B/C 决定。"""
    must_have = [
        "assessments",
        "requirement_id",
        "evidence",
        "reason",
        "不",
        "替候选人做 A/B/C 决定",
        "每一条",
        "一个都不能少",
        "只出现一次",
        "未知 ID",
    ]
    for s in must_have:
        assert s in EVALUATOR_SYSTEM_PROMPT, f"提示词丢失原有约束：{s!r}"


def test_prompt_contract_user_prompt_keeps_scorecard_and_resume():
    """user prompt 仍包含 scorecard 与候选人简历 JSON。"""
    sc = _sc_pybackend()
    resume = _resume("本科", "辽宁工业大学", "X")
    p = build_evaluator_user_prompt(sc, resume)
    assert "当前岗位 scorecard" in p
    assert "候选人简历" in p
    # 简历字段被嵌入
    assert "辽宁工业大学" in p
    # 必含 schema
    assert "requirement_id" in p
    assert "status" in p
    assert "evidence" in p
    assert "reason" in p


# ============================================================
# B. Python 确定性评分测试（构造合法 LLM 输出，不调 LLM）
# ============================================================


def _run_evaluator_and_decision(assessments: List[dict], resume: dict):
    sc = _sc_pybackend()
    # validate_assessments 必须先通过
    validate_assessments({"assessments": assessments}, sc)
    ev = finalize_evaluation(
        scorecard=sc, resume=resume, assessments=assessments, policy=POLICY,
    )
    dec = determine_decision(ev, sc, policy=POLICY)
    return ev, dec


def test_calibration_A_gilts():
    """A 候选人离线 evidence：must 全部 met、core 全部 met、preferred unverified。"""
    sc = _sc_pybackend()
    rids = all_requirement_ids(sc)
    assessments = [
        _assess("general_001", "met",
                 evidence=["5 年 Python 后端工程师职位"],
                 reason="5 年职位明确，覆盖 3 年要求"),
        _assess("general_002", "met",
                 evidence=["高并发推荐后端技术栈含 FastAPI"],
                 reason="项目主程，FastAPI 直接命中"),
        _assess("general_003", "met",
                 evidence=["描述含 SQL 调优", "项目 MySQL"],
                 reason="SQL 调优字面命中「熟练」"),
        _assess("general_004", "met",
                 evidence=["项目技术栈 Docker", "Docker 容器化部署到 Linux 生产环境"],
                 reason="Docker 字面命中"),
        _assess("general_005", "met",
                 evidence=["描述含 Redis 缓存", "项目技术栈 Redis"],
                 reason="Redis 字面命中"),
        _assess("general_006", "met",
                 evidence=["描述含 REST 接口", "FastAPI 即 REST 框架"],
                 reason="REST 字面命中"),
        _assess("general_007", "met",
                 evidence=["Docker 容器化部署到 Linux 生产环境"],
                 reason="Linux 部署字面命中"),
        _assess("general_008", "unverified", reason="简历未提 K8s"),
        _assess("general_009", "unverified", reason="简历未提消息队列"),
    ]
    # 覆盖度校验
    assert set(a["requirement_id"] for a in assessments) == set(rids)
    resume = _resume("硕士", "清华大学", "陈甲")
    ev, dec = _run_evaluator_and_decision(assessments, resume)
    # job_match 应约 96.55（按新口径）
    assert abs(ev["job_match_score"] - 96.55) < 0.1
    # final_score = 96.55*0.85 + 100*0.15 ≈ 97.07
    assert abs(ev["final_score"] - 97.07) < 0.1
    # education_tier=清华 = C9 = 100
    assert ev["education_score"] == 100.0
    # decision A
    assert dec["decision"] == "A"
    assert dec["contact_recommended"] is True
    # best_track
    assert ev["best_track"] == "general"
    # must_not_met / core_not_met 都应空
    assert not ev["must_not_met"]
    assert not ev["core_not_met"]


def test_calibration_B_gilts():
    """B 候选人离线 evidence：must 全 met/partial；core mixed；preferred unverified。"""
    sc = _sc_pybackend()
    rids = all_requirement_ids(sc)
    assessments = [
        _assess("general_001", "met",
                 evidence=["4 年 Python 开发职位"],
                 reason="4 年职位明确"),
        _assess("general_002", "met",
                 evidence=["CRM 后端技术栈含 FastAPI", "担任开发"],
                 reason="FastAPI 项目 + 开发角色"),
        _assess("general_003", "partial",
                 evidence=["MySQL SQL"],
                 reason="简历只列 MySQL SQL 未声明熟练程度，低于要求"),
        _assess("general_004", "partial",
                 evidence=["仅会基础 Dockerfile", "未上 Kubernetes"],
                 reason="简历明确自述「仅会基础」，低于「掌握 Docker」"),
        _assess("general_005", "unverified", reason="简历未提 Redis"),
        _assess("general_006", "met",
                 evidence=["FastAPI 即 REST 框架", "业务接口描述"],
                 reason="FastAPI 即 REST 框架"),
        _assess("general_007", "met",
                 evidence=["描述含 Linux 部署"],
                 reason="Linux 部署字面命中"),
        _assess("general_008", "unverified", reason="简历未提 K8s"),
        _assess("general_009", "unverified", reason="简历未提消息队列"),
    ]
    assert set(a["requirement_id"] for a in assessments) == set(rids)
    # 浙江工业大学 = 一本公办
    resume = _resume("本科", "浙江工业大学", "周乙")
    ev, dec = _run_evaluator_and_decision(assessments, resume)
    # job_match 应约 80.34
    assert abs(ev["job_match_score"] - 80.34) < 0.1
    # final = 80.34*0.85 + 71*0.15 = 78.94
    assert abs(ev["final_score"] - 78.94) < 0.1
    # 一本公办 71（按 policy）
    assert ev["education_score"] == 71.0
    # decision B
    assert dec["decision"] == "B"
    assert dec["contact_recommended"] is True
    assert ev["best_track"] == "general"
    # must_not_met 空
    assert not ev["must_not_met"]


def test_calibration_C_direction_conflict():
    """C 候选人：方向冲突导致 must=not_met → C。

    按新提示词 R4 + 第三部分：
      - general_001（Python 后端 3 年）：C 简历明确写 3 年 Java 后端 → 方向反证 → not_met。
      - general_002（FastAPI 生产经验）：C 简历**未提** FastAPI 但**未明确反证**（简历未说"不会 FastAPI"，
        只说项目是 Java Spring）→ 走"未提及"路径 → unverified。
        不得把"未提及"按 not_met 判；direction conflict 已由 general_001 承担。
    """
    sc = _sc_pybackend()
    rids = all_requirement_ids(sc)
    assessments = [
        _assess("general_001", "not_met",
                 evidence=["3 年 Java 后端职位", "项目为 Java Spring"],
                 reason="方向冲突：3 年 Java 非 Python 后端；要求是 Python 后端"),
        _assess("general_002", "unverified",
                 evidence=[],
                 reason="简历未提 FastAPI；也未明确反证不会；Java 项目不构成方向反证"),
        _assess("general_003", "partial",
                 evidence=["Oracle 隐含 SQL"],
                 reason="Oracle 隐含 SQL 但未声明熟练"),
        _assess("general_004", "unverified", reason="未提 Docker"),
        _assess("general_005", "unverified", reason="未提 Redis"),
        _assess("general_006", "unverified", reason="未明示 REST 深度"),
        _assess("general_007", "unverified", reason="未写明 Linux 部署"),
        _assess("general_008", "unverified", reason="未提 K8s"),
        _assess("general_009", "unverified", reason="未提消息队列"),
    ]
    assert set(a["requirement_id"] for a in assessments) == set(rids)
    resume = _resume("硕士", "北京大学", "吴丙")
    ev, dec = _run_evaluator_and_decision(assessments, resume)
    # C（仍因 general_001 must=not_met 命中）
    assert dec["decision"] == "C"
    assert dec["contact_recommended"] is False
    # must_not_met 仅 general_001（general_002 是 unverified，不再进 must_not_met）
    assert "general_001" in ev["must_not_met"]
    assert "general_002" not in ev["must_not_met"]
    # 学历高（C9=100）也救不回
    assert ev["education_score"] == 100.0
    # job_match 复算：
    # 1 must=not_met(0) + 1 must=unverified(0.5*5=2.5) + 1 must=partial(0.6*5=3.0)
    # + 4 core=unverified(0.5*3=6) + 2 preferred=unverified(0.5*1=1)
    # total_c = 0 + 2.5 + 3 + 6 + 1 = 12.5；total_w = 29
    # job_match = 12.5 / 29 * 100 = 43.1034
    assert abs(ev["job_match_score"] - 12.5 / 29 * 100) < 0.01
    # final = 43.1034*0.85 + 100*0.15 = 36.6379 + 15 = 51.6379
    _jm = 12.5 / 29 * 100
    assert abs(ev["final_score"] - (_jm * 0.85 + 100 * 0.15)) < 0.1
    # best_track
    assert ev["best_track"] == "general"
    # 即使教育 C9 (100) 也救不回
    assert ev["education_score"] == 100.0


def test_empty_resume_unverified_all():
    """空简历：所有 9 项 unverified → job_match=50.0，decision=C。"""
    sc = _sc_pybackend()
    rids = all_requirement_ids(sc)
    assessments = [
        _assess(rid, "unverified",
                 evidence=[],
                 reason="简历完全未提该要求")
        for rid in rids
    ]
    # 普通本科
    resume = _resume("本科", "某不知名学院", "X")
    ev, dec = _run_evaluator_and_decision(assessments, resume)
    # 全 unverified → 0.5*29 / 29 = 50.0
    assert abs(ev["job_match_score"] - 50.0) < 1e-6
    # 60 阈值天然阻止空简历进 B
    assert dec["decision"] == "C"
    # 即便学历高到顶（清华硕士）也救不回
    resume_c9 = _resume("硕士", "清华大学", "X")
    ev2, dec2 = _run_evaluator_and_decision(assessments, resume_c9)
    assert dec2["decision"] == "C"
    # 但 final 仍反映学历
    assert ev2["final_score"] > ev["final_score"]


def test_education_only_changes_final_score_not_decision():
    """学历只按 0.15 权重进入 final_score；A/B/C 的技能硬失败语义由 decision 决定。"""
    sc = _sc_pybackend()
    rids = all_requirement_ids(sc)

    # A 候选人：所有 must/core = met
    a_assess = [
        _assess("general_001", "met", reason=""),
        _assess("general_002", "met", reason=""),
        _assess("general_003", "met", reason=""),
        _assess("general_004", "met", reason=""),
        _assess("general_005", "met", reason=""),
        _assess("general_006", "met", reason=""),
        _assess("general_007", "met", reason=""),
        _assess("general_008", "unverified", reason=""),
        _assess("general_009", "unverified", reason=""),
    ]
    # A + 清华硕士（C9=100）→ A
    ev_c9, dec_c9 = _run_evaluator_and_decision(
        a_assess, _resume("硕士", "清华大学", "A"))
    # A + 普通本科（一本公办=71）→ final 不同但仍是 A
    ev_bk, dec_bk = _run_evaluator_and_decision(
        a_assess, _resume("本科", "浙江工业大学", "A"))
    # job_match 不受学历影响
    assert ev_c9["job_match_score"] == ev_bk["job_match_score"]
    # final_score 受 0.15 权重影响
    assert ev_c9["final_score"] > ev_bk["final_score"]
    # decision 都不变（A）
    assert dec_c9["decision"] == "A" and dec_bk["decision"] == "A"

    # C 候选人：必须=not_met
    c_assess = [
        _assess("general_001", "not_met", reason="方向冲突"),
        _assess("general_002", "not_met", reason="无 FastAPI"),
        _assess("general_003", "unverified", reason=""),
        _assess("general_004", "unverified", reason=""),
        _assess("general_005", "unverified", reason=""),
        _assess("general_006", "unverified", reason=""),
        _assess("general_007", "unverified", reason=""),
        _assess("general_008", "unverified", reason=""),
        _assess("general_009", "unverified", reason=""),
    ]
    # C + 清华硕士（C9=100） → 仍是 C
    ev_c9_c, dec_c9_c = _run_evaluator_and_decision(
        c_assess, _resume("硕士", "清华大学", "C"))
    assert dec_c9_c["decision"] == "C"
    # C 的 final：job_match（2 must not_met + 7 unverified = (0+0+7*0.5*1)/29 ... 见算分）*0.85 + 100*0.15
    # 只断言"仍是 C"与"education_score=100"，不锁死具体 final 数值
    assert ev_c9_c["education_score"] == 100.0
    assert "general_001" in ev_c9_c["must_not_met"]
    assert "general_002" in ev_c9_c["must_not_met"]


def test_prompt_contract_validate_assessments_rejects_unknown_id():
    """validate_assessments 仍拒绝未知 ID、重复 ID、缺失 ID（既有契约未破坏）。"""
    sc = _sc_pybackend()
    # 缺失
    incomplete = {"assessments": [
        _assess("general_001", "met", reason=""),
    ]}
    with pytest.raises(EvaluationError):
        validate_assessments(incomplete, sc)
    # 未知 ID
    unknown = {"assessments": [
        _assess("general_001", "met", reason=""),
        _assess("general_002", "met", reason=""),
        _assess("general_003", "met", reason=""),
        _assess("general_004", "met", reason=""),
        _assess("general_005", "met", reason=""),
        _assess("general_006", "met", reason=""),
        _assess("general_007", "met", reason=""),
        _assess("general_008", "unverified", reason=""),
        _assess("general_009", "unverified", reason=""),
        _assess("ghost_unknown", "met", reason=""),
    ]}
    with pytest.raises(EvaluationError):
        validate_assessments(unknown, sc)
    # 重复
    dup = {"assessments": [
        _assess("general_001", "met", reason=""),
        _assess("general_001", "met", reason=""),
    ] + [_assess(f"general_{i:03d}", "met", reason="") for i in range(2, 10)]}
    with pytest.raises(EvaluationError):
        validate_assessments(dup, sc)
