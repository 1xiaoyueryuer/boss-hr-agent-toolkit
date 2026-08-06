# -*- coding: utf-8 -*-
"""boss_hr.scoring.runner — boss-hr score v2 状态机入口。

单次 `boss-hr score` 的行为：
  1. 校验 run / manifest / 已 confirmed / new_resumes 存在；
  2. 确保本 run 有合法 `job_scorecard.json`（hash 一致 + policy_hash 匹配）；
  3. 找下一位没有合法 candidate output 的候选人：
       a. 读该候选人的净化输入（scoring/inputs/candidate_<geek_id>.json）；
       b. 调 LLM 评估 + Python 算分 + A/B/C 决策；
       c. 原子写 scoring/outputs/candidate_<geek_id>.json（schema v2）；
       d. 状态：candidate_scored。
  4. 全部候选人都有合法 output → finalize_when_ready_v2（生成
     screening_results.json v2）→ scoring_complete。

返回 dict（与 boss_hr.application.scoring_service 兼容）：
  - status="candidate_scored" / "scoring_complete" / "blocked" / "error"
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from .policy import load_policy, policy_hash
from .scorecard import (
    ensure_scorecard, validate_scorecard, source_jd_hash,
    load_existing_scorecard, all_requirement_ids, ScorecardError,
    scorecard_path,
)
from .evaluator import (
    evaluate_candidate, finalize_evaluation, validate_assessments,
    validate_candidate_output, write_candidate_output,
)
from .llm_client import (
    LLMClientError, load_config_from_env, load_llm_config, LLMConfig,
)
from .decision import determine_decision, DECISION_LABELS


# ============================================================
# 测试 hook 静态白名单（不可由环境变量扩展）
#
# 设计原则：防止任何模块名（包括通过 env 注入的"看似白名单"）被 import。
# 真实唯一允许的测试 hook 是仓库内 `tests/cli/_mock_llm_hook.py`。
# 任何其他值（含前缀/后缀变体、未知模块、env 拼接）都拒绝 import。
# ============================================================
_ALLOWED_TEST_HOOKS = frozenset({
    "tests.cli._mock_llm_hook",
})


def _resolve_test_hook(http_post=None):
    """返回 (http_post, changed) —— 若 env 指定了已通过 allowlist 验证的
    测试 hook，则用 import 的 http_post_override 替换；
    否则维持调用方传入的 http_post（或 None，让上层走默认 _http_post）。
    """
    if http_post is not None:
        # 调用方已注入 → 不再读 env（避免覆盖依赖注入）
        return http_post, False
    import os as _os
    import sys as _sys
    hook_mod = _os.environ.get("BOSS_HR_LLM_TEST_HOOK")
    if not hook_mod:
        return None, False
    if hook_mod not in _ALLOWED_TEST_HOOKS:
        # 安全第一：不抛错（不阻塞生产路径），仅 stderr 警告。
        # 警告内容只含模块名 + 提示信息，**不含** API key / 模型名 / 任何配置。
        _sys.stderr.write(
            f"[boss-hr] BOSS_HR_LLM_TEST_HOOK='{hook_mod}' 未在静态白名单中，"
            " 已忽略；继续使用默认 http_post。\n"
        )
        _sys.stderr.flush()
        return None, False
    # 在白名单中：import + 拿 http_post_override
    try:
        import importlib
        _hook = importlib.import_module(hook_mod)
        return _hook.http_post_override, True
    except Exception as e:
        _sys.stderr.write(
            f"[boss-hr] BOSS_HR_LLM_TEST_HOOK 加载失败：{type(e).__name__}；"
            " 继续使用默认 http_post。\n"
        )
        _sys.stderr.flush()
        return None, False


# ============================================================
# 公共结果包装
# ============================================================

def _blocked(
    message: str, *, run_id: Optional[str] = None, code: str = "BLOCKED",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "command": "score",
        "status": "blocked",
        "run_id": run_id,
        "error": {"code": code, "message": message},
        "data": data or {},
    }


def _error(
    message: str, *, run_id: Optional[str] = None, code: str = "INTERNAL",
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    return {
        "ok": False,
        "command": "score",
        "status": "error",
        "run_id": run_id,
        "error": {"code": code, "message": message},
        "data": data or {},
    }


def _ok_candidate_scored(
    *, run_id: str, candidate: Dict[str, Any], output: Dict[str, Any],
    remaining: int, eid: str, job_name: str,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "command": "score",
        "status": "candidate_scored",
        "run_id": run_id,
        "encrypt_job_id": eid,
        "job_name": job_name,
        "data": {
            "candidate_id": candidate.get("geek_id"),
            "name": candidate.get("name"),
            "decision": output.get("decision"),
            "decision_label": output.get("decision_label"),
            "final_score": output.get("final_score"),
            "job_match_score": output.get("job_match_score"),
            "education_score": output.get("education_score"),
            "best_track": output.get("best_track"),
            "remaining": remaining,
            "output_file": str(
                Path(_HERE).parent.parent / "runs"  # placeholder
            ),
        },
        "next_action": "repeat_score",
    }


def _ok_scoring_complete(
    *, run_id: str, eid: str, job_name: str, screening_file: str, scored: int,
) -> Dict[str, Any]:
    return {
        "ok": True,
        "command": "score",
        "status": "scoring_complete",
        "run_id": run_id,
        "encrypt_job_id": eid,
        "job_name": job_name,
        "data": {
            "scored": scored,
            "screening_results_file": screening_file,
        },
        "next_action": "report",
    }


# ============================================================
# 路径 / manifest 工具
# ============================================================

def _scoring_dir_for(scoring_root: str) -> str:
    return scoring_root


def _load_jd(job_detail_path: str) -> Dict[str, Any]:
    if not os.path.isfile(job_detail_path):
        return {}
    try:
        with open(job_detail_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _load_manifest(scoring_dir: str) -> Dict[str, Any]:
    path = os.path.join(scoring_dir, "manifest.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _candidates(manifest: Dict[str, Any], scoring_dir: str) -> List[Dict[str, Any]]:
    """每位候选人 + 输入/输出绝对路径 + 是否有合法 output。"""
    out: List[Dict[str, Any]] = []
    for entry in manifest.get("candidates", []) or []:
        geek_id = entry.get("geek_id")
        input_rel = entry.get("input_path") or ""
        output_rel = entry.get("output_path") or ""
        input_abs = (
            os.path.normpath(os.path.join(scoring_dir, input_rel))
            if input_rel else None
        )
        output_abs = (
            os.path.normpath(os.path.join(scoring_dir, output_rel))
            if output_rel else None
        )
        out.append({
            "geek_id": geek_id,
            "name": entry.get("name", ""),
            "input_path": input_rel,
            "output_path": output_rel,
            "input_abs": input_abs,
            "output_abs": output_abs,
            "job_id": entry.get("job_id"),
            "source_index": entry.get("source_index"),
        })
    return out


def _has_legal_output(
    output_abs: Optional[str],
    *,
    scorecard: Dict[str, Any],
    expected_policy_hash: str,
    expected_geek_id: str,
    expected_job_id: str,
) -> bool:
    """output 文件存在 + 通过 v2 schema 校验。"""
    if not output_abs or not os.path.isfile(output_abs):
        return False
    try:
        with open(output_abs, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    errs = validate_candidate_output(
        data,
        scorecard=scorecard,
        expected_policy_hash=expected_policy_hash,
        expected_geek_id=expected_geek_id,
        expected_job_id=expected_job_id,
    )
    return not errs


# ============================================================
# screening_results.json v2 收尾
# ============================================================

def _build_screening_results_v2(
    scorecard: Dict[str, Any],
    candidates_outputs: List[Dict[str, Any]],
    *,
    run_id: str,
    job_name: str,
    policy: Dict[str, Any],
) -> Dict[str, Any]:
    """从合法 candidate outputs 组装 screening_results.json v2。"""
    A = [c for c in candidates_outputs if c.get("decision") == "A"]
    B = [c for c in candidates_outputs if c.get("decision") == "B"]
    C = [c for c in candidates_outputs if c.get("decision") == "C"]

    def _sort_key(c: Dict[str, Any]) -> float:
        return -float(c.get("final_score") or 0.0)

    A.sort(key=_sort_key)
    B.sort(key=_sort_key)
    C.sort(key=_sort_key)
    ordered = A + B + C

    summary = {
        "total": len(candidates_outputs),
        "A": len(A),
        "B": len(B),
        "C": len(C),
        "contact_recommended": sum(
            1 for c in candidates_outputs if c.get("contact_recommended")
        ),
    }

    out_candidates: List[Dict[str, Any]] = []
    for rank, c in enumerate(ordered, start=1):
        out_candidates.append({
            "rank": rank,
            "name": c.get("name", ""),
            "geek_id": c.get("geek_id", ""),
            "best_track": c.get("best_track"),
            "job_match_score": c.get("job_match_score"),
            "education_score": c.get("education_score"),
            "education_tier": c.get("education_tier"),
            "education_reason": c.get("education_reason"),
            "education_review_required": c.get("education_review_required", False),
            "education_hard_pass": c.get("education_hard_pass", True),
            "final_score": c.get("final_score"),
            "decision": c.get("decision"),
            "decision_label": c.get("decision_label"),
            "decision_reasons": c.get("decision_reasons", []),
            "highlights": c.get("highlights", []),
            "concerns": c.get("concerns", []),
            "verification_questions": c.get("verification_questions", []),
            "contact_recommended": c.get("contact_recommended", False),
            "common_assessments": c.get("common_assessments", []),
            "track_assessments": c.get("track_assessments", []),
        })

    return {
        "schema_version": "2.0",
        "policy_version": policy["policy_version"],
        "policy_hash": policy_hash(policy),
        "job_scorecard_hash": scorecard.get("source_jd_hash", ""),
        "job_name": job_name,
        "run_id": run_id,
        "summary": summary,
        "scorecard": scorecard,
        "candidates": out_candidates,
    }


# ============================================================
# 主入口
# ============================================================

def _resolve_llm_config(
    llm_config: Optional[LLMConfig],
    llm_config_path,
):
    """生产入口：调用方注入的 llm_config 优先；否则走
    load_llm_config（env > ~/.boss-hr/llm.env）。

    返回 (config_or_none, error_dict_or_none)。
    """
    if llm_config is not None:
        return llm_config, None
    try:
        return load_llm_config(config_path=llm_config_path), None
    except LLMClientError as e:
        return None, _error(
            f"LLM 配置缺失：{e}", code="LLM_CONFIG_ERROR",
        )


def run_score_v2(
    *,
    job_name: str,
    encrypt_job_id: str,
    run_id: str,
    llm_config: Optional[LLMConfig] = None,
    llm_config_path=None,
    sleep_fn=None,
    http_post=None,
    write_fn=None,
) -> Dict[str, Any]:
    """boss-hr score v2 主入口。

    llm_config / llm_config_path / sleep_fn / http_post / write_fn 由
    application 层注入；测试中可 monkeypatch 全部。
    """
    if not encrypt_job_id:
        return _blocked("缺少 encrypt_job_id", run_id=run_id)
    if not run_id:
        return _blocked("缺少 run_id", run_id=run_id)

    # 1) run 存在 + confirmed + new_resumes 存在
    from shared.run_orchestrator import RunOrchestrator, EXIT_CODE_RUN_NOT_FOUND
    from shared.output_manager import JobOutputManager

    orch = RunOrchestrator(job_name, encrypt_job_id=encrypt_job_id)
    try:
        orch.bind_existing_run(run_id)
    except FileNotFoundError:
        return _blocked(
            f"run_id={run_id} 在岗位目录下不存在",
            run_id=run_id, code="RUN_NOT_FOUND",
        )
    except RuntimeError as e:
        return _blocked(str(e), run_id=run_id, code="JOB_MISMATCH")

    out = JobOutputManager(
        job_name, encrypt_job_id=encrypt_job_id, run_id=run_id, lazy=True,
    )
    run_dir = os.path.join(out.runs_dir, run_id)
    run_json_path = os.path.join(run_dir, "run.json")
    process_dir = os.path.join(run_dir, "process")
    new_resumes_path = os.path.join(process_dir, "new_resumes.json")
    job_detail_path = os.path.join(process_dir, "job_detail.json")
    scoring_dir = os.path.join(process_dir, "scoring")

    # confirmed？
    confirmed = False
    if os.path.isfile(run_json_path):
        try:
            with open(run_json_path, "r", encoding="utf-8") as f:
                st = json.load(f)
            confirmed = bool(st.get("confirmed"))
        except Exception:
            confirmed = False
    if not confirmed:
        return _blocked(
            f"run_id={run_id} 尚未用户确认",
            run_id=run_id, code="AWAITING_CONFIRMATION",
        )
    if not os.path.isfile(new_resumes_path):
        return _blocked(
            f"缺少 process/new_resumes.json：{new_resumes_path}",
            run_id=run_id, code="MISSING_INPUT",
        )
    if not os.path.isfile(job_detail_path):
        return _blocked(
            f"缺少 process/job_detail.json：{job_detail_path}",
            run_id=run_id, code="MISSING_INPUT",
        )

    # 2) 策略 + scorecard
    try:
        policy = load_policy()
    except Exception as e:
        return _error(f"评分策略加载失败：{e}", run_id=run_id, code="POLICY_ERROR")

    jd = _load_jd(job_detail_path)
    jd_hash_now = source_jd_hash(jd)
    expected_ph = policy_hash(policy)

    # 先看 scorecard 是否已合法（避免无谓加载 LLM）
    existing_sc = load_existing_scorecard(scoring_dir)
    scorecard_valid = False
    import sys as _sys
    _sys.stderr.write(f"[runner] sc_dir={scoring_dir} exists={existing_sc is not None}\n")
    _sys.stderr.write(f"[runner] jd_hash_now={jd_hash_now} stored_hash={existing_sc.get('source_jd_hash') if existing_sc else 'N/A'}\n")
    _sys.stderr.write(f"[runner] expected_ph={expected_ph} stored_ph={existing_sc.get('policy_hash') if existing_sc else 'N/A'}\n")
    _sys.stderr.flush()
    if existing_sc is not None:
        try:
            validate_scorecard(existing_sc, expected_policy_hash=expected_ph)
            _sys.stderr.write(f"[runner] validate_scorecard OK\n")
            _sys.stderr.flush()
            if existing_sc.get("source_jd_hash") == jd_hash_now:
                scorecard_valid = True
            else:
                _sys.stderr.write(f"[runner] jd_hash mismatch: stored={existing_sc.get('source_jd_hash')} current={jd_hash_now}\n")
                _sys.stderr.flush()
        except ScorecardError as e:
            _sys.stderr.write(f"[runner] scorecard validation failed: {e}\n")
            _sys.stderr.flush()
            scorecard_valid = False
        except Exception as e:
            _sys.stderr.write(f"[runner] scorecard validation EXC: {type(e).__name__}: {e}\n")
            _sys.stderr.flush()
            scorecard_valid = False

    if not scorecard_valid:
        # 真正需要调 LLM：先加载 config
        if llm_config is None:
            llm_config, err = _resolve_llm_config(llm_config, llm_config_path)
            if err is not None:
                return err

    # 预加载 LLM config（即使 scorecard 命中，_score_one 仍可能需要）
    if llm_config is None:
        llm_config, _err = _resolve_llm_config(llm_config, llm_config_path)
        # finalize 路径不需要 LLM；让 _score_one 自行报错

    # 测试 hook（**仅测试模式**）：
    #   BOSS_HR_LLM_TEST_HOOK 设值时，必须在代码内不可由环境变量扩展的
    #   静态白名单 `_ALLOWED_TEST_HOOKS` 中才生效；
    #   任何其他值（含前缀/后缀/未知模块/老 ALLOWLIST env）一律忽略。
    # 该机制**仅**用于子进程测试套件；README / SKILL / 手工使用说明均不
    # 提及此变量；生产部署永远不要设。
    http_post, _hook_loaded = _resolve_test_hook(http_post)

    # ensure_scorecard 内部会调 LLM（如需生成）；hash 一致时直接复用
    _sys.stderr.write(f"[runner] before ensure_scorecard: llm_config={llm_config is not None}\n")
    _sys.stderr.flush()
    scorecard = None
    if scorecard_valid and existing_sc is not None:
        # 直接复用，不调 LLM
        scorecard = existing_sc
    else:
        try:
            scorecard, _ = ensure_scorecard(
                jd,
                scoring_dir=scoring_dir,
                policy=policy,
                config=llm_config,
                sleep_fn=sleep_fn,
                http_post=http_post,
                write_fn=write_fn,
            )
        except ScorecardError as e:
            return _error(
                f"scorecard 校验失败：{e}", run_id=run_id, code="SCORECARD_INVALID",
            )
        except LLMClientError as e:
            return _error(
                f"scorecard 生成失败：{e}", run_id=run_id,
                code=getattr(e, "code", "LLM_ERROR"),
            )

    # 3) manifest（缺则自调 prepare_scoring_inputs）
    manifest = _load_manifest(scoring_dir)
    if not manifest:
        # 调 cli_runner → prepare_scoring_inputs
        try:
            from shared.cli_runner import run_python_cli
            proc = run_python_cli(
                "prepare_scoring_inputs",
                ["--job-name", job_name,
                 "--encrypt-job-id", encrypt_job_id,
                 "--run-id", run_id],
                timeout=60,
            )
            if proc.returncode != 0:
                return _error(
                    f"prepare_scoring_inputs 失败 (rc={proc.returncode})：{proc.stderr or proc.stdout}",
                    run_id=run_id, code="MISSING_INPUT",
                )
        except Exception as e:
            return _error(
                f"prepare_scoring_inputs 异常：{e}", run_id=run_id, code="MISSING_INPUT",
            )
        manifest = _load_manifest(scoring_dir)
        if not manifest:
            return _blocked(
                f"manifest.json 仍不存在：{scoring_dir}/manifest.json",
                run_id=run_id, code="MISSING_INPUT",
            )
    cand_list = _candidates(manifest, scoring_dir)
    if not cand_list:
        return _blocked(
            f"manifest 中没有候选人：{scoring_dir}/manifest.json",
            run_id=run_id, code="MISSING_INPUT",
        )

    # 4) 找下一位需要评分的候选人
    remaining = 0
    next_cand: Optional[Dict[str, Any]] = None
    for c in cand_list:
        is_legal = _has_legal_output(
            c.get("output_abs"),
            scorecard=scorecard,
            expected_policy_hash=expected_ph,
            expected_geek_id=c.get("geek_id") or "",
            expected_job_id=encrypt_job_id,
        )
        import sys as _ss
        if not is_legal:
            try:
                with open(c.get("output_abs"), "r", encoding="utf-8") as f:
                    data = json.load(f)
                errs = validate_candidate_output(
                    data, scorecard=scorecard,
                    expected_policy_hash=expected_ph,
                    expected_geek_id=c.get("geek_id") or "",
                    expected_job_id=encrypt_job_id,
                )
                _ss.stderr.write(f"[runner] cand {c.get('geek_id')} errs={errs}\n")
                _ss.stderr.flush()
            except Exception as e:
                _ss.stderr.write(f"[runner] cand {c.get('geek_id')} exc={e}\n")
                _ss.stderr.flush()
        if not is_legal:
            remaining += 1
            if next_cand is None:
                next_cand = c

    if next_cand is None:
        # 全部合法 → finalize_when_ready_v2
        import sys as _s3
        _s3.stderr.write(f"[runner] all legal, finalizing\n")
        _s3.stderr.flush()
        screening_path = os.path.join(process_dir, "screening_results.json")
        if os.path.isfile(screening_path):
            try:
                with open(screening_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                scored = len(existing.get("candidates", []) or [])
            except Exception:
                scored = 0
            return _ok_scoring_complete(
                run_id=run_id, eid=encrypt_job_id, job_name=job_name,
                screening_file=screening_path, scored=scored,
            )
        # 收集所有合法 output
        valid_outputs: List[Dict[str, Any]] = []
        for c in cand_list:
            output_abs = c.get("output_abs")
            if not output_abs or not os.path.isfile(output_abs):
                continue
            try:
                with open(output_abs, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            errs = validate_candidate_output(
                data,
                scorecard=scorecard,
                expected_policy_hash=expected_ph,
                expected_geek_id=c.get("geek_id") or "",
                expected_job_id=encrypt_job_id,
            )
            if errs:
                continue
            valid_outputs.append(data)
        result = _build_screening_results_v2(
            scorecard, valid_outputs,
            run_id=run_id, job_name=job_name, policy=policy,
        )
        from .llm_client import atomic_write_json
        atomic_write_json(screening_path, result)
        return _ok_scoring_complete(
            run_id=run_id, eid=encrypt_job_id, job_name=job_name,
            screening_file=screening_path, scored=len(valid_outputs),
        )

    # 5) 评这位候选人
    # 只有真要调 LLM 才需要 config（如果 output 已存在并合法但被前面已判定为 missing,
    # 这里需要重评 → 调 LLM）
    if llm_config is None:
        llm_config, err = _resolve_llm_config(llm_config, llm_config_path)
        if err is not None:
            return err
    http_post, _hook_loaded = _resolve_test_hook(http_post)
    return _score_one(
        next_cand=next_cand,
        scorecard=scorecard,
        policy=policy,
        llm_config=llm_config,
        run_id=run_id,
        eid=encrypt_job_id,
        job_name=job_name,
        remaining=remaining,
        sleep_fn=sleep_fn,
        http_post=http_post,
        write_fn=write_fn,
    )


def _score_one(
    *,
    next_cand: Dict[str, Any],
    scorecard: Dict[str, Any],
    policy: Dict[str, Any],
    llm_config: LLMConfig,
    run_id: str,
    eid: str,
    job_name: str,
    remaining: int,
    sleep_fn=None,
    http_post=None,
    write_fn=None,
) -> Dict[str, Any]:
    """调 LLM + Python 算分 + 决策 + 原子写 output。"""
    input_abs = next_cand.get("input_abs")
    output_abs = next_cand.get("output_abs")
    if not input_abs or not os.path.isfile(input_abs):
        return _error(
            f"候选人 {next_cand.get('geek_id')} 缺净化输入：{input_abs}",
            run_id=run_id, code="MISSING_INPUT",
        )
    if not output_abs:
        return _error(
            f"候选人 {next_cand.get('geek_id')} 缺 output 路径",
            run_id=run_id, code="MISSING_INPUT",
        )

    try:
        with open(input_abs, "r", encoding="utf-8") as f:
            resume = json.load(f)
    except Exception as e:
        return _error(
            f"读候选人输入失败 {input_abs}：{e}",
            run_id=run_id, code="INVALID_INPUT",
        )

    # 强制 geek_id / job_id 一致
    resume_geek_id = str(
        resume.get("geek_id") or resume.get("encrypt_geek_id") or ""
    )
    resume_job_id = str(
        resume.get("job_id") or resume.get("encrypt_job_id") or ""
    )
    if resume_geek_id != (next_cand.get("geek_id") or ""):
        return _error(
            f"候选人 {next_cand.get('geek_id')} 输入里的 geek_id={resume_geek_id!r} 与 manifest 不一致",
            run_id=run_id, code="GEEK_ID_MISMATCH",
        )
    if resume_job_id and resume_job_id != eid:
        return _error(
            f"候选人 {next_cand.get('geek_id')} 输入里的 job_id={resume_job_id!r} 与当前 {eid!r} 不一致",
            run_id=run_id, code="JOB_ID_MISMATCH",
        )

    # 调 LLM
    try:
        evaluation = evaluate_candidate(
            scorecard, resume, policy=policy,
            config=llm_config, sleep_fn=sleep_fn, http_post=http_post,
        )
    except LLMClientError as e:
        return _error(
            f"LLM 评估失败：{e}", run_id=run_id,
            code=getattr(e, "code", "LLM_ERROR"),
        )
    except Exception as e:
        return _error(
            f"评估失败：{e}", run_id=run_id, code="EVALUATION_ERROR",
        )

    # 决策
    decision_block = determine_decision(evaluation, scorecard, policy=policy)

    # 写盘
    try:
        if write_fn is None:
            from .llm_client import atomic_write_json
            write_fn = atomic_write_json
        payload = {
            "schema_version": "2.0",
            "policy_version": policy["policy_version"],
            "policy_hash": policy_hash(policy),
            "job_scorecard_hash": scorecard.get("source_jd_hash", ""),
            "name": resume.get("name", ""),
            "geek_id": resume_geek_id,
            "job_id": eid,
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
            "decision": decision_block["decision"],
            "decision_label": decision_block["decision_label"],
            "decision_reasons": decision_block["decision_reasons"],
            "highlights": decision_block["highlights"],
            "concerns": decision_block["concerns"],
            "verification_questions": decision_block["verification_questions"],
            "contact_recommended": decision_block["contact_recommended"],
            "must_not_met": evaluation["must_not_met"],
            "core_not_met": evaluation["core_not_met"],
            "core_partial": evaluation["core_partial"],
            "unverified_significant": evaluation["unverified_significant"],
        }
        write_fn(output_abs, payload)
    except Exception as e:
        return _error(
            f"写候选人 output 失败 {output_abs}：{e}",
            run_id=run_id, code="WRITE_FAILED",
        )

    payload["data"] = {
        "candidate_id": payload["geek_id"],
        "name": payload["name"],
        "decision": payload["decision"],
        "decision_label": payload["decision_label"],
        "final_score": payload["final_score"],
        "job_match_score": payload["job_match_score"],
        "education_score": payload["education_score"],
        "best_track": payload["best_track"],
        "remaining": remaining,
    }
    payload["status"] = "candidate_scored"
    payload["next_action"] = "repeat_score"
    payload["encrypt_job_id"] = eid
    payload["job_name"] = job_name
    payload["run_id"] = run_id
    payload["ok"] = True
    payload["command"] = "score"
    return payload


__all__ = [
    "run_score_v2",
    "validate_candidate_output",
]
