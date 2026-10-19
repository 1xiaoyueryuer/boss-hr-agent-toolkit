# -*- coding: utf-8 -*-
"""boss_hr.application.doctor_service — `boss-hr doctor` 业务逻辑。

复用 boss_hr.adapters.browser_preflight（不再复制 CDP / Cookie 检查）。
doctor 不连接业务岗位 API、不创建 run、不写业务输出。
"""
from __future__ import annotations
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent

from boss_hr.contracts.results import CommandResult, ok, error
from boss_hr.contracts.errors import ExitCode, ErrorCode, UnifiedError
from boss_hr.adapters.browser_environment import BOSS_LOGIN_URL
from boss_hr.adapters.browser_preflight import (
    BrowserProbe,
    check_python_version,
    check_patchright_installed,
    check_edge_executable,
    check_cdp_port_listening,
    check_cdp_connectable,
    check_boss_logged_in,
    CDP_HOST,
    CDP_PORT,
    CDP_URL_DEFAULT,
)


# ============================================================
# 启动专用 Edge（--launch-edge）
# ============================================================

def _build_launch_edge_command(edge_path: str, profile_dir: str) -> list[str]:
    """构造启动专用 Edge 的命令。

    使用**专用 profile 目录**（不污染用户普通 Edge profile）：
      %LOCALAPPDATA%\\boss-hr-edge-profile
    """
    return [
        edge_path,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={CDP_PORT}",
        f"--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        BOSS_LOGIN_URL,
    ]


def _default_profile_dir() -> str:
    """专用 Edge profile 目录。

    优先 %LOCALAPPDATA%（跨用户安全，不硬编码任何具体用户名）。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "boss-hr-edge-profile")


def launch_edge(*, wait_seconds: int = 15) -> dict:
    """启动专用 Edge 并等待端口监听。

    Returns:
        dict: {"ok": bool, "edge_path": str, "profile_dir": str,
               "pid": int | None, "error": str | None}
    """
    edge_path = check_edge_executable()
    if not edge_path:
        return {"ok": False, "error": "EDGE_NOT_FOUND",
                "message": "未找到 Microsoft Edge 可执行文件",
                "edge_path": None, "profile_dir": None, "pid": None}

    profile_dir = _default_profile_dir()
    if check_cdp_port_listening():
        return {"ok": True, "error": None, "message": None,
                "edge_path": edge_path, "profile_dir": profile_dir, "pid": None}
    os.makedirs(profile_dir, exist_ok=True)

    cmd = _build_launch_edge_command(edge_path, profile_dir)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        return {"ok": False, "error": "EDGE_LAUNCH_FAILED",
                "message": f"启动 Edge 失败：{type(e).__name__}: {e}",
                "edge_path": edge_path, "profile_dir": profile_dir, "pid": None}

    # 等待端口监听（最多 wait_seconds 秒）
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if check_cdp_port_listening():
            return {"ok": True, "error": None, "message": None,
                    "edge_path": edge_path, "profile_dir": profile_dir,
                    "pid": proc.pid}
        time.sleep(0.5)

    return {"ok": False, "error": "CDP_NOT_RUNNING",
            "message": f"已启动 Edge（pid={proc.pid}）但 {wait_seconds}s 内 9222 仍未监听",
            "edge_path": edge_path, "profile_dir": profile_dir, "pid": proc.pid}


# ============================================================
# 主入口：run_doctor
# ============================================================

def run_doctor(*, launch_edge_flag: bool = False,
               skip_browser: bool = False) -> CommandResult:
    """boss-hr doctor 主入口。

    skip_browser=True 用于不需要 CDP 的"纯本地"检查（pytest 用）。
    """
    # 0. Python 版本
    py_ok, py_ver = check_python_version()

    # 1. patchright 安装
    pr_ok = check_patchright_installed()

    probe = BrowserProbe(
        python_ok=py_ok,
        python_version=py_ver,
        patchright_installed=pr_ok,
    )

    if skip_browser:
        return _ok_or_diagnose_only(probe)

    # 2. --launch-edge 流程
    if launch_edge_flag:
        result = launch_edge()
        if not result["ok"]:
            return error(
                error_obj=UnifiedError(
                    code=ErrorCode(result["error"]),
                    message=result["message"],
                    recoverable=True,
                ),
                exit_code=ExitCode.GENERIC,
                next_action="retry_doctor_after_login",
                remediation={
                    "command": "boss-hr doctor",
                    "instructions": [
                        "在该专用 Edge 中登录 BOSS 招聘者后台",
                        "登录后保持 Edge 窗口打开",
                        "重新运行 boss-hr doctor 验证",
                    ],
                },
            )

    # 3. Edge 可执行
    edge_path = check_edge_executable()
    probe.edge_path = edge_path

    # 4. CDP 端口
    probe.cdp_port_listening = check_cdp_port_listening()
    if not probe.cdp_port_listening:
        return _diagnose(probe,
            ErrorCode.CDP_NOT_RUNNING,
            f"未检测到 Edge 远程调试端口 {CDP_PORT}",
            "launch_edge",
            "boss-hr doctor --launch-edge",
            [
                "启动专用 Edge（带 --remote-debugging-port=9222）",
                "在该 Edge 中登录 BOSS 招聘者后台",
                "登录后重新运行 boss-hr doctor 验证",
            ],
        )

    # 5. CDP 连接
    cdp_ok, cdp_err = check_cdp_connectable()
    probe.cdp_connected = cdp_ok
    probe.cdp_connected_error = cdp_err
    if not cdp_ok:
        return _diagnose(probe,
            ErrorCode.CDP_CONNECT_FAILED,
            f"Edge CDP 端口已开但连不上：{cdp_err}",
            "restart_edge",
            "boss-hr doctor --launch-edge",
            [
                "完全关闭该 Edge 后重新以 --remote-debugging-port=9222 启动",
                "或在另一个浏览器窗口登录后再次运行",
            ],
        )

    # 6. BOSS 登录态
    logged_in, login_info = check_boss_logged_in()
    probe.boss_logged_in = logged_in
    probe.page_url = login_info.get("current_url") or ""
    probe.page_kind = login_info.get("page_kind") or ""
    if not logged_in:
        return _diagnose(probe,
            ErrorCode.BOSS_LOGIN_REQUIRED,
            "Edge 已启动但 BOSS 招聘者未登录。",
            "login_boss",
            "boss-hr doctor",
            [
                "在专用 Edge 窗口中打开 https://www.zhipin.com 并扫码登录招聘者后台",
                "登录后保持 Edge 窗口打开",
                "重新运行 boss-hr doctor 验证",
            ],
        )

    # 7. BOSS 页面类型
    if probe.page_kind in ("unknown",) and not probe.page_url:
        return _diagnose(probe,
            ErrorCode.BOSS_PAGE_REQUIRED,
            "Edge 已登录但当前不在 BOSS 招聘者页面。",
            "open_boss_page",
            "boss-hr doctor",
            [
                "在专用 Edge 中打开 https://www.zhipin.com/web/chat/recommend "
                "或 https://www.zhipin.com/web/chat/index",
                "重新运行 boss-hr doctor 验证",
            ],
        )

    probe.boss_page_ok = True

    return ok(
        status="ready",
        data={
            "edge_found": probe.edge_path is not None,
            "edge_path": probe.edge_path,
            "cdp_port": CDP_PORT,
            "cdp_connected": probe.cdp_connected,
            "boss_logged_in": probe.boss_logged_in,
            "page_url": probe.page_url,
            "page_kind": probe.page_kind,
            "python_version": probe.python_version,
            "patchright_installed": probe.patchright_installed,
        },
        next_action="start",
    )


def _diagnose(probe: BrowserProbe, code: ErrorCode, message: str,
              next_action: str, command: Optional[str],
              instructions: list[str]) -> CommandResult:
    rem = {"instructions": instructions}
    if command:
        rem["command"] = command
    return error(
        error_obj=UnifiedError(code=code, message=message, recoverable=True),
        exit_code=ExitCode.GENERIC,
        next_action=next_action,
        remediation=rem,
    )


def _ok_or_diagnose_only(probe: BrowserProbe) -> CommandResult:
    """无浏览器路径：只返回本地检查结果（pytest / 无 CDP 环境用）。"""
    return ok(
        status="local_only",
        data={
            "python_version": probe.python_version,
            "patchright_installed": probe.patchright_installed,
            "browser_check_skipped": True,
        },
        next_action="run_with_browser",
    )


__all__ = ["run_doctor", "launch_edge"]
