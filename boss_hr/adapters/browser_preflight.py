# -*- coding: utf-8 -*-
"""boss_hr.adapters.browser_preflight — 浏览器命令统一预检 adapter。

职责：
  - 复用 shared.cdp_preflight（不复制 CDP / Cookie 检查逻辑）
  - 把"缺 CDP / 未登录 / 页面错"等**环境异常**翻译成带
    `recoverable / next_action / remediation` 的结构化错误
  - 让 start / fetch / greet 在调业务脚本**之前**一次性预检，
    避免落入"INTERNAL + subprocess rc=1"通用外壳

不在这里做：
  - 不连接 BOSS 业务岗位 API（保持只读）
  - 不创建 run / 不写业务产物
"""
from __future__ import annotations
import os
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.contracts.errors import ErrorCode, UnifiedError  # noqa: E402


CDP_URL_DEFAULT = "http://127.0.0.1:9222"
CDP_HOST = "127.0.0.1"
CDP_PORT = 9222


# ============================================================
# 阶段结果（结构化）
# ============================================================

@dataclass
class BrowserProbe:
    """浏览器环境 7 项检查的逐项结果（doctor 用）。"""
    python_ok: bool = False
    python_version: str = ""
    patchright_installed: bool = False
    edge_path: Optional[str] = None
    cdp_port_listening: bool = False
    cdp_connected: bool = False
    cdp_connected_error: str = ""
    boss_logged_in: bool = False
    boss_page_ok: bool = False
    page_url: str = ""
    page_kind: str = ""

    def any_failure(self) -> bool:
        return not (
            self.python_ok
            and self.patchright_installed
            and self.cdp_port_listening
            and self.cdp_connected
            and self.boss_logged_in
            and self.boss_page_ok
        )


# ============================================================
# 子检查（doctor + browser_preflight 共用）
# ============================================================

def check_python_version() -> tuple[bool, str]:
    """返回 (ok, version_str)。要求 >= 3.10。"""
    v = sys.version_info
    s = f"{v.major}.{v.minor}.{v.micro}"
    return (v.major, v.minor) >= (3, 10), s


def check_patchright_installed() -> bool:
    try:
        import patchright  # noqa: F401
        return True
    except ImportError:
        return False


def check_edge_executable() -> Optional[str]:
    """Windows 查找 Microsoft Edge 可执行文件。返回路径或 None。"""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    # PATH 里找 msedge
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        candidate = os.path.join(d, "msedge.exe" if os.name == "nt" else "msedge")
        if os.path.isfile(candidate):
            return candidate
    return None


def check_cdp_port_listening(host: str = CDP_HOST, port: int = CDP_PORT) -> bool:
    """TCP 探测 127.0.0.1:9222 是否监听。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect((host, port))
        return True
    except (OSError, socket.timeout):
        return False


def check_cdp_connectable(url: str = CDP_URL_DEFAULT) -> tuple[bool, str]:
    """通过 shared.cdp_preflight 真实连一次。返回 (connected, error_msg)。"""
    from shared.cdp_preflight import connect_cdp
    try:
        sess = connect_cdp(url, timeout_ms=4000)
        sess.disconnect()
        return True, ""
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_boss_logged_in(url: str = CDP_URL_DEFAULT) -> tuple[bool, dict]:
    """通过 shared.cdp_preflight.check_login 检查登录态 + 页面类型。"""
    from shared.cdp_preflight import connect_cdp, check_login
    sess = connect_cdp(url, timeout_ms=4000)
    try:
        info = check_login(sess)
    finally:
        sess.disconnect()
    return info.get("logged_in", False), info


# ============================================================
# 统一预检（start / fetch / greet 调用前）
# ============================================================

@dataclass
class PreflightResult:
    """统一预检结果。

    ok=True 表示环境就绪；ok=False 时通过 error_obj 携带可恢复错误。
    """
    ok: bool
    error_obj: Optional[UnifiedError] = None
    remediation: Optional[dict] = None
    next_action: Optional[str] = None
    info: Optional[dict] = None


def _build_remediation(*, code: ErrorCode, instructions: list[str],
                       command: Optional[str] = None) -> dict:
    out = {"instructions": instructions}
    if command:
        out["command"] = command
    return out


def browser_preflight(*, url: str = CDP_URL_DEFAULT) -> PreflightResult:
    """start / fetch / greet 共用预检。

    失败时返回 ok=False + UnifiedError(recoverable=True) + remediation，
    **不**落到 INTERNAL / subprocess rc=1 的通用外壳。

    成功时返回 ok=True + info（page_url / page_kind）。
    """
    # 1. Python 版本
    ok_py, ver = check_python_version()
    if not ok_py:
        return PreflightResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.INTERNAL,
                message=f"Python 版本过低：{ver}。要求 >= 3.10。",
            ),
            next_action="upgrade_python",
            remediation=_build_remediation(
                code=ErrorCode.INTERNAL,
                instructions=["安装 Python >= 3.10", "重新运行 boss-hr doctor"],
            ),
        )

    # 2. patchright
    if not check_patchright_installed():
        return PreflightResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.INTERNAL,
                message="patchright 未安装。",
            ),
            next_action="install_patchright",
            remediation=_build_remediation(
                code=ErrorCode.INTERNAL,
                instructions=[
                    "在工具包根目录运行: python -m pip install patchright",
                    "重新运行 boss-hr doctor",
                ],
                command="python -m pip install patchright",
            ),
        )

    # 3. CDP 端口监听
    if not check_cdp_port_listening():
        return PreflightResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.CDP_NOT_RUNNING,
                message=f"未检测到 Edge 远程调试端口 {CDP_PORT}",
            ),
            next_action="launch_edge",
            remediation=_build_remediation(
                code=ErrorCode.CDP_NOT_RUNNING,
                instructions=[
                    "启动专用 Edge（带 --remote-debugging-port=9222）",
                    "在该 Edge 中登录 BOSS 招聘者后台",
                    "登录后重新运行 boss-hr doctor 验证",
                ],
                command="boss-hr doctor --launch-edge",
            ),
        )

    # 4. CDP 连接
    cdp_ok, cdp_err = check_cdp_connectable(url)
    if not cdp_ok:
        return PreflightResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.CDP_CONNECT_FAILED,
                message=f"Edge CDP 端口已开但连不上：{cdp_err}",
            ),
            next_action="restart_edge",
            remediation=_build_remediation(
                code=ErrorCode.CDP_CONNECT_FAILED,
                instructions=[
                    "完全关闭该 Edge 后重新以 --remote-debugging-port=9222 启动",
                    "或在另一个浏览器窗口登录后再次运行",
                ],
                command="boss-hr doctor --launch-edge",
            ),
        )

    # 5. BOSS 登录态
    logged_in, info = check_boss_logged_in(url)
    if not logged_in:
        return PreflightResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.BOSS_LOGIN_REQUIRED,
                message="Edge 已启动但 BOSS 招聘者未登录。",
            ),
            next_action="login_boss",
            remediation=_build_remediation(
                code=ErrorCode.BOSS_LOGIN_REQUIRED,
                instructions=[
                    "在专用 Edge 窗口中打开 https://www.zhipin.com 并扫码登录招聘者后台",
                    "登录后保持 Edge 窗口打开",
                    "重新运行 boss-hr doctor 验证",
                ],
                command="boss-hr doctor",
            ),
        )

    # 6. 页面类型（chat / recommend / job-edit / unknown）
    page_kind = info.get("page_kind") or "unknown"
    page_url = info.get("current_url") or ""
    if page_kind in ("unknown",) and not page_url:
        return PreflightResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.BOSS_PAGE_REQUIRED,
                message="Edge 已登录但当前不在 BOSS 招聘者页面。",
            ),
            next_action="open_boss_page",
            remediation=_build_remediation(
                code=ErrorCode.BOSS_PAGE_REQUIRED,
                instructions=[
                    "在专用 Edge 中打开 https://www.zhipin.com/web/chat/recommend "
                    "或 https://www.zhipin.com/web/chat/index",
                    "重新运行 boss-hr doctor 验证",
                ],
                command="boss-hr doctor",
            ),
        )

    return PreflightResult(
        ok=True,
        info={
            "page_url": page_url,
            "page_kind": page_kind,
            "logged_in": logged_in,
        },
    )


__all__ = [
    "BrowserProbe",
    "PreflightResult",
    "browser_preflight",
    "check_python_version",
    "check_patchright_installed",
    "check_edge_executable",
    "check_cdp_port_listening",
    "check_cdp_connectable",
    "check_boss_logged_in",
    "CDP_URL_DEFAULT",
    "CDP_HOST",
    "CDP_PORT",
]
