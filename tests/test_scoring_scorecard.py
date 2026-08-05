# -*- coding: utf-8 -*-
"""JD scorecard 单元测试。"""
from __future__ import annotations
import json
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
_TOOLKIT_ROOT = _HERE.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.scoring.scorecard import (
    normalize_jd, source_jd_hash, validate_scorecard, all_requirement_ids,
    scorecard_path, load_existing_scorecard, generate_scorecard,
    ensure_scorecard, ScorecardError, SCORECARD_SCHEMA_VERSION,
    ALLOWED_IMPORTANCE, ID_PATTERN,
)
from boss_hr.scoring.policy import load_policy, policy_hash


# ============================================================
# Hash: 同一 JD 同一 hash
# ============================================================

def test_same_jd_same_hash():
    jd = {"jobName": "x", "encryptJobId": "e1", "bodyText": "本科及以上",
          "formValues": []}
    assert source_jd_hash(jd) == source_jd_hash(dict(jd))


def test_different_jd_different_hash():
    jd1 = {"jobName": "x", "bodyText": "本科"}
    jd2 = {"jobName": "y", "bodyText": "本科"}
    assert source_jd_hash(jd1) != source_jd_hash(jd2)


# ============================================================
# validate_scorecard: 多方向 + 单方向
# ============================================================

def _good_card(extra: dict | None = None) -> dict:
    policy = load_policy()
    card = {
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "policy_version": policy["policy_version"],
        "policy_hash": policy_hash(policy),
        "source_jd_hash": "jd-hash-1",
        "job_name": "x",
        "common_requirements": [
            {"id": "common_001", "description": "本科", "importance": "must",
             "source_evidence": "JD"}
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
    if extra:
        card.update(extra)
    return card


def test_validate_multitrack_scorecard():
    card = _good_card()
    policy = load_policy()
    validate_scorecard(card, expected_policy_hash=policy_hash(policy))


def test_validate_single_track_general():
    policy = load_policy()
    card = _good_card()
    card["alternative_tracks"] = [
        {"id": "general", "name": "通用",
         "requirements": [
             {"id": "general_001", "description": "通用", "importance": "must",
              "source_evidence": "JD"},
         ]},
    ]
    validate_scorecard(card, expected_policy_hash=policy_hash(policy))


def test_validate_wrong_schema_version_rejected():
    card = _good_card()
    card["schema_version"] = "1.0"
    with pytest.raises(ScorecardError):
        validate_scorecard(card, expected_policy_hash=policy_hash(load_policy()))


def test_validate_wrong_policy_hash_rejected():
    card = _good_card()
    with pytest.raises(ScorecardError):
        validate_scorecard(card, expected_policy_hash="wrong-hash")


def test_validate_must_only_from_jd_evidence():
    """must 必须有 source_evidence 反查"""
    card = _good_card()
    card["common_requirements"][0]["source_evidence"] = ""
    with pytest.raises(ScorecardError):
        validate_scorecard(card, expected_policy_hash=policy_hash(load_policy()))


def test_validate_unknown_importance_rejected():
    card = _good_card()
    card["common_requirements"][0]["importance"] = "critical"
    with pytest.raises(ScorecardError):
        validate_scorecard(card, expected_policy_hash=policy_hash(load_policy()))


def test_validate_duplicate_requirement_id_rejected():
    card = _good_card()
    card["alternative_tracks"][0]["requirements"].append(
        {"id": "braking_001", "description": "dup", "importance": "must",
         "source_evidence": "JD"}
    )
    with pytest.raises(ScorecardError):
        validate_scorecard(card, expected_policy_hash=policy_hash(load_policy()))


def test_validate_bad_id_format_rejected():
    card = _good_card()
    card["common_requirements"][0]["id"] = "Common 001"  # 含空格
    with pytest.raises(ScorecardError):
        validate_scorecard(card, expected_policy_hash=policy_hash(load_policy()))


def test_validate_missing_jd_hash_rejected():
    card = _good_card()
    card.pop("source_jd_hash")
    with pytest.raises(ScorecardError):
        validate_scorecard(card, expected_policy_hash=policy_hash(load_policy()))


def test_all_requirement_ids():
    ids = all_requirement_ids(_good_card())
    assert ids == ["common_001", "braking_001", "steering_001"]


# ============================================================
# ensure_scorecard: hash 匹配复用
# ============================================================

def test_ensure_scorecard_reuses_existing(tmp_path, monkeypatch):
    scoring_dir = tmp_path / "scoring"
    scoring_dir.mkdir()
    policy = load_policy()
    jd = {"jobName": "x", "encryptJobId": "e1", "bodyText": "本科", "formValues": []}
    card = _good_card({
        "source_jd_hash": source_jd_hash(jd),
    })
    (scoring_dir / "job_scorecard.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    called = []
    def fake_write(path, data):
        called.append(1)
        from boss_hr.scoring.llm_client import atomic_write_json
        atomic_write_json(path, data)

    sc, generated = ensure_scorecard(
        jd, scoring_dir=str(scoring_dir), policy=policy, write_fn=fake_write,
    )
    assert generated is False
    assert called == []
    assert sc["source_jd_hash"] == source_jd_hash(jd)


def test_ensure_scorecard_writes_when_missing(tmp_path):
    scoring_dir = tmp_path / "scoring"
    scoring_dir.mkdir()
    policy = load_policy()
    jd = {"jobName": "x", "encryptJobId": "e1", "bodyText": "本科", "formValues": []}

    # 配 LLM env 让 generate_scorecard 不抛 config 错
    from boss_hr.scoring.llm_client import LLMConfig
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")

    def fake_post(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": json.dumps({
            "job_name": "x",
            "common_requirements": [
                {"id": "common_001", "description": "d", "importance": "must",
                 "source_evidence": "JD"}
            ],
            "alternative_tracks": [
                {"id": "general", "name": "g", "requirements": [
                    {"id": "general_001", "description": "d", "importance": "must",
                     "source_evidence": "JD"}
                ]}
            ],
            "education_requirements": {"minimum_degree": "本科及以上",
                                         "source_evidence": "JD"},
        }, ensure_ascii=False)}}]}

    sc, generated = ensure_scorecard(
        jd, scoring_dir=str(scoring_dir), policy=policy,
        config=cfg, http_post=fake_post,
    )
    assert generated is True
    assert (scoring_dir / "job_scorecard.json").is_file()


def test_ensure_scorecard_rejects_jd_hash_mismatch(tmp_path):
    """旧 scorecard 来自不同 JD → 拒绝复用。"""
    scoring_dir = tmp_path / "scoring"
    scoring_dir.mkdir()
    policy = load_policy()
    card = _good_card({"source_jd_hash": "different-jd-hash"})
    (scoring_dir / "job_scorecard.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    jd = {"jobName": "x", "encryptJobId": "e1", "bodyText": "本科", "formValues": []}
    with pytest.raises(ScorecardError):
        ensure_scorecard(jd, scoring_dir=str(scoring_dir), policy=policy)


def test_ensure_scorecard_rejects_policy_hash_mismatch(tmp_path):
    scoring_dir = tmp_path / "scoring"
    scoring_dir.mkdir()
    policy = load_policy()
    card = _good_card()
    card["policy_hash"] = "stale-hash"
    (scoring_dir / "job_scorecard.json").write_text(
        json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    jd = {"jobName": "x", "encryptJobId": "e1", "bodyText": "本科", "formValues": []}
    with pytest.raises(ScorecardError):
        ensure_scorecard(jd, scoring_dir=str(scoring_dir), policy=policy)
