# -*- coding: utf-8 -*-
"""boss_hr.application.scoring_service — score 命令业务逻辑。

C1：find_next_candidate
  - 校验 run + confirmed + new_resumes
  - manifest 不存在时调 prepare_scoring_inputs
  - 从 manifest + outputs/ 文件状态找下一位候选人
  - 每次只返回一位；不调 collect / score_resumes

C2：finalize_when_ready
  - 当所有 output 都齐（remaining=0）时
  - screening_results.json 已存在 → 幂等返回 scoring_complete
  - 否则调 collect_llm_scores
  - missing/invalid → 返回该候选人（waiting_llm），让 LLM 覆盖 output 再来
  - 全部有效 → 调 score_resumes
  - score_resumes 失败透传 exit_code

调用顺序：
  score → find_next_candidate（remaining > 0）
       → finalize_when_ready（remaining == 0）
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.contracts.results import CommandResult, ok, error
from boss_hr.contracts.errors import ExitCode, ErrorCode, UnifiedError
from boss_hr.adapters.legacy_runner import (
    run_legacy_cli, legacy_error, try_extract_blocked_message,
)


def _resolve_encrypt_job_id(cli_value: Optional[str]) -> Optional[str]:
    if cli_value:
        return cli_value
    return os.environ.get("BOSS_HR_ENCRYPT_JOB_ID")


def _require_run(job_name: str, eid: str, run_id: str) -> tuple[int, Optional[str]]:
    from shared.run_orchestrator import (
        RunOrchestrator,
        EXIT_CODE_RUN_NOT_FOUND,
        EXIT_CODE_RUN_JOB_MISMATCH,
    )
    from shared.output_manager import JobOutputManager

    orch = RunOrchestrator(job_name, encrypt_job_id=eid)
    try:
        orch.bind_existing_run(run_id)
    except FileNotFoundError:
        return EXIT_CODE_RUN_NOT_FOUND, f"run_id={run_id} 在岗位目录下不存在"
    except RuntimeError as e:
        return EXIT_CODE_RUN_JOB_MISMATCH, str(e)

    out = JobOutputManager(job_name, encrypt_job_id=eid, run_id=run_id, lazy=True)
    run_json_path = os.path.join(out.runs_dir, run_id, "run.json")
    if os.path.exists(run_json_path):
        try:
            with open(run_json_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not state.get("confirmed"):
                return 20, f"run_id={run_id} 尚未用户确认"
        except Exception:
            pass

    new_resumes_path = os.path.join(out.runs_dir, run_id, "process", "new_resumes.json")
    if not os.path.isfile(new_resumes_path):
        return 26, f"缺少 process/new_resumes.json：{new_resumes_path}"

    return 0, None


def _read_manifest(scoring_dir: str) -> Optional[dict]:
    path = os.path.join(scoring_dir, "manifest.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _ensure_manifest(job_name: str, eid: str, run_id: str,
                     scoring_dir: str) -> tuple[bool, Optional[dict]]:
    manifest = _read_manifest(scoring_dir)
    if manifest is not None:
        return True, manifest

    result = run_legacy_cli(
        "prepare_scoring_inputs",
        [
            "--job-name", job_name,
            "--encrypt-job-id", eid,
            "--run-id", run_id,
        ],
        timeout=60,
    )
    if result.returncode != 0:
        return False, None
    manifest = _read_manifest(scoring_dir)
    if manifest is None:
        return False, None
    return True, manifest


def _candidates_with_status(manifest: dict, scoring_dir: str) -> list[dict]:
    """返回每位候选人 + output 文件状态（不解 JSON 内容）。

    每项 schema：
      {geek_id, name, input_path, output_path, output_abs,
       has_output, output_size}

    合法性判断**不**在这里做。collect_llm_scores.py 是评分 output 合法性
    的唯一判断来源；scoring_service 只关心"文件是否已写盘"——
    has_output = (output 文件存在 且 size > 0)。

    空文件（size==0）视为"未评分"，返回该候选人 waiting_llm 让 LLM 重写。
    """
    out = []
    for entry in manifest.get("candidates", []):
        geek_id = entry.get("geek_id")
        output_rel = entry.get("output_path")
        output_abs = os.path.normpath(os.path.join(scoring_dir, output_rel)) if output_rel else None
        size = 0
        if output_abs and os.path.isfile(output_abs):
            try:
                size = os.path.getsize(output_abs)
            except OSError:
                size = 0
        has_output = size > 0
        out.append({
            "geek_id": geek_id,
            "name": entry.get("name", ""),
            "input_path": entry.get("input_path"),
            "output_path": output_rel,
            "output_abs": output_abs,
            "has_output": has_output,
            "output_size": size,
        })
    return out


def _find_next(candidates: list[dict]) -> Optional[dict]:
    """找 output 不存在的候选人；已存在但 invalid 也跳过（让 collect 报告）。"""
    for c in candidates:
        if not c["has_output"]:
            return c
    return None


def _count_remaining(candidates: list[dict]) -> int:
    return sum(1 for c in candidates if not c["has_output"])


def _infer_run_dirs(job_name: str, eid: str, run_id: str) -> tuple[str, str]:
    """返回 (runs_dir, process_dir) 绝对路径。"""
    from shared.output_manager import JobOutputManager
    out = JobOutputManager(job_name, encrypt_job_id=eid, run_id=run_id, lazy=True)
    runs_dir = out.runs_dir
    process_dir = os.path.join(runs_dir, run_id, "process")
    return runs_dir, process_dir


def _common_pre_check(job_name: str, eid: str, run_id: str) -> tuple[int, Optional[str], Optional[dict]]:
    """公共预校验：encrypt_job_id + run_id + confirmed + new_resumes + manifest。

    返回 (exit_code, error_msg, scoring_workspace)
    - exit_code = 0 且 scoring_workspace 非 None → OK
    - exit_code != 0 → error_msg 有效
    """
    rc, msg = _require_run(job_name, eid, run_id)
    if rc != 0:
        return rc, msg, None

    runs_dir, process_dir = _infer_run_dirs(job_name, eid, run_id)
    scoring_dir = os.path.join(process_dir, "scoring")

    ok_, manifest = _ensure_manifest(job_name, eid, run_id, scoring_dir)
    if not ok_ or manifest is None:
        return 26, "manifest.json 仍不存在（prepare 失败或 new_resumes 为空）", None

    candidates = _candidates_with_status(manifest, scoring_dir)
    return 0, None, {
        "runs_dir": runs_dir,
        "process_dir": process_dir,
        "scoring_dir": scoring_dir,
        "manifest": manifest,
        "candidates": candidates,
    }


def find_next_candidate(*, job_name: str, encrypt_job_id: Optional[str],
                         run_id: Optional[str]) -> CommandResult:
    """C1：找下一位候选人。

    当 remaining=0（所有 output 已写盘）→ 返回 status=waiting_llm 但
    data.candidate_id=null，提示"all outputs ready; run again to trigger finalize"。
    """
    eid = _resolve_encrypt_job_id(encrypt_job_id)
    if not eid:
        return error(error_obj=UnifiedError(code=ErrorCode.MISSING_ENCRYPT_JOB_ID,
                                           message="缺少 encrypt_job_id"),
                     run_id=run_id, exit_code=ExitCode.GENERIC)
    if not run_id:
        return error(error_obj=UnifiedError(code=ErrorCode.MISSING_RUN_ID,
                                           message="缺少 --run-id"),
                     exit_code=ExitCode.MISSING_RUN_ID)

    rc, msg, ws = _common_pre_check(job_name, eid, run_id)
    if rc != 0:
        return _map_require_error(rc, msg, run_id, eid, job_name)

    candidates = ws["candidates"]
    next_cand = _find_next(candidates)
    remaining = _count_remaining(candidates)

    if next_cand is None:
        # 所有 output 都存在 → 提示进入 C2 finalize
        return ok(
            status="waiting_llm",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "candidate_id": None,
                "name": None,
                "input_file": None,
                "output_file": None,
                "remaining": 0,
                "note": "all outputs ready; run again to trigger finalize",
            },
            next_action="score_candidate_then_repeat",
        )

    input_path = (os.path.normpath(os.path.join(ws["scoring_dir"], next_cand["input_path"]))
                  if next_cand.get("input_path") else None)
    return ok(
        status="waiting_llm",
        run_id=run_id, encrypt_job_id=eid, job_name=job_name,
        data={
            "candidate_id": next_cand["geek_id"],
            "name": next_cand["name"],
            "input_file": input_path,
            "output_file": next_cand["output_abs"],
            "remaining": remaining,
        },
        next_action="score_candidate_then_repeat",
    )


def finalize_when_ready(*, job_name: str, encrypt_job_id: Optional[str],
                        run_id: Optional[str]) -> CommandResult:
    """C2：所有 output 都齐时调 collect + score_resumes 完成评分。

    流程：
      1) 已有 screening_results.json → 直接返回 scoring_complete（幂等）
      2) 调 collect_llm_scores.py
         - collect 失败 → 透传 exit_code（不调 score_resumes）
         - collect 标记 invalid → 返回该候选人（waiting_llm + validation_error）
         - collect 标记 missing → 返回该候选人（waiting_llm + note）
      3) 全部有效 → 调 score_resumes.py
         - score_resumes 失败 → 透传 exit_code
         - 成功 → 返回 scoring_complete + scoring_results_file 路径
    """
    eid = _resolve_encrypt_job_id(encrypt_job_id)
    if not eid:
        return error(error_obj=UnifiedError(code=ErrorCode.MISSING_ENCRYPT_JOB_ID,
                                           message="缺少 encrypt_job_id"),
                     run_id=run_id, exit_code=ExitCode.GENERIC)
    if not run_id:
        return error(error_obj=UnifiedError(code=ErrorCode.MISSING_RUN_ID,
                                           message="缺少 --run-id"),
                     exit_code=ExitCode.MISSING_RUN_ID)

    rc, msg, ws = _common_pre_check(job_name, eid, run_id)
    if rc != 0:
        return _map_require_error(rc, msg, run_id, eid, job_name)

    runs_dir = ws["runs_dir"]
    process_dir = ws["process_dir"]
    scoring_dir = ws["scoring_dir"]
    candidates = ws["candidates"]

    # 1) screening_results.json 幂等
    from shared.output_manager import JobOutputManager
    out = JobOutputManager(job_name, encrypt_job_id=eid, run_id=run_id, lazy=True)
    screening_results_path = out.screening_results_path
    if os.path.isfile(screening_results_path):
        try:
            with open(screening_results_path, "r", encoding="utf-8") as f:
                existing = json.load(f)
            scored = len(existing.get("candidates", []))
        except Exception:
            scored = 0
        return ok(
            status="scoring_complete",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={"scored": scored, "screening_results_file": str(screening_results_path)},
            next_action="report",
        )

    # 还有候选人没评分完 → 不是 finalize 时机
    remaining = _count_remaining(candidates)
    if remaining > 0:
        # C2 被显式调但还没齐：返回 waiting_llm + 第一位候选人
        next_cand = _find_next(candidates)
        return ok(
            status="waiting_llm",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "candidate_id": next_cand["geek_id"] if next_cand else None,
                "name": next_cand["name"] if next_cand else None,
                "input_file": (os.path.normpath(os.path.join(scoring_dir, next_cand["input_path"]))
                                if next_cand and next_cand.get("input_path") else None),
                "output_file": next_cand["output_abs"] if next_cand else None,
                "remaining": remaining,
                "note": "not all outputs ready; finalize cannot proceed",
            },
            next_action="score_candidate_then_repeat",
        )

    # 2) 调 collect
    collect_result = run_legacy_cli(
        "collect_llm_scores",
        [
            "--job-name", job_name,
            "--encrypt-job-id", eid,
            "--run-id", run_id,
        ],
        timeout=60,
    )

    if collect_result.returncode != 0:
        better_msg = try_extract_blocked_message(collect_result.stdout)
        unified = legacy_error(collect_result)
        if better_msg:
            unified = UnifiedError(code=unified.code, message=better_msg,
                                   subprocess_returncode=unified.subprocess_returncode)
        return error(error_obj=unified,
                     run_id=run_id, encrypt_job_id=eid, job_name=job_name,
                     exit_code=ExitCode(collect_result.returncode))

    # 解析 collect stdout JSON
    collect_payload = None
    for line in reversed((collect_result.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                collect_payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if collect_payload is None:
        return error(error_obj=UnifiedError(
            code=ErrorCode.INTERNAL,
            message="collect 输出无 JSON 可解析",
        ), run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode.INTERNAL)

    invalid_list = collect_payload.get("invalid", []) or []
    missing_list = collect_payload.get("missing", []) or []

    if invalid_list:
        # 返回第一位 invalid 候选人；让 LLM 覆盖 output 后再 finalize
        first = invalid_list[0]
        out_path = _find_output_path(candidates, first["geek_id"])
        return ok(
            status="waiting_llm",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "candidate_id": first["geek_id"],
                "name": first.get("name"),
                "input_file": _find_input_path(scoring_dir, candidates, first["geek_id"]),
                "output_file": out_path,
                "remaining": _count_invalid_or_missing(candidates, invalid_list, missing_list),
                "validation_error": first.get("errors") or first.get("error"),
                "note": "invalid output; overwrite output_file with valid score, then run score again",
            },
            next_action="score_candidate_then_repeat",
        )

    if missing_list:
        # missing 是真正的 output 不存在 → 走 C1 逻辑返回那位候选人
        first = missing_list[0]
        return ok(
            status="waiting_llm",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "candidate_id": first["geek_id"],
                "name": first.get("name"),
                "input_file": _find_input_path(scoring_dir, candidates, first["geek_id"]),
                "output_file": _find_output_path(candidates, first["geek_id"]),
                "remaining": len(missing_list),
                "note": "missing output; LLM must write score to this path",
            },
            next_action="score_candidate_then_repeat",
        )

    # 3) 全部有效 → 调 score_resumes
    score_result = run_legacy_cli(
        "score_resumes",
        [
            "--job-name", job_name,
            "--encrypt-job-id", eid,
            "--run-id", run_id,
        ],
        timeout=600,
    )

    if score_result.returncode != 0:
        better_msg = try_extract_blocked_message(score_result.stdout)
        unified = legacy_error(score_result)
        if better_msg:
            unified = UnifiedError(code=unified.code, message=better_msg,
                                   subprocess_returncode=unified.subprocess_returncode)
        return error(error_obj=unified,
                     run_id=run_id, encrypt_job_id=eid, job_name=job_name,
                     exit_code=ExitCode(score_result.returncode))

    # 以实际生成的结果计数，不能把 collect 数量当作最终评分数量。
    if not os.path.isfile(screening_results_path):
        return error(error_obj=UnifiedError(
            code=ErrorCode.INTERNAL,
            message="评分脚本未生成 screening_results.json，请检查本轮候选人是否已评分",
        ), run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode.INTERNAL)
    with open(screening_results_path, "r", encoding="utf-8") as f:
        scored = len(json.load(f).get("candidates", []))

    # 成功
    return ok(
        status="scoring_complete",
        run_id=run_id, encrypt_job_id=eid, job_name=job_name,
        data={
            "scored": scored,
            "screening_results_file": str(out.screening_results_path),
        },
        next_action="report",
    )


def _map_require_error(rc: int, msg: str, run_id: str, eid: str,
                       job_name: str) -> CommandResult:
    """_require_run 返回的 rc → CommandResult。"""
    if rc == 23:
        code = ErrorCode.RUN_NOT_FOUND
    elif rc == 24:
        code = ErrorCode.JOB_MISMATCH
    elif rc == 20:
        code = ErrorCode.AWAITING_CONFIRMATION
    else:
        code = ErrorCode.INTERNAL
    return error(
        error_obj=UnifiedError(code=code, message=msg or "前置校验失败"),
        run_id=run_id, encrypt_job_id=eid, job_name=job_name,
        exit_code=ExitCode(rc),
    )


def _find_output_path(candidates: list[dict], geek_id: str) -> Optional[str]:
    for c in candidates:
        if c["geek_id"] == geek_id:
            return c["output_abs"]
    return None


def _find_input_path(scoring_dir: str, candidates: list[dict],
                     geek_id: str) -> Optional[str]:
    for c in candidates:
        if c["geek_id"] == geek_id and c.get("input_path"):
            return os.path.normpath(os.path.join(scoring_dir, c["input_path"]))
    return None


def _count_invalid_or_missing(candidates, invalid, missing) -> int:
    bad_geek_ids = {x["geek_id"] for x in invalid} | {x["geek_id"] for x in missing}
    return sum(1 for c in candidates if c["geek_id"] in bad_geek_ids)


# ============================================================
# 统一入口：score 命令 dispatcher
# ============================================================

def run_score(*, job_name: str, encrypt_job_id: Optional[str],
              run_id: Optional[str]) -> CommandResult:
    """score 命令业务入口；按状态自动选 C1 (协调) 或 C2 (finalize)。

    判断条件：manifest 所有候选人的 output 文件是否都已存在。
    """
    eid = _resolve_encrypt_job_id(encrypt_job_id)
    if not eid:
        return error(error_obj=UnifiedError(code=ErrorCode.MISSING_ENCRYPT_JOB_ID,
                                           message="缺少 encrypt_job_id"),
                     run_id=run_id, exit_code=ExitCode.GENERIC)
    if not run_id:
        return error(error_obj=UnifiedError(code=ErrorCode.MISSING_RUN_ID,
                                           message="缺少 --run-id"),
                     exit_code=ExitCode.MISSING_RUN_ID)

    rc, msg, ws = _common_pre_check(job_name, eid, run_id)
    if rc != 0:
        return _map_require_error(rc, msg, run_id, eid, job_name)

    remaining = _count_remaining(ws["candidates"])
    if remaining > 0:
        return find_next_candidate(job_name=job_name,
                                   encrypt_job_id=eid, run_id=run_id)
    return finalize_when_ready(job_name=job_name,
                               encrypt_job_id=eid, run_id=run_id)


__all__ = ["run_score", "find_next_candidate", "finalize_when_ready"]
