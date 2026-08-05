# -*- coding: utf-8 -*-
"""评分策略 + hash 单元测试。"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
_TOOLKIT_ROOT = _HERE.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.scoring.policy import (
    load_policy, policy_hash, status_value, importance_weight, reset_policy_cache,
    DEFAULT_POLICY_PATH,
)


def test_load_policy_succeeds():
    p = load_policy()
    assert p["policy_version"] == "abc-education-v1"
    assert p["job_match_weight"] + p["education_weight"] == pytest.approx(1.0)
    assert set(p["importance_weights"].keys()) == {"must", "core", "preferred"}


def test_status_values_v2_contract():
    p = load_policy()
    assert status_value(p, "met") == 1.0
    assert status_value(p, "partial") == 0.6
    assert status_value(p, "unverified") == 0.5
    assert status_value(p, "not_met") == 0.0


def test_importance_order():
    p = load_policy()
    assert importance_weight(p, "must") > importance_weight(p, "core")
    assert importance_weight(p, "core") > importance_weight(p, "preferred")


def test_policy_hash_stable():
    p1 = load_policy()
    h1 = policy_hash(p1)
    # 同样的 dict → 同样 hash
    h2 = policy_hash(dict(p1))
    assert h1 == h2
    assert len(h1) == 64  # SHA-256 hex


def test_policy_hash_changes_on_mutation():
    p1 = load_policy()
    h1 = policy_hash(p1)
    p2 = dict(p1)
    p2["unknown_school_score"] = 80
    h2 = policy_hash(p2)
    assert h1 != h2


def test_policy_hash_canonical_key_order():
    """字典 key 顺序不影响 hash（key 排序后序列化）。"""
    p = load_policy()
    p1 = {"a": 1, "b": 2, "c": 3}
    p2 = {"c": 3, "b": 2, "a": 1}
    assert policy_hash(p1) == policy_hash(p2)


def test_policy_file_must_exist(tmp_path):
    bogus = tmp_path / "no.json"
    with pytest.raises(FileNotFoundError):
        load_policy(str(bogus))


def test_policy_must_be_dict(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_policy_invalid_json(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("{ broken", encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_policy_weight_must_sum_to_1(tmp_path):
    p = load_policy()
    bad = dict(p)
    bad["job_match_weight"] = 0.5
    bad["education_weight"] = 0.3
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_policy_unknown_importance_rejected(tmp_path):
    p = load_policy()
    bad = dict(p)
    bad["importance_weights"] = {"must": 5, "core": 3, "preferred": 1, "extra": 99}
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_policy_unknown_status_rejected(tmp_path):
    p = load_policy()
    bad = dict(p)
    bad["status_values"] = {"met": 1.0, "partial": 0.6, "unverified": 0.5,
                             "not_met": 0.0, "extra": 0.3}
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_policy_status_values_must_be_v2_canonical():
    p = load_policy()
    bad = dict(p)
    bad["status_values"] = {"met": 0.9, "partial": 0.5,
                             "unverified": 0.4, "not_met": 0.0}
    import tempfile
    f = Path(tempfile.mkstemp(suffix=".json")[1])
    f.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_policy_importance_order_must_strictly_decrease(tmp_path):
    p = load_policy()
    bad = dict(p)
    bad["importance_weights"] = {"must": 3, "core": 3, "preferred": 1}
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_policy_unknown_school_score_range(tmp_path):
    p = load_policy()
    bad = dict(p)
    bad["unknown_school_score"] = 200
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_policy_degree_bonus_must_be_non_negative(tmp_path):
    p = load_policy()
    bad = dict(p)
    bad["degree_bonus"] = {"硕士": -1}
    f = tmp_path / "bad.json"
    f.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError):
        load_policy(str(f))


def test_default_policy_path_exists():
    assert Path(DEFAULT_POLICY_PATH).is_file()
