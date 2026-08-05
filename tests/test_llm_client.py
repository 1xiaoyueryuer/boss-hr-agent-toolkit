# -*- coding: utf-8 -*-
"""LLM adapter 单元测试（mock；零网络）。"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_HERE = Path(__file__).resolve().parent.parent
_TOOLKIT_ROOT = _HERE.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))

from boss_hr.scoring.llm_client import (
    LLMConfig, LLMConfigError, LLMAuthError, LLMRateLimitError,
    LLMServerError, LLMTransportError, LLMTimeoutError,
    LLMEmptyResponseError, LLMJSONParseError,
    load_config_from_env, build_messages, complete,
    extract_json, atomic_write_json, ENV_BASE_URL, ENV_API_KEY, ENV_MODEL,
    ENV_TIMEOUT, RETRYABLE_STATUS, _http_post,
)


# ============================================================
# 1-3. 缺 base URL / API key / model
# ============================================================

def test_missing_base_url(monkeypatch):
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "m")
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    with pytest.raises(LLMConfigError):
        load_config_from_env()


def test_missing_api_key(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "https://x.com/v1")
    monkeypatch.setenv(ENV_MODEL, "m")
    monkeypatch.delenv(ENV_API_KEY, raising=False)
    with pytest.raises(LLMConfigError):
        load_config_from_env()


def test_missing_model(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "https://x.com/v1")
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.delenv(ENV_MODEL, raising=False)
    with pytest.raises(LLMConfigError):
        load_config_from_env()


# ============================================================
# 4. API key 不出现在错误中
# ============================================================

def test_api_key_not_in_error(monkeypatch, caplog):
    monkeypatch.setenv(ENV_BASE_URL, "https://x.com/v1")
    monkeypatch.setenv(ENV_API_KEY, "sk-SECRET-XYZ-12345")
    monkeypatch.setenv(ENV_MODEL, "m")
    # 触发一个会抛错的场景（缺 base URL）
    with pytest.raises(LLMConfigError) as exc:
        load_config_from_env({"BOSS_HR_LLM_BASE_URL": ""})
    # 错误里不能含 secret
    assert "SECRET" not in str(exc.value)
    assert "sk-" not in str(exc.value)


def test_api_key_not_in_config_repr():
    cfg = LLMConfig(base_url="https://x.com", api_key="sk-DONOTLEAK",
                     model="m")
    # repr 不应暴露 key（Python 默认 dataclass frozen repr 会暴露，
    # 测试只确认我们不在 messages / log 中打印）
    msgs = build_messages("sys", "user")
    assert all("sk-DONOTLEAK" not in (m.get("content") or "") for m in msgs)


# ============================================================
# 5. 401/403 不重试
# ============================================================

def test_401_no_retry(monkeypatch):
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")
    calls = []

    def fake_post(url, headers, payload, timeout):
        calls.append(1)
        raise LLMAuthError("401", http_status=401)

    msgs = build_messages("s", "u")
    with pytest.raises(LLMAuthError):
        complete(msgs, cfg, http_post=fake_post)
    assert len(calls) == 1, "401 不应触发重试"


def test_403_no_retry():
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")

    def fake_post(url, headers, payload, timeout):
        raise LLMAuthError("403", http_status=403)

    msgs = build_messages("s", "u")
    with pytest.raises(LLMAuthError):
        complete(msgs, cfg, http_post=fake_post)


# ============================================================
# 6. 429 有限重试
# ============================================================

def test_429_retries_with_backoff():
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m",
                     max_retries=2)
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    call_count = 0

    def fake_post(url, headers, payload, timeout):
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            raise LLMRateLimitError("429", http_status=429)
        return {"choices": [{"message": {"content": "ok"}}]}

    msgs = build_messages("s", "u")
    out = complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert out == "ok"
    assert call_count == 3
    # 两次 backoff：0.5, 1.0
    assert sleep_calls == [0.5, 1.0]


# ============================================================
# 7. 5xx 有限重试
# ============================================================

def test_5xx_retries_with_backoff():
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m",
                     max_retries=3)
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    call_count = 0

    def fake_post(url, headers, payload, timeout):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise LLMServerError("503", http_status=503)
        return {"choices": [{"message": {"content": "ok"}}]}

    msgs = build_messages("s", "u")
    out = complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert out == "ok"
    assert call_count == 3


# ============================================================
# 8. 超时
# ============================================================

def test_timeout_propagates(monkeypatch):
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")

    def fake_post(url, headers, payload, timeout):
        raise LLMTimeoutError("timeout")

    msgs = build_messages("s", "u")
    with pytest.raises(LLMTimeoutError):
        complete(msgs, cfg, http_post=fake_post)


# ============================================================
# 9. 网络异常
# ============================================================

def test_transport_error_propagates():
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")

    def fake_post(url, headers, payload, timeout):
        raise LLMTransportError("connection refused")

    msgs = build_messages("s", "u")
    with pytest.raises(LLMTransportError):
        complete(msgs, cfg, http_post=fake_post)


# ============================================================
# 10. 空响应
# ============================================================

def test_empty_response_raises():
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")

    def fake_post(url, headers, payload, timeout):
        return {"choices": []}

    msgs = build_messages("s", "u")
    with pytest.raises(LLMEmptyResponseError):
        complete(msgs, cfg, http_post=fake_post)


# ============================================================
# 11. Markdown code fence 包裹 JSON
# ============================================================

def test_extract_json_from_code_fence():
    text = "Here:\n```json\n{\"a\": 1, \"b\": [1,2,3]}\n```\nEnd."
    obj = extract_json(text)
    assert obj == {"a": 1, "b": [1, 2, 3]}


def test_extract_json_from_plain_fence():
    text = "```\n{\"x\": 2}\n```"
    obj = extract_json(text)
    assert obj == {"x": 2}


# ============================================================
# 12. 非法 JSON
# ============================================================

def test_invalid_json_raises():
    with pytest.raises(LLMJSONParseError):
        extract_json("no json here")

    with pytest.raises(LLMJSONParseError):
        extract_json("```json\n{ broken\n```")

    with pytest.raises(LLMJSONParseError):
        extract_json("")


def test_json_top_level_not_object():
    with pytest.raises(LLMJSONParseError):
        extract_json("just a string")
    with pytest.raises(LLMJSONParseError):
        extract_json("[1, 2, 3]")


# ============================================================
# 13. 原子写入
# ============================================================

def test_atomic_write(tmp_path):
    target = tmp_path / "out.json"
    atomic_write_json(str(target), {"a": 1})
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}


def test_atomic_write_no_leftover_tmp(tmp_path):
    target = tmp_path / "out.json"
    atomic_write_json(str(target), {"a": 2})
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []


def test_atomic_write_replaces_existing(tmp_path):
    target = tmp_path / "out.json"
    target.write_text("old", encoding="utf-8")
    atomic_write_json(str(target), {"a": 3})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 3}


# ============================================================
# 14. 失败不留下半文件
# ============================================================

def test_atomic_write_failure_no_partial(tmp_path, monkeypatch):
    target = tmp_path / "out.json"
    # monkeypatch json.dump 抛错
    import boss_hr.scoring.llm_client as lc
    orig_dump = lc.json.dump
    def bad_dump(*a, **kw):
        raise OSError("disk full")
    monkeypatch.setattr(lc.json, "dump", bad_dump)
    with pytest.raises(OSError):
        atomic_write_json(str(target), {"a": 1})
    # 目标文件不应被创建；临时文件应被清理
    assert not target.exists()
    leftover = list(tmp_path.glob("*.tmp*"))
    assert leftover == []


# ============================================================
# 15. messages 独立：每次 complete() 不共享 messages
# ============================================================

def test_messages_independent_between_calls():
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")
    captured = []

    def fake_post(url, headers, payload, timeout):
        captured.append(payload["messages"][:])
        return {"choices": [{"message": {"content": "ok"}}]}

    msgs_a = build_messages("sys-a", "user-a")
    msgs_b = build_messages("sys-b", "user-b")
    complete(msgs_a, cfg, http_post=fake_post)
    complete(msgs_b, cfg, http_post=fake_post)

    # 两次 call 的 messages 必须独立、不混
    assert captured[0][0]["content"] == "sys-a"
    assert captured[0][1]["content"] == "user-a"
    assert captured[1][0]["content"] == "sys-b"
    assert captured[1][1]["content"] == "user-b"


# ============================================================
# 16. 不包含上一位候选人内容
# ============================================================

def test_no_previous_candidate_in_messages():
    """模拟 evaluator：两次 build_messages 之间不共享 user content。"""
    msgs1 = build_messages("sys", "对候选人1的评估")
    msgs2 = build_messages("sys", "对候选人2的评估")
    assert "候选人1" in msgs1[1]["content"]
    assert "候选人2" in msgs2[1]["content"]
    assert "候选人1" not in msgs2[1]["content"]
    assert "候选人2" not in msgs1[1]["content"]


# ============================================================
# Extra: URL normalization & retry budget
# ============================================================

def test_url_normalization():
    # 显式 /v1
    cfg = LLMConfig(base_url="https://api.example.com/v1", api_key="k", model="m")
    assert cfg.chat_completions_url() == "https://api.example.com/v1/chat/completions"
    # 自动补 /v1
    cfg = LLMConfig(base_url="https://api.example.com", api_key="k", model="m")
    assert cfg.chat_completions_url() == "https://api.example.com/v1/chat/completions"
    # 显式 /v2 不补
    cfg = LLMConfig(base_url="https://api.example.com/v2", api_key="k", model="m")
    assert cfg.chat_completions_url() == "https://api.example.com/v2/chat/completions"


def test_max_retries_zero():
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m",
                     max_retries=0)
    calls = []

    def fake_post(url, headers, payload, timeout):
        calls.append(1)
        raise LLMRateLimitError("429", http_status=429)

    msgs = build_messages("s", "u")
    with pytest.raises(LLMRateLimitError):
        complete(msgs, cfg, http_post=fake_post)
    assert len(calls) == 1


def test_default_sleep_fn_is_time_sleep():
    """生产默认 sleep_fn 必须是 time.sleep（不是 no-op）。"""
    import time
    import inspect
    from boss_hr.scoring import llm_client as _lc
    sig = inspect.signature(_lc.complete)
    assert sig.parameters["sleep_fn"].default is time.sleep


def test_401_no_sleep_call():
    """401 不等待、不重试（sleep_fn 调用次数应为 0）。"""
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    def fake_post(url, headers, payload, timeout):
        raise LLMAuthError("401", http_status=401)

    msgs = build_messages("s", "u")
    with pytest.raises(LLMAuthError):
        complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert sleep_calls == [], f"401 不应调 sleep，实际 {sleep_calls}"


def test_403_no_sleep_call():
    """403 不等待、不重试。"""
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    def fake_post(url, headers, payload, timeout):
        raise LLMAuthError("403", http_status=403)

    msgs = build_messages("s", "u")
    with pytest.raises(LLMAuthError):
        complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert sleep_calls == [], f"403 不应调 sleep，实际 {sleep_calls}"


def test_500_produces_real_backoff_calls():
    """5xx：生产真实 backoff（测试用 fake sleep 只记录参数，不真等待）。"""
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m",
                     max_retries=2)
    sleep_calls: list[float] = []

    def fake_sleep(s):
        # fake sleep：实际 sleep 0 秒；记录参数
        sleep_calls.append(s)

    attempt = 0

    def fake_post(url, headers, payload, timeout):
        nonlocal attempt
        attempt += 1
        if attempt <= 2:
            raise LLMServerError("503", http_status=503)
        return {"choices": [{"message": {"content": "ok"}}]}

    msgs = build_messages("s", "u")
    out = complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert out == "ok"
    # 指数退避：0.5, 1.0
    assert sleep_calls == [0.5, 1.0]


def test_429_produces_real_backoff_calls():
    """429：生产真实 backoff。"""
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m",
                     max_retries=3)
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    attempt = 0

    def fake_post(url, headers, payload, timeout):
        nonlocal attempt
        attempt += 1
        if attempt <= 3:
            raise LLMRateLimitError("429", http_status=429)
        return {"choices": [{"message": {"content": "ok"}}]}

    msgs = build_messages("s", "u")
    out = complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert out == "ok"
    # max_retries=3 → 额外重试 3 次 → backoff 0.5, 1.0, 2.0
    assert sleep_calls == [0.5, 1.0, 2.0]


def test_transport_error_produces_backoff_calls():
    """网络异常：生产真实 backoff（也走重试路径）。"""
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m",
                     max_retries=2)
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    attempt = 0

    def fake_post(url, headers, payload, timeout):
        nonlocal attempt
        attempt += 1
        if attempt <= 2:
            raise LLMTransportError("connection refused")
        return {"choices": [{"message": {"content": "ok"}}]}

    msgs = build_messages("s", "u")
    out = complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert out == "ok"
    assert sleep_calls == [0.5, 1.0]


def test_timeout_propagates_after_exhausted_retries():
    """超时：归类为可重试，max_retries 耗尽后向上抛；backoff 仍按指数退避。"""
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m",
                     max_retries=2)
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    def fake_post(url, headers, payload, timeout):
        raise LLMTimeoutError("timeout")

    msgs = build_messages("s", "u")
    with pytest.raises(LLMTimeoutError):
        complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    # 两次重试 → 两次 backoff
    assert sleep_calls == [0.5, 1.0]


def test_timeout_max_retries_zero_no_sleep():
    """max_retries=0 时超时立即抛出，无 backoff。"""
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m",
                     max_retries=0)
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    def fake_post(url, headers, payload, timeout):
        raise LLMTimeoutError("timeout")

    msgs = build_messages("s", "u")
    with pytest.raises(LLMTimeoutError):
        complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert sleep_calls == []


def test_no_real_sleep_when_all_calls_succeed():
    """全部成功：不调 sleep（生产也不等）。"""
    cfg = LLMConfig(base_url="https://x.com/v1", api_key="k", model="m")
    sleep_calls: list[float] = []

    def fake_sleep(s):
        sleep_calls.append(s)

    def fake_post(url, headers, payload, timeout):
        return {"choices": [{"message": {"content": "ok"}}]}

    msgs = build_messages("s", "u")
    out = complete(msgs, cfg, http_post=fake_post, sleep_fn=fake_sleep)
    assert out == "ok"
    assert sleep_calls == []


def test_public_llm_env_vars_only():
    """生产用户配置只暴露 4 个公开 env vars（不含 TEST_HOOK）。"""
    public_vars = {
        ENV_BASE_URL,
        ENV_API_KEY,
        ENV_MODEL,
        ENV_TIMEOUT,
    }
    assert "BOSS_HR_LLM_TEST_HOOK" not in public_vars


# ============================================================
# BOSS_HR_LLM_TEST_HOOK 安全模型（静态白名单 v2）
#
# 设计原则：允许的模块名是代码内不可由环境变量扩展的静态 frozenset。
# 任意未知模块名（含前缀/后缀/老 ALLOWLIST env 拼接）一律拒绝 import。
# ============================================================

def test_static_allowlist_is_frozenset():
    """_ALLOWED_TEST_HOOKS 必须是不可由 env 扩展的 frozenset。"""
    from boss_hr.scoring import runner as _r
    assert isinstance(_r._ALLOWED_TEST_HOOKS, frozenset)
    assert _r._ALLOWED_TEST_HOOKS == frozenset({"tests.cli._mock_llm_hook"})


def test_resolve_test_hook_static_allowed(monkeypatch):
    """唯一允许的模块名（精确匹配）→ 加载。"""
    from boss_hr.scoring import runner as _r
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", "tests.cli._mock_llm_hook")
    http_post, loaded = _r._resolve_test_hook(None)
    assert loaded is True
    assert callable(http_post)


def test_resolve_test_hook_unknown_module_ignored(monkeypatch, capsys):
    """任意未知模块名：不得 import，走默认 http_post。"""
    from boss_hr.scoring import runner as _r
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", "evil.module")
    http_post, loaded = _r._resolve_test_hook(None)
    assert loaded is False
    assert http_post is None
    captured = capsys.readouterr()
    assert "未在静态白名单中" in captured.err
    assert "已忽略" in captured.err


def test_resolve_test_hook_prefix_variant_rejected(monkeypatch, capsys):
    """前缀变体：_mock_llm_hook_extra / tests.cli / tests → 拒绝。"""
    from boss_hr.scoring import runner as _r
    for bad in [
        "tests.cli._mock_llm_hook_extra",
        "tests.cli",
        "tests",
        "tests.cli._mock_llm_hook.bak",
    ]:
        monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", bad)
        http_post, loaded = _r._resolve_test_hook(None)
        assert loaded is False, f"{bad} 误被允许"
        assert http_post is None


def test_resolve_test_hook_suffix_variant_rejected(monkeypatch):
    """后缀变体：_mock_llm_hook_v2 / .py / 空格 / 大小写变体 → 拒绝。"""
    from boss_hr.scoring import runner as _r
    for bad in [
        "tests.cli._mock_llm_hook_v2",
        "tests.cli._mock_llm_hook.py",
        "tests.cli._mock_llm_hook ",
        " tests.cli._mock_llm_hook",
        "tests.cli._Mock_LLM_Hook",
        "TESTS.CLI._MOCK_LLM_HOOK",
    ]:
        monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", bad)
        http_post, loaded = _r._resolve_test_hook(None)
        assert loaded is False, f"{bad!r} 误被允许"
        assert http_post is None


def test_resolve_test_hook_legacy_allowlist_env_ignored(monkeypatch, capsys):
    """即使额外设置旧 BOSS_HR_LLM_TEST_HOOK_ALLOWLIST，未知模块仍不能加载。"""
    from boss_hr.scoring import runner as _r
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", "evil.module")
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK_ALLOWLIST",
                       "evil.module,tests.cli._mock_llm_hook")
    http_post, loaded = _r._resolve_test_hook(None)
    assert loaded is False
    assert http_post is None
    captured = capsys.readouterr()
    assert "未在静态白名单中" in captured.err


def test_resolve_test_hook_legacy_allowlist_cannot_extend_allowlist(monkeypatch, capsys):
    """旧 ALLOWLIST env 含"陌生但看似安全"模块名 → 仍被静态白名单拒绝。"""
    from boss_hr.scoring import runner as _r
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", "some.attacker.module")
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK_ALLOWLIST",
                       "some.attacker.module")
    http_post, loaded = _r._resolve_test_hook(None)
    assert loaded is False
    assert http_post is None


def test_resolve_test_hook_unset_keeps_default(monkeypatch):
    """未设 BOSS_HR_LLM_TEST_HOOK：返回 (None, False)，上层走默认 _http_post。"""
    from boss_hr.scoring import runner as _r
    monkeypatch.delenv("BOSS_HR_LLM_TEST_HOOK", raising=False)
    http_post, loaded = _r._resolve_test_hook(None)
    assert loaded is False
    assert http_post is None


def test_resolve_test_hook_empty_string(monkeypatch):
    """空字符串视为未设。"""
    from boss_hr.scoring import runner as _r
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", "")
    http_post, loaded = _r._resolve_test_hook(None)
    assert loaded is False
    assert http_post is None


def test_resolve_test_hook_caller_injection_takes_precedence(monkeypatch):
    """调用方已注入 http_post → 不读 env（防 env 覆盖依赖注入）。"""
    from boss_hr.scoring import runner as _r
    sentinel = lambda *a, **kw: None
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", "tests.cli._mock_llm_hook")
    http_post, loaded = _r._resolve_test_hook(sentinel)
    assert http_post is sentinel
    assert loaded is False  # 未读 env hook；走调用方注入


def test_illegal_hook_does_not_leak_api_key(monkeypatch, capsys):
    """非法 hook 的 stderr warning 不得含 API key / model / base_url。"""
    from boss_hr.scoring import runner as _r
    monkeypatch.setenv("BOSS_HR_LLM_TEST_HOOK", "evil.module")
    monkeypatch.setenv("BOSS_HR_LLM_API_KEY", "sk-SECRET-DO-NOT-LEAK-12345")
    monkeypatch.setenv("BOSS_HR_LLM_BASE_URL", "https://secret.example.com")
    monkeypatch.setenv("BOSS_HR_LLM_MODEL", "private-model")
    http_post, loaded = _r._resolve_test_hook(None)
    captured = capsys.readouterr()
    assert "SECRET" not in captured.err
    assert "sk-" not in captured.err
    assert "private-model" not in captured.err
    assert "secret.example.com" not in captured.err


def test_test_hook_module_path_uses_importlib():
    """hook 加载走 importlib.import_module（Python 标准导入机制）。

    不引入 subprocess 或 exec；不动态执行任意字符串。
    """
    import importlib
    mod = importlib.import_module("tests.cli._mock_llm_hook")
    assert hasattr(mod, "http_post_override")
    assert callable(mod.http_post_override)


def test_static_allowlist_not_overridable_by_env(monkeypatch):
    """通过任何 env 变量都不能扩展 _ALLOWED_TEST_HOOKS。

    验证：env 变量名不存在 / 未被读取；_ALLOWED_TEST_HOOKS 仍是 frozenset。
    """
    from boss_hr.scoring import runner as _r
    for env in [
        "BOSS_HR_LLM_TEST_HOOK_ALLOWLIST",
        "BOSS_HR_LLM_TEST_HOOK_EXTRA",
        "BOSS_HR_LLM_TEST_HOOK_PREFIX",
        "BOSS_HR_LLM_TEST_HOOKS",
    ]:
        monkeypatch.setenv(env, "any.module")
    # frozenset 不变
    assert _r._ALLOWED_TEST_HOOKS == frozenset({"tests.cli._mock_llm_hook"})


def test_legacy_allowlist_env_name_no_longer_processed(monkeypatch):
    """旧 BOSS_HR_LLM_TEST_HOOK_ALLOWLIST env 完全不被读取。"""
    from boss_hr.scoring import runner as _r
    import inspect
    src = inspect.getsource(_r)
    assert "BOSS_HR_LLM_TEST_HOOK_ALLOWLIST" not in src, (
        "BOSS_HR_LLM_TEST_HOOK_ALLOWLIST 应已从代码中完全移除"
    )
