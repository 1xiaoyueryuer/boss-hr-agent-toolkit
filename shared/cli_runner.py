# -*- coding: utf-8 -*-
"""Internal worker execution for boss-hr.

Only the unified CLI is public. This module runs allowlisted Python workers
with argument arrays, a fixed working directory, and UTF-8 output. Workers
retain responsibility for run boundaries and business validation.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence

TOOLKIT_ROOT = Path(__file__).resolve().parent.parent

TOOLS = {
    "boss_jd": "boss-job-detail/scripts/boss_jd.py",
    "confirm_run": "shared/confirm_run.py",
    "recommend_list": "boss-recommend-downloader/scripts/recommend_list.py",
    "recommend_download": "boss-recommend-downloader/scripts/recommend_download.py",
    "score_resumes": "resume-screener/scripts/score_resumes.py",
    "generate_html_report": "html-report/scripts/generate_html_report.py",
    "auto_greet": "boss-hr-greet/scripts/auto_greet.py",
    "prepare_scoring_inputs": "resume-screener/scripts/prepare_scoring_inputs.py",
    "collect_llm_scores": "resume-screener/scripts/collect_llm_scores.py",
}


def _resolve_tool(tool: str) -> Path:
    if not isinstance(tool, str) or tool not in TOOLS:
        raise ValueError(f"工具不在白名单中：{tool!r}；允许：{sorted(TOOLS)}")
    script = (TOOLKIT_ROOT / TOOLS[tool]).resolve()
    if not script.is_relative_to(TOOLKIT_ROOT):
        raise ValueError(f"工具路径逃逸工具包根目录：{script}")
    if not script.is_file():
        raise FileNotFoundError(f"内部工具不存在：{script}")
    return script


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONHOME="")
    return env


def run_python_cli(
    tool: str,
    args: Sequence[str],
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run one worker; return its actual exit code and decoded output.

    Unknown paths are rejected before execution. TimeoutExpired propagates to
    the caller; this execution layer does not select or modify task state.
    """
    script = _resolve_tool(tool)
    if isinstance(args, (str, bytes)) or not isinstance(args, Sequence):
        raise TypeError("args 必须是参数序列，不能是命令字符串")
    return subprocess.run(
        [sys.executable, "-X", "utf8", str(script), *map(str, args)],
        shell=False,
        cwd=str(TOOLKIT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_build_env(),
        timeout=timeout,
    )
