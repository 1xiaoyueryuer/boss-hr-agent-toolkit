# -*- coding: utf-8 -*-
"""boss_hr.application.greet_service — greet 命令业务逻辑。

职责：
  - 校验 encrypt_job_id / run_id / run 存在（预校验，收敛为 23 / 24）
  - 通过 adapters/legacy_runner 调 auto_greet.py（子进程）
  - 读 runs/<run_id>/process/greet_log.json 取 summary
  - 把结果包装成 CommandResult

不直接调 patchright / 不复制 CDP 扫描 / 倒序招呼 / DOM click 逻辑。

⚠️ 必须走子进程（cli_runner），**不能** `from auto_greet import auto_greet`
直接调函数：`auto_greet()` 函数体内引用了只在 `if __name__ == '__main__'`
块里定义的全局 `args`（auto_greet.py:744/753/766），直接调用会在收尾阶段
抛 NameError，因此通过子进程调用其 CLI 入口。

⚠️ greet_log.json 可能不存在：无高分候选人时旧脚本提前 return（rc=0）。
本模块容忍该文件缺失；当前 skip hook 会保留 run 目录。
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
from boss_hr.adapters import legacy_runner
# 重要：用全限定名 `legacy_runner.run_legacy_cli(...)` 而非局部 `run_legacy_cli(...)`；
# 这样 monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", ...) 才生效。
from boss_hr.adapters.legacy_runner import legacy_error, try_extract_blocked_message
from boss_hr.adapters.browser_environment import ensure_browser_ready


def _resolve_encrypt_job_id(cli_value: Optional[str]) -> Optional[str]:
    if cli_value:
        return cli_value
    return os.environ.get("BOSS_HR_ENCRYPT_JOB_ID")


def _greet_log_path(job_name: str, eid: str, run_id: str) -> str:
    """算 runs/<run_id>/process/greet_log.json 路径（与旧脚本同款公式）。"""
    from shared.output_manager import JobOutputManager
    out = JobOutputManager(job_name, encrypt_job_id=eid, run_id=run_id, lazy=True)
    return os.path.join(out.runs_dir, run_id, "process", "greet_log.json")


def _read_greet_log(path: str) -> Optional[dict]:
    """读 greet_log.json；不存在 / 损坏都返回 None（调用方按无产物处理）。"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _pre_check(job_name: str, eid: str, run_id: str
               ) -> tuple[int, Optional[str]]:
    """前置校验：run 存在 + 岗位匹配。返回 (exit_code, error_msg)。

    与 report / fetch 一致：把旧脚本的裸 FileNotFoundError / RuntimeError
    （都是 rc=1）收敛成统一 CLI 的 23 / 24。

    不校验 confirmed：旧 greet 脚本没有 is_confirmed() 守卫
    （全仓库只有 recommend_list / recommend_download 有），保持一致不加。
    """
    from shared.run_orchestrator import (
        RunOrchestrator,
        EXIT_CODE_RUN_NOT_FOUND,
        EXIT_CODE_RUN_JOB_MISMATCH,
    )
    orch = RunOrchestrator(job_name, encrypt_job_id=eid)
    try:
        orch.bind_existing_run(run_id)
    except FileNotFoundError:
        return EXIT_CODE_RUN_NOT_FOUND, f"run_id={run_id} 在岗位目录下不存在"
    except RuntimeError as e:
        return EXIT_CODE_RUN_JOB_MISMATCH, str(e)
    return 0, None


def greet_candidates(*, job_name: str, encrypt_job_id: Optional[str],
                     run_id: Optional[str], only_names: Optional[str] = None,
                     threshold: float = 70.0, max_count: int = 10,
                     dry_run: bool = False) -> CommandResult:
    """greet 命令业务实现。

    流程：
      1) 校验 encrypt_job_id / run_id
      2) 预校验 run 存在 + 岗位匹配（23 / 24）
      3) 调 auto_greet.py 子进程
      4) 失败 → 透传 rc
      5) 成功 → 读 greet_log.json 取 summary（可能不存在 → no_candidates）
      6) 返回 greet_complete
    """
    eid = _resolve_encrypt_job_id(encrypt_job_id)
    if not eid:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_ENCRYPT_JOB_ID,
                message="缺少 encrypt_job_id（传 --encrypt-job-id 或设环境变量 BOSS_HR_ENCRYPT_JOB_ID）",
            ),
            run_id=run_id, exit_code=ExitCode.GENERIC,  # 1
        )

    if not run_id:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_RUN_ID,
                message="缺少 --run-id（run_id 是数据边界，禁止自动选择历史 run）",
            ),
            exit_code=ExitCode.MISSING_RUN_ID,  # 2
        )

    rc, msg = _pre_check(job_name, eid, run_id)
    if rc != 0:
        code = ErrorCode.RUN_NOT_FOUND if rc == 23 else ErrorCode.JOB_MISMATCH
        return error(
            error_obj=UnifiedError(code=code, message=msg or "前置校验失败"),
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode(rc),
        )

    # v1.1.2: 自动启动 Edge + 登录态
    # v1.1.3: greet 保留 v1.1.2 旧轮询路径（wait_for_user_login=True，
    # 默认 20s）。start 由单一参数 --login-wait-seconds 推导，
    # =0 不阻塞、>0 阻塞；fetch/greet 保持兼容接线不变。
    ready = ensure_browser_ready(auto_launch=True, wait_for_user_login=True)
    if not ready.ok:
        return error(
            error_obj=ready.error_obj,
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode.GENERIC,
            next_action=ready.next_action,
            remediation=ready.remediation,
        )

    # 构造子脚本参数。
    # --only-names 模式下旧脚本自己会把 threshold 置 0、max 置名单长度
    # （auto_greet.py:821-828），这里只透传原始参数，不重复该逻辑。
    args_list = [
        "--job-name", job_name,
        "--encrypt-job-id", eid,
        "--run-id", run_id,
        "--threshold", str(threshold),
        "--max", str(max_count),
    ]
    if only_names:
        args_list += ["--only-names", only_names]
    if dry_run:
        args_list.append("--dry-run")

    result = legacy_runner.run_legacy_cli(
        "auto_greet",
        args_list,
        timeout=1800,  # 招呼逐人节流 3-6s + 扫描，取与 download 同量级
    )

    if result.returncode != 0:
        better_msg = try_extract_blocked_message(result.stdout)
        unified = legacy_error(result)
        if better_msg:
            unified = UnifiedError(
                code=unified.code, message=better_msg,
                subprocess_returncode=unified.subprocess_returncode,
            )
        # 子脚本 rc 可能不在 ExitCode enum 里；用 int 透传避免 ValueError
        return error(
            error_obj=unified,
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=result.returncode,
        )

    # 成功：读 greet_log.json
    log_path = _greet_log_path(job_name, eid, run_id)
    greet_log = _read_greet_log(log_path)

    if greet_log is None:
        # 无高分候选人时脚本可能提前返回、未写日志；兼容缺失日志。
        # v1.1.3: status 语义化 → no_candidates；next_action=done。
        return ok(
            status="no_candidates",
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            data={
                "greeted": 0,
                "clicked_unverified": 0,
                "not_found": 0,
                "total": 0,
                "candidates_targeted": 0,
                "dry_run": dry_run,
                "greet_log_file": None,
                "no_candidates": True,
            },
            next_action="done",
        )

    summary = greet_log.get("summary") or {}
    results = greet_log.get("results") or []
    greeted = int(summary.get("greeted", 0))
    not_found = int(summary.get("not_found", 0))
    # 顶层 status 决定 CLI 状态语义：
    #
    #   非 dry-run 路径：
    #     - complete        → greet_complete
    #     - partial_success → partial_success（next_action=review_warnings）
    #     - all_not_found   → greet_complete 但 data.partial_success_warnings=True
    #     - no_candidates   → no_candidates
    #
    #   dry-run 路径（v1.1.3 final）：
    #     - no_candidates     → no_candidates（目标列表为空）
    #     - dry_run_complete  → dry_run_complete（全部定位到；不 click）
    #     - dry_run_review    → dry_run_review（部分/全部 not_found；需人工核对）
    top_status = (greet_log.get("status")
                  or summary.get("status")
                  or ("complete" if greeted > 0 and not_found == 0
                      else "partial_success" if greeted > 0 and not_found > 0
                      else "no_candidates"))
    if top_status == "partial_success":
        cli_status = "partial_success"
        next_action = "review_warnings"
    elif top_status == "complete":
        cli_status = "greet_complete"
        next_action = "done"
    elif top_status == "all_not_found":
        cli_status = "greet_complete"
        next_action = "review_warnings"
    elif top_status == "dry_run_complete":
        cli_status = "dry_run_complete"
        next_action = "done"
    elif top_status == "dry_run_review":
        cli_status = "dry_run_review"
        next_action = "review_warnings"
    else:
        cli_status = "no_candidates"
        next_action = "done"
    return ok(
        status=cli_status,
        run_id=run_id, encrypt_job_id=eid, job_name=job_name,
        data={
            "greeted": greeted,
            "clicked_unverified": int(summary.get("clicked_unverified", 0)),
            "not_found": not_found,
            "total": int(summary.get("total", len(results))),
            "candidates_targeted": len(results),
            "dry_run": dry_run,
            "greet_log_file": os.path.abspath(log_path),
            "no_candidates": (cli_status == "no_candidates"),
            "partial_success_warnings": (top_status in ("partial_success", "all_not_found", "dry_run_review")),
            "greet_log_status": top_status,
            "not_found_names": [r.get("name") for r in results
                                 if r.get("status") in ("not_found", "not_found_after_full_scan")],
        },
        next_action=next_action,
    )


__all__ = ["greet_candidates"]
