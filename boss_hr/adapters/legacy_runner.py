"""boss_hr.adapters.legacy_runner — 旧 CLI 脚本执行适配器。

职责：
  - 包装 shared.cli_runner.run_python_cli()（不创建 spec 文件）
  - 处理子进程 stdout/stderr（统一 bytes→str 解码）
  - 把 generate_html_report 等旧脚本的 stdout 解析为结构化结果
    （报告路径 regex 在这里）
  - 把子进程退出码映射为 contracts.UnifiedError

application 层只调本模块；不直接 import shared.cli_runner、不写
subprocess 命令。
"""
from __future__ import annotations
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

# 让 application 层能正常 import shared.*
_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.contracts.errors import (
    ExitCode, ErrorCode, UnifiedError, error_from_subprocess_rc,
)


@dataclass(frozen=True)
class LegacyRunResult:
    """单次 legacy 脚本执行的结构化结果。"""
    returncode: int
    stdout: str
    stderr: str
    report_file: Optional[str] = None  # 仅 generate_html_report 等会填
    extra: dict = None  # 其他子脚本按需扩展


# generate_html_report.py 的"✅ HTML 报告已生成: <path>"行
_REPORT_PATH_RE = re.compile(r"HTML 报告已生成:\s*(\S.*?)(?:\s*$|\n)", re.MULTILINE)


def _resolve_report_path(eid: str, run_id: str, job_name: str) -> str:
    """从 JobOutputManager 算默认报告路径（旧脚本同款公式）。"""
    from output_manager import JobOutputManager
    out = JobOutputManager(job_name, encrypt_job_id=eid, run_id=run_id, lazy=True)
    return os.path.join(out.runs_dir, run_id, f"{run_id}_screening_report.html")


def run_legacy_cli(tool: str, args: Sequence[str], *,
                   timeout: float = 60,
                   extract_report_path: bool = False,
                   eid: str | None = None,
                   run_id: str | None = None,
                   job_name: str | None = None) -> LegacyRunResult:
    """调 cli_runner.run_python_cli；可选地从 stdout 抽报告路径。

    cli_runner.run_python_cli 用 text=True + encoding="utf-8"，
    所以 proc.stdout / proc.stderr 已经是 str，不需要再 decode。
    """
    from cli_runner import run_python_cli

    proc = run_python_cli(tool, list(args), timeout=timeout)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    report_file: Optional[str] = None
    if extract_report_path and proc.returncode == 0:
        m = _REPORT_PATH_RE.search(stdout)
        if m:
            report_file = m.group(1).strip()
        elif eid and run_id and job_name:
            report_file = _resolve_report_path(eid, run_id, job_name)

    return LegacyRunResult(
        returncode=proc.returncode,
        stdout=stdout,
        stderr=stderr,
        report_file=report_file,
    )


def legacy_error(result: LegacyRunResult, *,
                 default_message: str | None = None) -> UnifiedError:
    """LegacyRunResult → UnifiedError（按子进程退出码映射）。"""
    if result.returncode == 0:
        return UnifiedError(code=ErrorCode.INTERNAL,
                            message="子进程 rc=0 不应转为 error")
    return error_from_subprocess_rc(
        result.returncode,
        default_message=default_message or f"legacy tool 退出码 {result.returncode}",
    )


def try_extract_blocked_message(stdout: str) -> Optional[str]:
    """从子进程 stdout 抽 {"status":"blocked","exit_code":...,"message":...} 的 message。"""
    if not stdout:
        return None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not (line.startswith("{") and line.endswith("}")):
            continue
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(j, dict) and j.get("status") == "blocked" and "message" in j:
            return j["message"]
    return None


__all__ = [
    "LegacyRunResult",
    "run_legacy_cli",
    "legacy_error",
    "try_extract_blocked_message",
]
