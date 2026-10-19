# -*- coding: utf-8 -*-
"""boss_hr.adapters.browser_environment — 共享浏览器自动恢复。

提供 `ensure_browser_ready(auto_launch=True, login_wait_seconds=20)`，
供 start / fetch / greet 共用：

  1. 查找 Microsoft Edge；
  2. 检查 127.0.0.1:9222；
  3. 端口未监听时：使用专用 profile 启动 Edge；
  4. 等待端口开启；
  5. 连接 CDP；
  6. 检查 BOSS 登录态；
  7. 检查招聘者页面。

显式拒绝：

  - 使用用户日常 Edge profile
  - 删除专用 profile
  - 自动填写账号、密码或验证码
  - 硬编码用户名路径
  - 启动多个重复的 9222 Edge
  - 因启动失败而读源码或历史输出

不连 BOSS 业务岗位 API；不创建 run；不写业务产物。
"""
from __future__ import annotations
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import urlsplit

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.contracts.errors import ErrorCode, UnifiedError  # noqa: E402
from boss_hr.adapters.browser_preflight import (  # noqa: E402
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


CDP_LAUNCH_WAIT_SECONDS = 15
LOGIN_POLL_INTERVAL = 1.0
BOSS_LOGIN_URL = "https://www.zhipin.com/web/user/?intent=1"
EDGE_BROWSER_LOGIN_URL = BOSS_LOGIN_URL


@dataclass
class BrowserReadyResult:
    """ensure_browser_ready 的结构化结果。

    ok=True  → 浏览器 + BOSS 登录态都已就绪
    ok=False → ok=False 时携带 error_obj + remediation

    数据字段（不破坏旧调用方，向后兼容）：
      - browser_auto_launched: bool  — 本次调用是否自动启动了 Edge
      - login_session_reused: bool   — 浏览器自动启动后 Cookie 立即有效
      - login_page_opened: bool      — 打开了登录页等用户登录
      - login_wait_seconds: int     — 实际等待登录的秒数
    """
    ok: bool
    error_obj: Optional[UnifiedError] = None
    remediation: Optional[dict] = None
    next_action: Optional[str] = None
    info: dict = field(default_factory=dict)


# ============================================================
# Profile 目录
# ============================================================

def _dedicated_profile_dir() -> str:
    """专用 Edge profile 目录（不污染用户日常 Edge profile）。

    优先 %LOCALAPPDATA%（Windows 标准位置）；跨用户安全。
    目录名固定 `boss-hr-edge-profile`（不嵌具体用户名变体）。
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "boss-hr-edge-profile")


def _build_edge_launch_command(edge_path: str,
                                profile_dir: str) -> list[str]:
    """构造启动专用 Edge 的命令。"""
    return [
        edge_path,
        f"--user-data-dir={profile_dir}",
        f"--remote-debugging-port={CDP_PORT}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        BOSS_LOGIN_URL,
    ]


def _ensure_dir(p: str) -> None:
    os.makedirs(p, exist_ok=True)


# ============================================================
# Edge 启动 + 端口等待
# ============================================================

@dataclass
class _EdgeLaunch:
    ok: bool
    pid: Optional[int] = None
    edge_path: Optional[str] = None
    profile_dir: Optional[str] = None
    error_code: Optional[str] = None  # "EDGE_NOT_FOUND" / "EDGE_LAUNCH_FAILED" / "CDP_NOT_RUNNING"
    message: Optional[str] = None


def launch_dedicated_edge(*, wait_seconds: int = CDP_LAUNCH_WAIT_SECONDS
                          ) -> _EdgeLaunch:
    """启动专用 Edge 并等待端口监听。

    Returns:
        _EdgeLaunch
    """
    edge_path = check_edge_executable()
    if not edge_path:
        return _EdgeLaunch(
            ok=False, error_code="EDGE_NOT_FOUND",
            message="未找到 Microsoft Edge 可执行文件",
        )

    profile_dir = _dedicated_profile_dir()
    if check_cdp_port_listening():
        return _EdgeLaunch(ok=True, edge_path=edge_path, profile_dir=profile_dir)
    _ensure_dir(profile_dir)

    cmd = _build_edge_launch_command(edge_path, profile_dir)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        return _EdgeLaunch(
            ok=False, error_code="EDGE_LAUNCH_FAILED",
            message=f"启动 Edge 失败：{type(e).__name__}: {e}",
            edge_path=edge_path, profile_dir=profile_dir,
        )

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if check_cdp_port_listening():
            return _EdgeLaunch(
                ok=True, pid=proc.pid,
                edge_path=edge_path, profile_dir=profile_dir,
            )
        time.sleep(0.5)

    return _EdgeLaunch(
        ok=False, error_code="CDP_NOT_RUNNING",
        message=f"已启动 Edge（pid={proc.pid}）但 {wait_seconds}s 内 9222 仍未监听",
        edge_path=edge_path, profile_dir=profile_dir, pid=proc.pid,
    )


# ============================================================
# 登录状态轮询（不填表 / 不读 cookie 值 / 不绕过）
# ============================================================

def _wait_for_page_content(page, timeout_ms: int) -> None:
    """A committed URL alone does not mean a login/verification page rendered."""
    page.wait_for_function(
        """() => {
            const b = document.body;
            return !!b && (b.innerText.trim().length > 0 ||
                [...b.querySelectorAll('img,canvas,iframe,input,button')].some(el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                }));
        }""",
        timeout=timeout_ms,
    )


def _show_qr_login(page, timeout_ms: int) -> None:
    """Switch the official SMS form to QR once; leave QR/security pages alone."""
    if urlsplit(page.url or "").path.rstrip("/") != "/web/user":
        return
    try:
        switch = page.locator(".btn-sign-switch.ewm-switch")
        if switch.is_visible():
            switch.click(timeout=min(1500, timeout_ms))
    except Exception:
        # Optional UI convenience: a rendered login form is still usable if
        # BOSS changes its switch control. Never retry clicks or fill a form.
        pass


def _open_login_page(url: str = EDGE_BROWSER_LOGIN_URL,
                     timeout_seconds: int = 30) -> tuple[bool, str]:
    """Reuse rendered BOSS pages; repair a blank backend entry once.

    Never refresh an existing login/verification page on command retries, since
    doing so invalidates QR codes and can repeatedly interrupt SPA loading.
    Errors contain types only, never URL query tokens or cookie values.
    """
    from shared.cdp_preflight import connect_cdp, is_boss_url

    sess = None
    try:
        sess = connect_cdp(CDP_URL_DEFAULT, timeout_ms=4000)
        page = sess.page
        if page is None:
            return False, "CDPSession has no page"
        try:
            page.bring_to_front()
        except Exception:
            pass

        deadline = time.monotonic() + timeout_seconds

        def remaining_ms():
            return max(1, int((deadline - time.monotonic()) * 1000))

        current = page.url or ""
        if is_boss_url(current):
            backend_entry = urlsplit(current).path.startswith("/web/chat/")
            try:
                _wait_for_page_content(page, min(5000, remaining_ms())
                                       if backend_entry else remaining_ms())
                if not is_boss_url(page.url or ""):
                    return False, "unexpected_host:non_boss"
                _show_qr_login(page, remaining_ms())
                return True, ""
            except Exception:
                # Let the existing login or verification finish loading. Retrying
                # start must not turn a slow/blocked login into a reload loop.
                if not backend_entry:
                    return False, "page_content_timeout"
                if is_boss_url(page.url or "") and not urlsplit(page.url).path.startswith("/web/chat/"):
                    try:
                        _wait_for_page_content(page, remaining_ms())
                    except Exception:
                        return False, "page_content_timeout"
                    if not is_boss_url(page.url or ""):
                        return False, "unexpected_host:non_boss"
                    _show_qr_login(page, remaining_ms())
                    return True, ""
                # A blank protected backend route can be repaired by going
                # directly to the official public login entry.

        navigation_error = None
        try:
            page.goto(url, wait_until="commit", timeout=remaining_ms())
        except Exception as exc:
            navigation_error = type(exc).__name__
            # A timeout after commit may still render; inspect before failing.
        if not is_boss_url(page.url or ""):
            return False, ("goto:" + navigation_error if navigation_error else "unexpected_host:non_boss")
        try:
            _wait_for_page_content(page, remaining_ms())
        except Exception:
            return False, "page_content_timeout"
        if not is_boss_url(page.url or ""):
            return False, "unexpected_host:non_boss"
        _show_qr_login(page, remaining_ms())
        return True, ""
    except Exception as exc:
        return False, f"{type(exc).__name__}:connect_or_page"
    finally:
        if sess is not None:
            try:
                sess.disconnect()
            except Exception:
                pass


def _poll_login_status(*, wait_seconds: int) -> tuple[bool, dict]:
    """在指定秒数内轮询登录态；返回 (logged_in, info)。

    成功：logged_in=True
    超时：logged_in=False
    """
    deadline = time.time() + wait_seconds
    last_info: dict = {}
    while time.time() < deadline:
        if not check_cdp_port_listening():
            time.sleep(LOGIN_POLL_INTERVAL)
            continue
        try:
            logged_in, info = check_boss_logged_in(CDP_URL_DEFAULT)
            last_info = info
            if logged_in:
                return True, info
        except Exception:
            pass
        time.sleep(LOGIN_POLL_INTERVAL)
    return False, last_info


# ============================================================
# 主入口
# ============================================================

def ensure_browser_ready(*, auto_launch: bool = True,
                         login_wait_seconds: int = 20,
                         wait_for_user_login: bool = False
                         ) -> BrowserReadyResult:
    """start / fetch / greet 共用：保证浏览器 + BOSS 登录态可用。

    Args:
        auto_launch: True → 9222 未开时自动启动专用 Edge
                     False → 缺 CDP 时直接返回 CDP_NOT_RUNNING
        login_wait_seconds: 启动 Edge 后阻塞轮询用户登录的秒数；
                            **仅在 wait_for_user_login=True 时生效**
        wait_for_user_login: True  → 阻塞轮询（fetch / greet 默认）
                            False → 启动 Edge + 打开登录页后立即返回
                                     waiting_user_login 状态（start 默认）

    Returns:
        BrowserReadyResult
          ok=True  → 浏览器就绪 + 登录态有效
          ok=False → 携带 error_obj + remediation + next_action
                     （BOSS_LOGIN_REQUIRED 配合 info.waiting_user_login=True
                     是「等用户扫码」门，start 把 ok=False 翻成 status=waiting_user_login）

    v1.1.3 fix: start 不再在 CLI 内阻塞等待用户扫码。
    默认只启动浏览器 + 打开登录页 + 立即返回 waiting_user_login，
    由 Agent / 用户重复 start 命令触发登录态复核。

    测试钩子（仅离线测试使用）：
      环境变量 BOSS_HR_TEST_PREFLIGHT_WAITING=1 时，跳过真实 CDP 检测，
      直接返回 waiting_user_login 状态（仅供 tests/ 单测，不连真实浏览器）。
    """
    # ---- 测试钩子：跳过真实 CDP / Edge 检测 ----
    if os.environ.get("BOSS_HR_TEST_PREFLIGHT_WAITING") == "1":
        return BrowserReadyResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.BOSS_LOGIN_REQUIRED,
                message="Edge 已就绪但 BOSS 招聘者尚未登录。",
                recoverable=True,
            ),
            next_action="login_then_repeat_command",
            remediation={
                "command": "boss-hr start <同样的 query>",
                "instructions": [
                    "扫码登录 BOSS 招聘者后台",
                    "重新运行原命令",
                ],
            },
            info={
                "browser_auto_launched": True,
                "login_page_opened": True,
                "login_page_open_error": "",
                "waiting_user_login": True,
                "login_wait_seconds": 0,
            },
        )
    # 1. Python 版本（快速 fail）
    py_ok, py_ver = check_python_version()
    if not py_ok:
        return BrowserReadyResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.INTERNAL,
                message=f"Python 版本过低：{py_ver}。要求 >= 3.10。",
                recoverable=False,
            ),
            next_action="upgrade_python",
            remediation={"instructions": ["安装 Python >= 3.10"]},
        )

    # 2. patchright
    if not check_patchright_installed():
        return BrowserReadyResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.INTERNAL,
                message="patchright 未安装。",
                recoverable=False,
            ),
            next_action="install_patchright",
            remediation={
                "instructions": ["在工具包根目录运行: python -m pip install patchright"],
                "command": "python -m pip install patchright",
            },
        )

    auto_launched = False
    login_session_reused = False
    login_page_opened = False

    # 3. Edge 可执行
    if not check_edge_executable():
        return BrowserReadyResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.EDGE_NOT_FOUND,
                message="未找到 Microsoft Edge 可执行文件",
                recoverable=True,
            ),
            next_action="install_edge",
            remediation={
                "instructions": [
                    "安装 Microsoft Edge 浏览器",
                    "重新运行原命令",
                ],
            },
        )

    # 4. CDP 端口
    if not check_cdp_port_listening():
        if not auto_launch:
            return BrowserReadyResult(
                ok=False,
                error_obj=UnifiedError(
                    code=ErrorCode.CDP_NOT_RUNNING,
                    message=f"未检测到 Edge 远程调试端口 {CDP_PORT}",
                    recoverable=True,
                ),
                next_action="launch_edge",
                remediation={
                    "command": "boss-hr doctor --launch-edge",
                    "instructions": [
                        "启动专用 Edge（带 --remote-debugging-port=9222）",
                        "在该 Edge 中登录 BOSS 招聘者后台",
                        "登录后重新运行原命令",
                    ],
                },
            )
        # 自动启动专用 Edge
        launch = launch_dedicated_edge()
        if not launch.ok:
            return BrowserReadyResult(
                ok=False,
                error_obj=UnifiedError(
                    code=ErrorCode(launch.error_code or "EDGE_LAUNCH_FAILED"),
                    message=launch.message or "Edge 启动失败",
                    recoverable=True,
                ),
                next_action="manual_diagnose",
                remediation={
                    "command": "boss-hr doctor",
                    "instructions": [
                        "Edge 自动启动失败；运行 boss-hr doctor 查看详细诊断",
                        "或手动以 --remote-debugging-port=9222 启动 Edge 后重试",
                    ],
                },
                info={"edge_path": launch.edge_path,
                      "profile_dir": launch.profile_dir},
            )
        auto_launched = True

    # 5. CDP 连接
    cdp_ok, cdp_err = check_cdp_connectable(CDP_URL_DEFAULT)
    if not cdp_ok:
        return BrowserReadyResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.CDP_CONNECT_FAILED,
                message=f"Edge CDP 端口已开但连不上：{cdp_err}",
                recoverable=True,
            ),
            next_action="restart_edge",
            remediation={
                "command": "boss-hr doctor --launch-edge",
                "instructions": [
                    "完全关闭该 Edge 后重新以 --remote-debugging-port=9222 启动",
                    "或运行 boss-hr doctor 排查",
                ],
            },
            info={"auto_launched": auto_launched},
        )

    # 6. BOSS 登录态
    logged_in, login_info = check_boss_logged_in(CDP_URL_DEFAULT)
    if logged_in:
        if auto_launched:
            # 自动启动后 Cookie 立即有效 → session reused
            login_session_reused = True
        return BrowserReadyResult(
            ok=True,
            info={
                "browser_auto_launched": auto_launched,
                "login_session_reused": login_session_reused,
                "page_kind": login_info.get("page_kind"),
                "page_url": login_info.get("current_url"),
            },
        )

    # 7. 未登录：自动打开登录页
    if auto_launched or check_cdp_port_listening():
        login_page_opened, login_page_open_error = _open_login_page()
        # v1.1.3: start 默认 wait_for_user_login=False → 不阻塞轮询扫码。
        # fetch / greet 仍保留 wait_for_user_login=True 的旧轮询路径。
        if not wait_for_user_login:
            return BrowserReadyResult(
                ok=False,
                error_obj=UnifiedError(
                    code=ErrorCode.BOSS_LOGIN_REQUIRED,
                    message=(
                        "Edge 已就绪但 BOSS 招聘者尚未登录。"
                        if login_page_opened
                        else "Edge 已启动，但 BOSS 招聘者尚未登录。"
                    ),
                    recoverable=True,
                ),
                next_action="login_then_repeat_command",
                remediation=(
                    {
                        "command": "boss-hr start <同样的 query>",
                        "instructions": [
                            f"Edge 已自动打开 BOSS 登录页（{EDGE_BROWSER_LOGIN_URL}）"
                            if login_page_opened else
                            f"请在专用 Edge 窗口中手动打开 {EDGE_BROWSER_LOGIN_URL}",
                            "在专用 Edge 内扫码登录 BOSS 招聘者后台",
                            "登录完成后重新运行原命令",
                        ],
                    }
                ),
                info={
                    "browser_auto_launched": auto_launched,
                    "login_page_opened": login_page_opened,
                    "login_page_open_error": login_page_open_error,
                    "waiting_user_login": True,
                    "login_wait_seconds": 0,
                },
            )
        # 兼容路径：fetch / greet 仍允许在 CLI 内阻塞轮询登录。
        logged_in, last_info = _poll_login_status(
            wait_seconds=login_wait_seconds,
        )
        if logged_in:
            return BrowserReadyResult(
                ok=True,
                info={
                    "browser_auto_launched": auto_launched,
                    "login_session_reused": False,
                    "login_page_opened": login_page_opened,
                    "page_kind": last_info.get("page_kind"),
                    "page_url": last_info.get("current_url"),
                    "login_wait_seconds": login_wait_seconds,
                },
            )
        # 超时仍未登录；message 与 remediation 严格根据 login_page_opened 区分文案，
        # 不伪称已打开登录页
        if login_page_opened:
            timeout_msg = "已自动打开 BOSS 招聘者登录页，但登录超时。"
            remediation_instructions = [
                f"Edge 已自动打开 BOSS 登录页（{EDGE_BROWSER_LOGIN_URL}）",
                f"请在 {login_wait_seconds}s 内扫码登录招聘者后台",
                "登录完成后重新运行原命令",
            ]
        else:
            # 自动启动 Edge 成功但 _open_login_page 失败：不伪称已打开
            err_suffix = (
                f"（{login_page_open_error}）" if login_page_open_error else ""
            )
            timeout_msg = (
                f"已自动启动专用 Edge，但未能自动打开 BOSS 招聘者登录页{err_suffix}。"
                f"请在专用 Edge 窗口中手动打开 {EDGE_BROWSER_LOGIN_URL} 登录。"
            )
            remediation_instructions = [
                f"在已自动打开的专用 Edge 窗口中手动打开：{EDGE_BROWSER_LOGIN_URL}",
                f"在 {login_wait_seconds}s 内完成 BOSS 招聘者扫码登录",
                "登录完成后重新运行原命令",
            ]
        return BrowserReadyResult(
            ok=False,
            error_obj=UnifiedError(
                code=ErrorCode.BOSS_LOGIN_REQUIRED,
                message=timeout_msg,
                recoverable=True,
            ),
            next_action="login_then_retry",
            remediation={"instructions": remediation_instructions},
            info={
                "browser_auto_launched": auto_launched,
                "login_page_opened": login_page_opened,
                "login_page_open_error": login_page_open_error,
                "login_wait_seconds": login_wait_seconds,
                "waiting_user_login": True,
            },
        )

    # auto_launch=False 且端口未开：走到上面已经返回；
    # 端口已开但 Cookie 失效：走到超时分支
    return BrowserReadyResult(
        ok=False,
        error_obj=UnifiedError(
            code=ErrorCode.BOSS_LOGIN_REQUIRED,
            message="BOSS 招聘者未登录。",
            recoverable=True,
        ),
        next_action="login_then_retry",
        remediation={
            "instructions": [
                "在专用 Edge 窗口中登录 BOSS 招聘者后台",
                "重新运行原命令",
            ],
        },
        info={"waiting_user_login": True},
    )


__all__ = [
    "BrowserReadyResult",
    "ensure_browser_ready",
    "launch_dedicated_edge",
    "_dedicated_profile_dir",
    "CDP_LAUNCH_WAIT_SECONDS",
    "EDGE_BROWSER_LOGIN_URL",
]
