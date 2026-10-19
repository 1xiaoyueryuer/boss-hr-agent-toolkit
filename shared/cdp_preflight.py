# -*- coding: utf-8 -*-
"""CDP 连接 + 登录态探测（2026-07-31 新增，替代 boss_agent_cli 的登录检测职责）

设计动机：
  boss_agent_cli 提供 boss login、boss me、boss status 等命令检测登录态。
  本模块用 patchright 直连 Edge CDP，自己判断：
    - Edge 是否在 9222 端口跑着
    - BOSS 招聘者 session 是否有效（zp_at + wt2 + bst 三 cookie 都存在且非空）
    - 当前页面是 login / recommend / chat / job_edit / unknown

不替代：
  - 扫码登录（CDP 启动后由用户在浏览器内完成）
  - stoken 合并（BOSS 的 __zp_stoken__ 风控：本工具包走真实 CDP cookie，不需要）

用法：
  from shared.cdp_preflight import connect_cdp, check_login

  session = connect_cdp()  # 默认 http://127.0.0.1:9222
  state = check_login(session)
  if not state['logged_in']:
      raise RuntimeError("BOSS 未登录，请扫码")
  cookies = get_cookies(session)
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# BOSS 招聘者 session 的关键 cookie
RECRUITER_REQUIRED_COOKIES = ("zp_at", "wt2", "bst")


def is_boss_url(url: str) -> bool:
    """Match the actual HTTPS host, never a token/path containing zhipin.com."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        return parts.scheme == "https" and (host == "zhipin.com" or host.endswith(".zhipin.com"))
    except ValueError:
        return False


def select_page(pages):
    """Reuse a BOSS tab first, then a blank tab, then an existing tab."""
    for page in pages:
        if is_boss_url(page.url or ""):
            return page
    for page in pages:
        if page.url in ("", "about:blank", "edge://newtab/", "chrome://newtab/"):
            return page
    return pages[0] if pages else None

# 已知 BOSS 页面 URL 关键字 → 页面类型
_PAGE_KIND_RULES = [
    (re.compile(r"/web/chat/recommend"), "recommend"),
    (re.compile(r"/web/chat/(index|search)"), "chat"),
    (re.compile(r"/web/chat/job/edit"), "job_edit"),
    (re.compile(r"/web/frame/job/edit"), "job_edit"),
    (re.compile(r"/login"), "login"),
]


@dataclass
class CDPSession:
    """CDP 连接句柄。生命周期由调用方管（with 或手动 disconnect）。"""
    cdp_url: str
    playwright: Any = None
    browser: Any = None
    context: Any = None
    page: Any = None
    connected: bool = False
    error: str = ""

    def disconnect(self) -> None:
        """关掉 playwright（不关浏览器，9222 还活着其它任务可能复用）"""
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.connected = False


def connect_cdp(cdp_url: str = "http://127.0.0.1:9222", *, timeout_ms: int = 8000) -> CDPSession:
    """连到 Edge CDP 调试端口，返回 CDPSession。

    异常处理：
      - playwright 未安装 → RuntimeError
      - 9222 没进程 → RuntimeError("CDP 不可达: ...")
      - 连上了但 browser/context/page 取不到 → RuntimeError
    """
    try:
        from patchright.sync_api import sync_playwright
    except ImportError as e:
        raise RuntimeError(
            "patchright 未安装。请运行: python -m pip install patchright（使用本机 Edge，无需下载 Chromium）"
        ) from e

    session = CDPSession(cdp_url=cdp_url)
    try:
        session.playwright = sync_playwright().start()
        session.browser = session.playwright.chromium.connect_over_cdp(cdp_url, timeout=timeout_ms)
        # Reuse the BOSS tab even when Edge restored a blank/startup tab first.
        contexts = session.browser.contexts or []
        if not contexts:
            raise RuntimeError("CDP 已连，但 browser 没有 context（可能刚启动还没页面）")
        session.context = contexts[0]
        pages = session.context.pages or []
        session.page = select_page(pages) if pages else session.context.new_page()
        session.connected = True
        return session
    except Exception as e:
        session.error = f"{type(e).__name__}: {e}"
        session.disconnect()
        raise RuntimeError(f"CDP 不可达: {cdp_url} ({session.error})") from e


def _classify_page(url: str) -> str:
    """根据 URL 判断页面类型。"""
    if not url:
        return "unknown"
    for pattern, kind in _PAGE_KIND_RULES:
        if pattern.search(url):
            return kind
    return "unknown"


def get_cookies(session: CDPSession) -> dict[str, str]:
    """返回当前 context 的 cookie 字典（name → value），方便 HTTP fetch 复用。"""
    if not session.connected or session.context is None:
        raise RuntimeError("CDP 未连接")
    out: dict[str, str] = {}
    for c in session.context.cookies():
        out[c["name"]] = c["value"]
    return out


def check_login(session: CDPSession) -> dict[str, Any]:
    """检查 BOSS 招聘者 session 是否有效。

    返回结构：
      {
        'logged_in': bool,         # zp_at + wt2 + bst 都在且非空
        'cookies': {                # 每个关键 cookie 的存在状态
          'zp_at': bool,
          'wt2': bool,
          'bst': bool,
          'present': [name, ...],  # 所有 cookie 名（只列名，不列值，避免泄密）
          'total': int,            # context.cookies() 总条数
        },
        'current_url': str,
        'page_kind': str,          # 'recommend' | 'chat' | 'job_edit' | 'login' | 'unknown'
      }
    """
    if not session.connected or session.context is None:
        raise RuntimeError("CDP 未连接")

    cookies = session.context.cookies()
    cookie_names = {c["name"]: c["value"] for c in cookies}

    presence = {name: bool(cookie_names.get(name)) for name in RECRUITER_REQUIRED_COOKIES}
    logged_in = all(presence.values())

    url = ""
    if session.page is not None:
        try:
            url = session.page.url or ""
        except Exception:
            url = ""

    return {
        "logged_in": logged_in,
        "cookies": {
            **presence,
            "present": sorted(cookie_names.keys()),
            "total": len(cookies),
        },
        "current_url": url,
        "page_kind": _classify_page(url),
    }


__all__ = [
    "is_boss_url",
    "select_page",
    "CDPSession",
    "connect_cdp",
    "check_login",
    "get_cookies",
    "RECRUITER_REQUIRED_COOKIES",
]
