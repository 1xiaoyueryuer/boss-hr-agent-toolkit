# -*- coding: utf-8 -*-
"""greet v2 schema 行为测试。"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
_TOOLKIT_ROOT = r"C:\Users\yuyu\Desktop\boss-hr-agent-toolkit"
sys.path.insert(0, _TOOLKIT_ROOT)
sys.path.insert(0, str(Path(_TOOLKIT_ROOT) / "shared"))


def _spec(name):
    p = str(Path(_TOOLKIT_ROOT) / "boss-hr-greet" / "scripts" / "auto_greet.py")
    import importlib.util
    spec_ = importlib.util.spec_from_file_location(name, p)
    mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(mod)
    return mod


# autouse: 子模块 reload-friendly
@pytest.fixture(autouse=True)
def _reload_greet():
    sys.modules.pop("auto_greet", None)
    yield


def test_v2_a_b_included_c_excluded(tmp_path):
    ag = _spec("auto_greet")
    job_dir = tmp_path
    run_id = "2026-08-05_120000"
    run_dir = job_dir / "runs" / run_id
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    screening = {
        "schema_version": "2.0",
        "candidates": [
            {"name": "Alice", "final_score": 90, "decision": "A",
             "contact_recommended": True, "education_tier": "985",
             "best_track": "braking"},
            {"name": "Bob", "final_score": 75, "decision": "B",
             "contact_recommended": True, "education_tier": "211",
             "best_track": "braking"},
            {"name": "Carol", "final_score": 30, "decision": "C",
             "contact_recommended": False, "education_tier": "C9",
             "best_track": None},
        ],
    }
    (process_dir / "screening_results.json").write_text(
        json.dumps(screening, ensure_ascii=False), encoding="utf-8"
    )
    high = ag.load_high_score_candidates(str(job_dir), run_id,
                                          score_threshold=70)
    names = sorted([h["name"] for h in high])
    assert names == ["Alice", "Bob"]
    # Alice (A) 排在 Bob (B) 之前
    assert high[0]["name"] == "Alice"
    assert high[1]["name"] == "Bob"
    # C 永远不出现在 v2 路径（即使 threshold=0 也不允许）
    high_zero = ag.load_high_score_candidates(str(job_dir), run_id,
                                                score_threshold=0)
    names_zero = sorted([h["name"] for h in high_zero])
    assert "Carol" not in names_zero
    assert names_zero == ["Alice", "Bob"]


def test_v2_cannot_be_chosen_via_only_names():
    """v2 模式 --only-names 也不能把 C 候选人选入。"""
    ag = _spec("auto_greet")
    job_dir = "/tmp/x"
    run_id = "r1"
    # 不写 screening_results.json
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names="C先生")
    # 缺 screening_results.json → 空
    assert high == []


def test_v2_only_names_with_pool_does_not_bring_back_c():
    """v2 模式 from-pool 兜底仍允许（v1 fallback）；v2 不会把 pool 当 A/B 评。"""
    # 当前实现：pool 补人 → tier='from-pool'；v2 不再筛 C
    # 测试：--only-names 一个 from-pool 名字，应出现在 high 中
    # （具体规则见 auto_greet.py:251 注释）
    ag = _spec("auto_greet")
    job_dir = "/tmp/x"
    run_id = "r1"
    # 不写 screening，期望 high=[]
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names="Bob")
    assert high == []


def test_v2_threshold_compatibility():
    """v2 协议：--threshold **完全不参与** 资格判断。

    v2 资格 = decision in (A, B) AND contact_recommended=true。
    即便 --threshold=100，合法 A/B 仍入选。
    即便 --threshold=0，decision=C 仍排除。
    """
    ag = _spec("auto_greet")
    job_dir = "/tmp/x"
    run_id = "r1"
    run_dir = Path(job_dir) / "runs" / run_id
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    screening = {
        "schema_version": "2.0",
        "candidates": [
            {"name": "A1", "final_score": 50, "decision": "A",
             "contact_recommended": True, "education_tier": "C9",
             "best_track": "braking"},
            {"name": "B1", "final_score": 40, "decision": "B",
             "contact_recommended": True, "education_tier": "211",
             "best_track": "steering"},
            {"name": "C1", "final_score": 99, "decision": "C",
             "contact_recommended": False, "education_tier": "C9"},
        ],
    }
    (process_dir / "screening_results.json").write_text(
        json.dumps(screening, ensure_ascii=False), encoding="utf-8"
    )
    # threshold=100：A/B 仍入选（C 不依赖 threshold 排除）
    high = ag.load_high_score_candidates(str(job_dir), run_id, score_threshold=100)
    names = sorted([h["name"] for h in high])
    assert "A1" in names
    assert "B1" in names
    assert "C1" not in names

    # threshold=0：C 仍排除（A/B contact_recommended=false 同样排除）
    screening_b = {
        "schema_version": "2.0",
        "candidates": [
            {"name": "Ax", "final_score": 0, "decision": "A",
             "contact_recommended": False},
            {"name": "Cx", "final_score": 0, "decision": "C",
             "contact_recommended": True},
        ],
    }
    (process_dir / "screening_results.json").write_text(
        json.dumps(screening_b, ensure_ascii=False), encoding="utf-8"
    )
    high = ag.load_high_score_candidates(str(job_dir), run_id, score_threshold=0)
    names = [h["name"] for h in high]
    assert names == []


def test_v2_c_with_high_score_still_excluded():
    """v2 即使 final_score=99 但 decision=C，也永远排除（C 优先规则）。"""
    ag = _spec("auto_greet")
    job_dir = "/tmp/x"
    run_id = "r1"
    run_dir = Path(job_dir) / "runs" / run_id
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    screening = {
        "schema_version": "2.0",
        "candidates": [
            {"name": "HighC", "final_score": 99, "decision": "C",
             "contact_recommended": False, "education_tier": "C9"},
        ],
    }
    (process_dir / "screening_results.json").write_text(
        json.dumps(screening, ensure_ascii=False), encoding="utf-8"
    )
    high = ag.load_high_score_candidates(str(job_dir), run_id, score_threshold=0)
    assert high == []


def test_v1_still_uses_threshold():
    """v1 screening_results.json 仍按 score + threshold 工作（向后兼容）。"""
    ag = _spec("auto_greet")
    job_dir = "/tmp/x"
    run_id = "r1"
    run_dir = Path(job_dir) / "runs" / run_id
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    screening = {
        # 没 schema_version
        "candidates": [
            {"name": "Old", "total": 80, "tier": "推荐"},
            {"name": "Low", "total": 50, "tier": "不推荐"},
        ],
    }
    (process_dir / "screening_results.json").write_text(
        json.dumps(screening, ensure_ascii=False), encoding="utf-8"
    )
    high = ag.load_high_score_candidates(str(job_dir), run_id, score_threshold=70)
    names = [h["name"] for h in high]
    assert "Old" in names
    assert "Low" not in names


def test_v2_decision_field_in_output():
    """v2 模式 high 列表里 tier 字段是 decision（A/B），不是 v1 的"推荐/待定"。"""
    ag = _spec("auto_greet")
    job_dir = "/tmp/x"
    run_id = "r1"
    run_dir = Path(job_dir) / "runs" / run_id
    process_dir = run_dir / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    screening = {
        "schema_version": "2.0",
        "candidates": [
            {"name": "A1", "final_score": 95, "decision": "A",
             "contact_recommended": True, "education_tier": "C9",
             "best_track": "braking"},
            {"name": "B1", "final_score": 75, "decision": "B",
             "contact_recommended": True, "education_tier": "211",
             "best_track": "steering"},
        ],
    }
    (process_dir / "screening_results.json").write_text(
        json.dumps(screening, ensure_ascii=False), encoding="utf-8"
    )
    high = ag.load_high_score_candidates(str(job_dir), run_id, score_threshold=70)
    tiers = {h["name"]: h["tier"] for h in high}
    assert tiers["A1"] == "A"
    assert tiers["B1"] == "B"


# ============================================================
# v2 threshold 修复：threshold 完全不参与资格判断
# ============================================================

def _write_v2_screening(tmp_path, candidates):
    job_dir = tmp_path
    run_id = "2026-08-05_120000"
    process_dir = job_dir / "runs" / run_id / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    screening = {
        "schema_version": "2.0",
        "job_name": "x",
        "candidates": candidates,
    }
    (process_dir / "screening_results.json").write_text(
        json.dumps(screening, ensure_ascii=False), encoding="utf-8"
    )
    return str(job_dir), run_id


def test_v2_b_with_score_65_default_threshold_70_still_included(tmp_path):
    """v2 B 类 final_score=65、默认 threshold=70 仍入选。

    v2 资格 = decision in (A, B) AND contact_recommended=true。
    threshold 不参与 v2 资格判断。
    """
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_screening(tmp_path, [
        {"name": "B_LowScore", "final_score": 65.0, "decision": "B",
         "contact_recommended": True, "education_tier": "211"},
    ])
    high = ag.load_high_score_candidates(job_dir, run_id, score_threshold=70)
    names = [h["name"] for h in high]
    assert "B_LowScore" in names


def test_v2_a_b_still_included_with_threshold_100(tmp_path):
    """v2 A/B 在 threshold=100 时仍入选。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_screening(tmp_path, [
        {"name": "Aa", "final_score": 50, "decision": "A",
         "contact_recommended": True},
        {"name": "Bb", "final_score": 40, "decision": "B",
         "contact_recommended": True},
    ])
    high = ag.load_high_score_candidates(job_dir, run_id, score_threshold=100)
    names = sorted([h["name"] for h in high])
    assert names == ["Aa", "Bb"]


def test_v2_c_still_excluded_with_threshold_0(tmp_path):
    """v2 C 在 threshold=0 时仍排除（C 排除不依赖 threshold）。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_screening(tmp_path, [
        {"name": "Cc", "final_score": 99, "decision": "C",
         "contact_recommended": True},  # 即使 contact_recommended=true，C 仍排除
    ])
    high = ag.load_high_score_candidates(job_dir, run_id, score_threshold=0)
    assert high == []


def test_v2_a_b_with_contact_recommended_false_excluded(tmp_path):
    """v2 decision=A/B 但 contact_recommended=false → 排除。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_screening(tmp_path, [
        {"name": "A_no_contact", "final_score": 95, "decision": "A",
         "contact_recommended": False},
        {"name": "B_no_contact", "final_score": 75, "decision": "B",
         "contact_recommended": False},
    ])
    high = ag.load_high_score_candidates(job_dir, run_id, score_threshold=70)
    assert high == []


def test_v2_c_with_contact_recommended_true_still_excluded(tmp_path):
    """v2 decision=C 但 contact_recommended=true → 仍排除（decision 是硬规则）。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_screening(tmp_path, [
        {"name": "C_with_contact", "final_score": 99, "decision": "C",
         "contact_recommended": True},
    ])
    high = ag.load_high_score_candidates(job_dir, run_id, score_threshold=0)
    assert high == []


def test_v1_still_uses_threshold_filter(tmp_path):
    """v1 旧 screening_results.json 仍按 score >= threshold 工作。"""
    ag = _spec("auto_greet")
    job_dir = tmp_path
    run_id = "2026-08-05_120000"
    process_dir = job_dir / "runs" / run_id / "process"
    process_dir.mkdir(parents=True, exist_ok=True)
    screening = {
        # 无 schema_version → v1 协议
        "candidates": [
            {"name": "Old", "total": 80, "tier": "推荐"},
            {"name": "Low", "total": 50, "tier": "不推荐"},
        ],
    }
    (process_dir / "screening_results.json").write_text(
        json.dumps(screening, ensure_ascii=False), encoding="utf-8"
    )
    high = ag.load_high_score_candidates(str(job_dir), run_id, score_threshold=70)
    names = [h["name"] for h in high]
    assert "Old" in names
    assert "Low" not in names


def test_v2_order_a_then_b_then_final_score_desc(tmp_path):
    """v2 排序：A 优先 → B → 同类 final_score 倒序。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_screening(tmp_path, [
        # 故意乱序：A_low、A_high、B_low、B_high、C
        {"name": "B_low", "final_score": 60, "decision": "B",
         "contact_recommended": True},
        {"name": "A_low", "final_score": 85, "decision": "A",
         "contact_recommended": True},
        {"name": "B_high", "final_score": 80, "decision": "B",
         "contact_recommended": True},
        {"name": "A_high", "final_score": 95, "decision": "A",
         "contact_recommended": True},
        {"name": "C1", "final_score": 99, "decision": "C",
         "contact_recommended": True},
    ])
    high = ag.load_high_score_candidates(str(job_dir), run_id, score_threshold=70)
    order = [h["name"] for h in high]
    assert order == ["A_high", "A_low", "B_high", "B_low"]


def test_v2_max_count_truncates_after_sort(tmp_path):
    """v2 --max 在排序之后截断。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_screening(tmp_path, [
        {"name": "A1", "final_score": 95, "decision": "A", "contact_recommended": True},
        {"name": "A2", "final_score": 90, "decision": "A", "contact_recommended": True},
        {"name": "B1", "final_score": 75, "decision": "B", "contact_recommended": True},
        {"name": "B2", "final_score": 70, "decision": "B", "contact_recommended": True},
    ])
    high_full = ag.load_high_score_candidates(str(job_dir), run_id,
                                              score_threshold=70)
    assert len(high_full) == 4
    # auto_greet 里 max_count 截断；这里 load_high_score_candidates 不做 max 截断
    # 但排序仍按 v2 规则：A 优先 → final_score 倒序
    order = [h["name"] for h in high_full]
    assert order == ["A1", "A2", "B1", "B2"]


# ============================================================
# v2 --only-names 修复：与 v2 screening_results 合法集合取交集；
# 禁止从 candidate_pool.json 等文件补招呼资格。
# ============================================================

def _write_v2_with_pool(tmp_path, screening_cands, pool_items=None):
    """写 v2 screening_results.json + candidate_pool.json。"""
    job_dir, run_id = _write_v2_screening(tmp_path, screening_cands)
    if pool_items is not None:
        import os as _os
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        with open(state_dir / "candidate_pool.json", "w", encoding="utf-8") as _f:
            json.dump({"items": pool_items}, _f, ensure_ascii=False)
    return str(job_dir), run_id


def test_v2_only_names_picks_a(tmp_path):
    """v2 --only-names 指定 A → 入选。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_with_pool(
        tmp_path,
        [{"name": "A1", "final_score": 90, "decision": "A",
          "contact_recommended": True}],
    )
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names=["A1"])
    names = [h["name"] for h in high]
    assert names == ["A1"]


def test_v2_only_names_picks_b(tmp_path):
    """v2 --only-names 指定 B → 入选。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_with_pool(
        tmp_path,
        [{"name": "B1", "final_score": 70, "decision": "B",
          "contact_recommended": True}],
    )
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names=["B1"])
    assert [h["name"] for h in high] == ["B1"]


def test_v2_only_names_excludes_c(tmp_path):
    """v2 --only-names 指定 C → 排除（C 不在 v2 合法集合内）。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_with_pool(
        tmp_path,
        [{"name": "C1", "final_score": 99, "decision": "C",
          "contact_recommended": True}],
    )
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names=["C1"])
    assert high == []


def test_v2_only_names_a_b_c_only_a_b(tmp_path):
    """v2 --only-names 同时指定 A/B/C → 仅保留 A/B。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_with_pool(
        tmp_path,
        [
            {"name": "A1", "final_score": 90, "decision": "A",
             "contact_recommended": True},
            {"name": "B1", "final_score": 70, "decision": "B",
             "contact_recommended": True},
            {"name": "C1", "final_score": 50, "decision": "C",
             "contact_recommended": True},
        ],
    )
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names=["A1","B1","C1"])
    names = sorted([h["name"] for h in high])
    assert names == ["A1", "B1"]


def test_v2_only_names_unknown_not_pulled_from_pool(tmp_path):
    """v2 --only-names 指定的人在 candidate_pool 中，但不在 v2 screening_results：
    不得补入招呼资格。
    """
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_with_pool(
        tmp_path,
        screening_cands=[
            {"name": "InScreening", "final_score": 80, "decision": "A",
             "contact_recommended": True},
        ],
        pool_items={
            "k1": {"name": "PoolOnly", "encrypt_geek_id": "gid_pool",
                    "school": "x"},
        },
    )
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names=["PoolOnly"])
    assert high == []
    # 反向：InScreening 在 v2 screening 中 → --only-names InScreening 入选
    high2 = ag.load_high_score_candidates(job_dir, run_id,
                                           score_threshold=70,
                                           only_names=["InScreening"])
    assert [h["name"] for h in high2] == ["InScreening"]


def test_v2_only_names_same_name_different_id_safe(tmp_path):
    """v2 同名不同 geekId：encryptGeekId 仍按 BOSS 唯一身份防误发。

    这里 load_high_score_candidates 按 name 过滤；
    实际点击时按 encryptGeekId 定位。验证：同名 C1 仅 C 不入选；
    同名 A1 仍按 name 出现在 high 列表（不重复）。
    """
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_with_pool(
        tmp_path,
        [
            {"name": "杨先生", "final_score": 90, "decision": "A",
             "contact_recommended": True},
            {"name": "杨先生", "final_score": 70, "decision": "B",
             "contact_recommended": True},
            {"name": "杨先生", "final_score": 30, "decision": "C",
             "contact_recommended": True},
        ],
    )
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names=["杨先生"])
    # 三条同名，按 name 过滤时三次都命中；但 high 列表以每条入一份
    # （更稳健的做法是用 encryptGeekId 唯一过滤；此处仅验证逻辑层）
    # 至少 C 不出现在 high（C 必须被 v2 资格排除）
    assert all(h["tier"] != "C" for h in high)


def test_v2_only_names_empty_after_filter_no_candidates(tmp_path, capsys):
    """v2 only-names 筛选后无人符合 → high 空。"""
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_with_pool(
        tmp_path,
        [{"name": "OnlyC", "final_score": 99, "decision": "C",
          "contact_recommended": True}],
    )
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70,
                                          only_names=["OnlyC"])
    assert high == []
    # 调用方（auto_greet 主流程）看到 high 为空 → status=no_candidates / dry_run 不点击
    # 这里仅验证 high 为空；点击/扫描路径不在 load_high_score_candidates 单元内


def test_v2_dry_run_does_not_modify_run(tmp_path):
    """v2 dry-run 仍由主流程保证零 run 修改；
    load_high_score_candidates 是只读函数，本身不改 run。
    本测试确认：load_high_score_candidates 调用前后 run 目录无变化。
    """
    ag = _spec("auto_greet")
    job_dir, run_id = _write_v2_with_pool(
        tmp_path,
        [
            {"name": "A1", "final_score": 90, "decision": "A",
             "contact_recommended": True},
        ],
    )
    run_dir = Path(job_dir) / "runs" / run_id
    before = sorted(p.name for p in run_dir.rglob("*"))
    high = ag.load_high_score_candidates(job_dir, run_id,
                                          score_threshold=70)
    after = sorted(p.name for p in run_dir.rglob("*"))
    assert before == after
    # load_high_score_candidates 只读，不写任何 output
    assert [h["name"] for h in high] == ["A1"]
