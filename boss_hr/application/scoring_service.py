# -*- coding: utf-8 -*-
"""boss_hr.application.scoring_service — score 命令业务逻辑（v2）。

`boss-hr score` 唯一公开入口；每次调用：
  - 当仍有未评分候选人：返回 `candidate_scored`，智能体重新执行完全相同命令。
  - 当全部候选人完成：返回 `scoring_complete`，进入 `report`。

旧 v1 `waiting_llm` 协议退役。LLM 评估在 `boss_hr.scoring.runner` 内部完成；
父 agent 不再读 `input_file`、不再写 `output_file`、也不再调自己的模型。
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


# v2 状态码 → ExitCode 映射
_V2_STATUS_MAP = {
    "BLOCKED": ExitCode.MISSING_INPUT,
    "RUN_NOT_FOUND": ExitCode.RUN_NOT_FOUND,
    "JOB_MISMATCH": ExitCode.RUN_JOB_MISMATCH,
    "AWAITING_CONFIRMATION": ExitCode.AWAITING_CONFIRMATION,
    "MISSING_INPUT": ExitCode.MISSING_INPUT,
    "INTERNAL": ExitCode.INTERNAL,
    "POLICY_ERROR": ExitCode.INTERNAL,
    "LLM_CONFIG_ERROR": ExitCode.INTERNAL,
    "LLM_ERROR": ExitCode.INTERNAL,
    "SCORECARD_INVALID": ExitCode.INTERNAL,
    "EVALUATION_ERROR": ExitCode.INTERNAL,
    "WRITE_FAILED": ExitCode.INTERNAL,
    "INVALID_INPUT": ExitCode.INTERNAL,
    "GEEK_ID_MISMATCH": ExitCode.INTERNAL,
    "JOB_ID_MISMATCH": ExitCode.INTERNAL,
}

# 错误码 → UnifiedError.code
_V2_ERR_CODE_MAP = {
    "BLOCKED": ErrorCode.INTERNAL,
    "RUN_NOT_FOUND": ErrorCode.RUN_NOT_FOUND,
    "JOB_MISMATCH": ErrorCode.JOB_MISMATCH,
    "AWAITING_CONFIRMATION": ErrorCode.AWAITING_CONFIRMATION,
    "MISSING_INPUT": ErrorCode.MISSING_SCREENING,
    "INTERNAL": ErrorCode.INTERNAL,
    "POLICY_ERROR": ErrorCode.INTERNAL,
    "LLM_CONFIG_ERROR": ErrorCode.INTERNAL,
    "LLM_ERROR": ErrorCode.INTERNAL,
    "SCORECARD_INVALID": ErrorCode.INTERNAL,
    "EVALUATION_ERROR": ErrorCode.INTERNAL,
    "WRITE_FAILED": ErrorCode.INTERNAL,
    "INVALID_INPUT": ErrorCode.INTERNAL,
    "GEEK_ID_MISMATCH": ErrorCode.INTERNAL,
    "JOB_ID_MISMATCH": ErrorCode.INTERNAL,
}


def _resolve_encrypt_job_id(cli_value: Optional[str]) -> Optional[str]:
    if cli_value:
        return cli_value
    return os.environ.get("BOSS_HR_ENCRYPT_JOB_ID")


def _wrap_v2_result(result: dict) -> CommandResult:
    """把 runner 输出的 dict 包成 CommandResult。

    runner 状态：
      - ok=True, status="candidate_scored" → ok + next_action="repeat_score"
      - ok=True, status="scoring_complete" → ok + next_action="report"
      - ok=False, status="blocked"/"error" → error
    """
    if result.get("ok"):
        status = result.get("status", "candidate_scored")
        data = result.get("data") or {}
        if status == "candidate_scored":
            return ok(
                status="candidate_scored",
                run_id=result.get("run_id"),
                encrypt_job_id=result.get("encrypt_job_id"),
                job_name=result.get("job_name"),
                data=data,
                next_action="repeat_score",
            )
        if status == "scoring_complete":
            return ok(
                status="scoring_complete",
                run_id=result.get("run_id"),
                encrypt_job_id=result.get("encrypt_job_id"),
                job_name=result.get("job_name"),
                data=data,
                next_action="report",
            )
        # 其它 ok 状态 → 视作 candidate_scored 兜底
        return ok(
            status=status,
            run_id=result.get("run_id"),
            encrypt_job_id=result.get("encrypt_job_id"),
            job_name=result.get("job_name"),
            data=data,
            next_action="repeat_score",
        )

    # failure
    err = result.get("error") or {}
    code_str = err.get("code", "INTERNAL")
    exit_code = _V2_STATUS_MAP.get(code_str, ExitCode.INTERNAL)
    err_code = _V2_ERR_CODE_MAP.get(code_str, ErrorCode.INTERNAL)
    return error(
        error_obj=UnifiedError(
            code=err_code,
            message=err.get("message", "score 失败"),
        ),
        run_id=result.get("run_id"),
        encrypt_job_id=result.get("encrypt_job_id"),
        job_name=result.get("job_name"),
        exit_code=exit_code,
        data=result.get("data") or {},
    )


def run_score(*, job_name: str, encrypt_job_id: Optional[str],
              run_id: Optional[str]) -> CommandResult:
    """score 命令业务入口（v2 状态机）。

    一次 score 最多处理一位候选人：
      - 仍有未评分 → status=candidate_scored, next_action=repeat_score
      - 全部完成  → status=scoring_complete, next_action=report
    """
    eid = _resolve_encrypt_job_id(encrypt_job_id)
    if not eid:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_ENCRYPT_JOB_ID,
                message="缺少 encrypt_job_id（传 --encrypt-job-id 或设环境变量 BOSS_HR_ENCRYPT_JOB_ID）",
            ),
            run_id=run_id, exit_code=ExitCode.GENERIC,
        )
    if not run_id:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_RUN_ID,
                message="缺少 --run-id（run_id 是数据边界，禁止自动选择历史 run）",
            ),
            exit_code=ExitCode.MISSING_RUN_ID,
        )

    from boss_hr.scoring.runner import run_score_v2
    result = run_score_v2(
        job_name=job_name,
        encrypt_job_id=eid,
        run_id=run_id,
    )
    return _wrap_v2_result(result)


# ============================================================
# 旧 v1 入口（保留以便 v1 run 只读复用 — 仍通过 application
# 入口被 cli.py 调起；本文件不再导出 find_next_candidate /
# finalize_when_ready，因为 v1 协议已退役）
# ============================================================

# v1 兼容：score_resumes / collect_llm_scores 仍按旧 schema 工作。
# 旧 v1 run 的 screening_results.json 仍可读，但本次 score 命令不再
# 返回 waiting_llm — 它直接由 run_score_v2 内部走 LLM 评估。


__all__ = ["run_score"]
