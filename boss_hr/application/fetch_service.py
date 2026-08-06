# -*- coding: utf-8 -*-
"""boss_hr.application.fetch_service — fetch 命令业务逻辑。

职责（C1）：
  - 校验 run 存在 + confirmed=true + count > 0
  - 调 cli_runner.run_python_cli('recommend_list', ...) 拉候选人列表
  - 列表成功后才调 cli_runner.run_python_cli('recommend_download', ...) 下载简历
  - download 失败时不回滚（保留 list 产物）
  - 返回 {requested_count, listed_count, downloaded_count, failed_count,
          candidate_list_file, new_resumes_file, failed_resumes_file}

不直接调 patchright / 不读 recommend_list.py 的 print 输出。
所有调用走 adapters/legacy_runner。
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
# 这样 monkeypatch.setattr("boss_hr.adapters.legacy_runner.run_legacy_cli", ...) 才生效
# （import-from 后的引用在调用方命名空间独立，setattr 不会传播）。
from boss_hr.adapters.legacy_runner import legacy_error, try_extract_blocked_message
from boss_hr.adapters.browser_environment import ensure_browser_ready


def _resolve_encrypt_job_id(cli_value: Optional[str]) -> Optional[str]:
    if cli_value:
        return cli_value
    return os.environ.get("BOSS_HR_ENCRYPT_JOB_ID")


# download 超时策略：
# - 节流时间长，固定 1800 仅为下限
# - 随候选人数量 count 线性放大：300 + count * 60
_DOWNLOAD_TIMEOUT_MIN_SECONDS = 1800
_DOWNLOAD_TIMEOUT_BASE_SECONDS = 300
_DOWNLOAD_TIMEOUT_PER_CANDIDATE_SECONDS = 60


def _calculate_download_timeout(count: int) -> int:
    return max(
        _DOWNLOAD_TIMEOUT_MIN_SECONDS,
        _DOWNLOAD_TIMEOUT_BASE_SECONDS
        + count * _DOWNLOAD_TIMEOUT_PER_CANDIDATE_SECONDS,
    )


def _infer_run_paths(job_name: str, eid: str, run_id: str) -> dict:
    """从 JobOutputManager 算 list / download 产物路径。"""
    from shared.output_manager import JobOutputManager
    out = JobOutputManager(job_name, encrypt_job_id=eid, run_id=run_id, lazy=True)
    runs_dir = out.runs_dir
    process_dir = os.path.join(runs_dir, run_id, "process")
    return {
        "runs_dir": runs_dir,
        "process_dir": process_dir,
        "candidate_list_file": os.path.join(process_dir, "recommend_geek_ids.json"),
        "new_resumes_file": os.path.join(process_dir, "new_resumes.json"),
        "failed_resumes_file": os.path.join(process_dir, "failed_resumes.json"),
    }


def _pre_check(job_name: str, eid: str, run_id: str, count: int
               ) -> tuple[int, Optional[str], Optional[dict]]:
    """前置校验：run 存在 + confirmed=true + count>0 + new_resumes 不必存在（fetch 会建）。

    返回 (exit_code, error_msg, paths)。
    - (0, None, paths)：通过
    - (rc!=0, msg, None)：失败
    """
    if count is None or count <= 0:
        return ExitCode(1).value if False else 1, f"--count 必须为正整数（当前 {count}）", None
    from shared.run_orchestrator import (
        RunOrchestrator,
        EXIT_CODE_RUN_NOT_FOUND,
        EXIT_CODE_RUN_JOB_MISMATCH,
    )
    orch = RunOrchestrator(job_name, encrypt_job_id=eid)
    try:
        orch.bind_existing_run(run_id)
    except FileNotFoundError:
        return EXIT_CODE_RUN_NOT_FOUND, f"run_id={run_id} 在岗位目录下不存在", None
    except RuntimeError as e:
        return EXIT_CODE_RUN_JOB_MISMATCH, str(e), None

    run_json_path = os.path.join(orch._mgr.runs_dir, run_id, "run.json")
    if os.path.exists(run_json_path):
        try:
            with open(run_json_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            if not state.get("confirmed"):
                return 20, f"run_id={run_id} 尚未用户确认", None
        except Exception:
            pass

    paths = _infer_run_paths(job_name, eid, run_id)
    return 0, None, paths


def _read_json_array(path: str) -> list:
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_candidates(*, job_name: str, encrypt_job_id: Optional[str],
                     run_id: Optional[str], count: int) -> CommandResult:
    """fetch 命令业务实现。

    流程：
      1) 预校验（run/confirmed/count）
      2) 调 recommend_list.py --max N*2（列表过量拉取，弥补 BOSS 过滤/已沟通剔除）
      3) list 失败 → 透传 rc（list 阶段失败）
      4) list 成功 → 读 recommend_geek_ids.json 算 listed_count
      5) 调 recommend_download.py --max N
      6) download 失败 → 透传 rc（download 阶段失败，list 产物保留）
      7) download 成功 → 读 new_resumes.json / failed_resumes.json
      8) 返回 candidates_fetched
    """
    eid = _resolve_encrypt_job_id(encrypt_job_id)
    if not eid:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_ENCRYPT_JOB_ID,
                message="缺少 encrypt_job_id",
            ),
            run_id=run_id, exit_code=ExitCode.GENERIC,
        )
    if not run_id:
        return error(
            error_obj=UnifiedError(
                code=ErrorCode.MISSING_RUN_ID,
                message="缺少 --run-id",
            ),
            exit_code=ExitCode.MISSING_RUN_ID,
        )

    rc, msg, paths = _pre_check(job_name, eid, run_id, count)
    if rc != 0:
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

    # v1.1.2: 自动启动 Edge + 登录态（不再要求用户预跑 doctor）
    ready = ensure_browser_ready(auto_launch=True)
    if not ready.ok:
        return error(
            error_obj=ready.error_obj,
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode.GENERIC,
            next_action=ready.next_action,
            remediation=ready.remediation,
        )

    # Step 1: 调 recommend_list（按 N*2 过量拉取，弥补 BOSS 侧已沟通/不可见剔除）
    list_result = legacy_runner.run_legacy_cli(
        "recommend_list",
        [
            "--job-name", job_name,
            "--encrypt-job-id", eid,
            "--run-id", run_id,
            "--max", str(count * 2),
        ],
        timeout=600,
    )

    if list_result.returncode != 0:
        better_msg = try_extract_blocked_message(list_result.stdout)
        unified = legacy_error(list_result)
        if better_msg:
            unified = UnifiedError(code=unified.code, message=better_msg,
                                   subprocess_returncode=unified.subprocess_returncode)
        return error(
            error_obj=unified,
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode(list_result.returncode),
        )

    # 读 list 产物
    listed = _read_json_array(paths["candidate_list_file"])
    listed_count = len(listed)

    # Step 2: 调 recommend_download（用相同 --max）
    dl_result = legacy_runner.run_legacy_cli(
        "recommend_download",
        [
            "--job-name", job_name,
            "--encrypt-job-id", eid,
            "--run-id", run_id,
            "--max", str(count),
        ],
        timeout=_calculate_download_timeout(count),  # download 节流长，按 count 动态放大
    )

    if dl_result.returncode != 0:
        # 保留 list 产物，不回滚
        better_msg = try_extract_blocked_message(dl_result.stdout)
        unified = legacy_error(dl_result)
        if better_msg:
            unified = UnifiedError(code=unified.code, message=better_msg,
                                   subprocess_returncode=unified.subprocess_returncode)
        return error(
            error_obj=unified,
            run_id=run_id, encrypt_job_id=eid, job_name=job_name,
            exit_code=ExitCode(dl_result.returncode),
        )

    # 读 download 产物
    new_resumes = _read_json_array(paths["new_resumes_file"])
    failed_resumes = _read_json_array(paths["failed_resumes_file"])
    downloaded_count = sum(1 for r in new_resumes if r.get("ok", True))
    failed_count = len(failed_resumes)

    return ok(
        status="candidates_fetched",
        run_id=run_id, encrypt_job_id=eid, job_name=job_name,
        data={
            "requested_count": count,
            "listed_count": listed_count,
            "downloaded_count": downloaded_count,
            "failed_count": failed_count,
            "candidate_list_file": paths["candidate_list_file"],
            "new_resumes_file": paths["new_resumes_file"],
            "failed_resumes_file": paths["failed_resumes_file"],
        },
        next_action="score",
    )


__all__ = ["fetch_candidates"]
