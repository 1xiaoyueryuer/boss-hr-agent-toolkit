"""User-visible login behavior across repeated commands and slow page loads."""
from types import SimpleNamespace

import pytest

from boss_hr.adapters import browser_environment as be
from shared import cdp_preflight as cdp


class Page:
    def __init__(self, url="about:blank", rendered=False):
        self.url = url
        self.rendered = rendered
        self.navigations = []

    def goto(self, url, **kwargs):
        self.navigations.append(url)
        self.url = url
        self.rendered = True

    def bring_to_front(self):
        pass

    def wait_for_function(self, *args, **kwargs):
        if not self.rendered:
            raise TimeoutError("page remains blank")


def session(monkeypatch, page):
    disconnected = []
    monkeypatch.setattr(cdp, "connect_cdp", lambda *a, **kw: SimpleNamespace(
        page=page, disconnect=lambda: disconnected.append(True)))
    return disconnected


def test_repeated_commands_navigate_once_and_preserve_qr(monkeypatch):
    page = Page()
    disconnected = session(monkeypatch, page)
    assert be._open_login_page() == (True, "")
    assert be._open_login_page() == (True, "")
    assert page.navigations == [be.BOSS_LOGIN_URL]
    assert len(disconnected) == 2


@pytest.mark.parametrize("url", [be.BOSS_LOGIN_URL, "https://www.zhipin.com/web/chat/recommend",
                                 "https://www.zhipin.com/web/common/security-check.html"])
def test_rendered_existing_page_is_never_reloaded(monkeypatch, url):
    page = Page(url, rendered=True)
    session(monkeypatch, page)
    assert be._open_login_page() == (True, "")
    assert page.navigations == []


def test_blank_backend_repaired_with_public_login_once(monkeypatch):
    page = Page("https://www.zhipin.com/web/chat/recommend")
    session(monkeypatch, page)
    assert be._open_login_page() == (True, "")
    assert be._open_login_page() == (True, "")
    assert page.navigations == [be.BOSS_LOGIN_URL]


def test_blank_login_reports_failure_without_reload_loop(monkeypatch):
    page = Page(be.BOSS_LOGIN_URL)
    session(monkeypatch, page)
    for _ in range(2):
        assert be._open_login_page() == (False, "page_content_timeout")
    assert page.navigations == []


def test_navigation_timeout_after_commit_can_still_render(monkeypatch):
    page = Page()
    def goto(url, **kwargs):
        page.url, page.rendered = url, True
        raise TimeoutError("timeout with secret=token")
    page.goto = goto
    session(monkeypatch, page)
    assert be._open_login_page() == (True, "")


def test_navigation_network_error_is_not_success(monkeypatch):
    page = Page()
    def goto(*a, **kw):
        raise OSError("network failed secret=token")
    page.goto = goto
    session(monkeypatch, page)
    assert be._open_login_page() == (False, "goto:OSError")


def test_wrong_host_never_counts_as_boss():
    assert not cdp.is_boss_url("https://zhipin.com.evil.example/?token=secret")
    assert not cdp.is_boss_url("https://evil.example/zhipin.com")
    assert not cdp.is_boss_url("about:blank")
    assert cdp.is_boss_url(be.BOSS_LOGIN_URL)


def test_select_boss_tab_even_when_startup_tab_is_first():
    pages = [Page(), Page("https://example.com"), Page(be.BOSS_LOGIN_URL)]
    assert cdp.select_page(pages) is pages[2]
    assert cdp.select_page(pages[:2]) is pages[0]


def test_existing_url_is_not_enough_to_claim_opened(monkeypatch):
    page = Page()
    def goto(url, **kwargs):
        page.url = url
    page.goto = goto
    session(monkeypatch, page)
    assert be._open_login_page() == (False, "page_content_timeout")


def test_qr_switch_only_clicked_once():
    clicks = []
    switch = SimpleNamespace(is_visible=lambda: not clicks,
                             click=lambda **kw: clicks.append(kw))
    page = SimpleNamespace(url=be.BOSS_LOGIN_URL, locator=lambda selector: switch)
    be._show_qr_login(page, 2000)
    be._show_qr_login(page, 2000)
    assert len(clicks) == 1


def test_verification_page_does_not_touch_login_controls():
    def forbidden(*args):
        raise AssertionError("Verification must remain under user control")
    page = SimpleNamespace(url="https://www.zhipin.com/web/user/safe/verify-slider", locator=forbidden)
    be._show_qr_login(page, 2000)


def test_launchers_do_not_reopen_an_existing_browser(monkeypatch):
    from boss_hr.application import doctor_service as ds
    monkeypatch.setattr(be, "check_edge_executable", lambda: "msedge.exe")
    monkeypatch.setattr(be, "check_cdp_port_listening", lambda: True)
    monkeypatch.setattr(ds, "check_edge_executable", lambda: "msedge.exe")
    monkeypatch.setattr(ds, "check_cdp_port_listening", lambda: True)
    # The global offline fixture rejects any actual Edge process creation.
    assert be.launch_dedicated_edge().ok
    assert ds.launch_edge()["ok"]


def test_backend_redirect_to_slow_login_is_not_restarted(monkeypatch):
    page = Page("https://www.zhipin.com/web/chat/recommend")
    checks = []
    def wait(*a, **kw):
        checks.append(True)
        if len(checks) == 1:
            page.url = be.BOSS_LOGIN_URL
            raise TimeoutError("redirect still rendering")
    page.wait_for_function = wait
    session(monkeypatch, page)
    assert be._open_login_page() == (True, "")
    assert len(checks) == 2
    assert not page.navigations
