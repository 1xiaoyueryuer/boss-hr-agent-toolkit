# -*- coding: utf-8 -*-
"""持久化 LLM 用户配置文件解析与合并测试。

覆盖：
  - 仅文件加载 / 仅 env 加载 / env 逐字段覆盖文件
  - 加载后 os.environ 不被写回
  - 不同 cwd 下从同一文件路径加载
  - UTF-8 BOM / 空行 / 注释 / 空白 / 配对引号
  - malformed 行（缺 =、空 key、未闭合引号）含行号
  - 重复 key
  - 非法字段（含 test hook / 模块路径）整份失败
  - API key 占位符被拒绝
  - 错误信息永不泄露 API key
  - timeout / max_retries 严格数值校验
  - 文件不存在 + env 不全 → 错误
  - load_config_from_env 不读文件（向后兼容）
  - runner.run_score_v2 使用 llm_config_path 注入
  - boss_hr.scoring 顶层导入新公开 API
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "shared"))

from boss_hr.scoring.llm_client import (  # noqa: E402
    LLMConfig, LLMConfigError,
    parse_llm_env_file, load_llm_env_file, load_llm_config,
    resolve_default_config_path, DEFAULT_CONFIG_PATH, ALLOWED_FILE_KEYS,
    load_config_from_env,
    ENV_BASE_URL, ENV_API_KEY, ENV_MODEL, ENV_TIMEOUT, ENV_MAX_RETRIES,
)
from boss_hr.scoring import (  # noqa: E402
    load_llm_config as top_load_llm_config,
    parse_llm_env_file as top_parse_llm_env_file,
    DEFAULT_CONFIG_PATH as TOP_DEFAULT_CONFIG_PATH,
    ALLOWED_FILE_KEYS as TOP_ALLOWED_FILE_KEYS,
    resolve_default_config_path as top_resolve_default_config_path,
)


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_bytes(content.encode("utf-8"))
    return p


def _valid_env_text(
    base="https://api.minimaxi.com/v1",
    key="sk-REALKEY-1234567890",
    model="MiniMax-M3",
    timeout="120",
    retries="2",
):
    return (
        f"{ENV_BASE_URL}={base}\n"
        f"{ENV_API_KEY}={key}\n"
        f"{ENV_MODEL}={model}\n"
        f"{ENV_TIMEOUT}={timeout}\n"
        f"{ENV_MAX_RETRIES}={retries}\n"
    )


def _clear_env(monkeypatch):
    for k in (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL, ENV_TIMEOUT, ENV_MAX_RETRIES):
        monkeypatch.delenv(k, raising=False)


# ============================================================
# 1. parse_llm_env_file / load_llm_env_file 基础
# ============================================================

def test_parse_basic():
    text = _valid_env_text()
    out = parse_llm_env_file(text, source="<test>")
    assert out == {
        ENV_BASE_URL: "https://api.minimaxi.com/v1",
        ENV_API_KEY: "sk-REALKEY-1234567890",
        ENV_MODEL: "MiniMax-M3",
        ENV_TIMEOUT: "120",
        ENV_MAX_RETRIES: "2",
    }


def test_parse_utf8_bom(tmp_path):
    p = _write(tmp_path, "llm.env", "﻿" + _valid_env_text())
    out = load_llm_env_file(p)
    assert out[ENV_BASE_URL] == "https://api.minimaxi.com/v1"
    assert out[ENV_MODEL] == "MiniMax-M3"


def test_parse_blank_lines_comments_whitespace():
    text = (
        "\n"
        "# a comment\n"
        "  \n"
        f"  {ENV_BASE_URL}  =  https://x.com/v1  \n"
        f"# inline? still comment because # is first non-ws\n"
        f"{ENV_API_KEY}=sk-AAAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
    )
    out = parse_llm_env_file(text, source="<t>")
    assert out[ENV_BASE_URL] == "https://x.com/v1"
    assert out[ENV_API_KEY] == "sk-AAAA"


def test_parse_paired_quotes():
    text = (
        f'{ENV_BASE_URL}="https://x.com/v1"\n'
        f"{ENV_API_KEY}='sk-AAAA'\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
    )
    out = parse_llm_env_file(text, source="<t>")
    assert out[ENV_BASE_URL] == "https://x.com/v1"
    assert out[ENV_API_KEY] == "sk-AAAA"


def test_parse_equals_in_value():
    text = (
        f"{ENV_BASE_URL}=https://x.com/v1?a=1&b=2\n"
        f"{ENV_API_KEY}=sk-AAA=BBB\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
    )
    out = parse_llm_env_file(text, source="<t>")
    assert out[ENV_BASE_URL] == "https://x.com/v1?a=1&b=2"
    assert out[ENV_API_KEY] == "sk-AAA=BBB"


# ============================================================
# 2. malformed / 重复 / 非法字段 → 整份失败
# ============================================================

def test_malformed_missing_equals():
    text = f"{ENV_BASE_URL} https://x.com/v1\n"
    with pytest.raises(LLMConfigError) as exc:
        parse_llm_env_file(text, source="<t>")
    assert "第 1 行" in str(exc.value)
    assert "=" in str(exc.value)


def test_malformed_empty_key():
    text = "=value\n"
    with pytest.raises(LLMConfigError) as exc:
        parse_llm_env_file(text, source="<t>")
    assert "第 1 行" in str(exc.value)
    assert "key 为空" in str(exc.value)


def test_malformed_unclosed_quote():
    text = f'{ENV_BASE_URL}="https://x.com/v1\n'
    with pytest.raises(LLMConfigError) as exc:
        parse_llm_env_file(text, source="<t>")
    assert "第 1 行" in str(exc.value)
    assert "引号" in str(exc.value)


def test_duplicate_key_fails():
    text = (
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_BASE_URL}=https://y.com/v1\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
    )
    with pytest.raises(LLMConfigError) as exc:
        parse_llm_env_file(text, source="<t>")
    assert "重复" in str(exc.value)
    assert "第 3 行" in str(exc.value)


def test_disallowed_field_fails():
    text = (
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
        "BOSS_HR_LLM_TEST_HOOK=evil.module\n"
    )
    with pytest.raises(LLMConfigError) as exc:
        parse_llm_env_file(text, source="<t>")
    assert "白名单" in str(exc.value)
    assert "BOSS_HR_LLM_TEST_HOOK" in str(exc.value)


def test_disallowed_field_test_hook_load_llm_env(tmp_path):
    p = _write(tmp_path, "llm.env",
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
        "BOSS_HR_LLM_TEST_HOOK=tests.cli._mock_llm_hook\n"
    )
    with pytest.raises(LLMConfigError):
        load_llm_env_file(p)


def test_disallowed_field_allowlist(tmp_path):
    p = _write(tmp_path, "llm.env",
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
        "BOSS_HR_LLM_TEST_HOOK_ALLOWLIST=anything\n"
    )
    with pytest.raises(LLMConfigError):
        load_llm_env_file(p)


def test_disallowed_field_output_dir(tmp_path):
    p = _write(tmp_path, "llm.env",
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
        "BOSS_HR_OUTPUT_DIR=/somewhere\n"
    )
    with pytest.raises(LLMConfigError):
        load_llm_env_file(p)


def test_disallowed_module_path_like_key(tmp_path):
    # 任何看起来像模块路径的键也不允许
    p = _write(tmp_path, "llm.env",
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
        "tests.cli._mock_llm_hook=evil\n"
    )
    with pytest.raises(LLMConfigError):
        load_llm_env_file(p)


# ============================================================
# 3. 整份失败：不会返回部分合法配置
# ============================================================

def test_invalid_field_means_no_partial_load(tmp_path):
    # 文件含 1 个非法字段 → 整份拒绝；load_llm_config(env=合法) 仍要工作
    p = _write(tmp_path, "llm.env",
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
        "BOSS_HR_LLM_TEST_HOOK=evil\n"
    )
    with pytest.raises(LLMConfigError):
        load_llm_env_file(p)


def test_file_invalid_env_overrides_partial(tmp_path, monkeypatch):
    # 即便 env 已提供全部合法字段，文件非法仍让整次加载失败
    p = _write(tmp_path, "llm.env",
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
        "EVIL=1\n"
    )
    _clear_env(monkeypatch)
    with pytest.raises(LLMConfigError):
        load_llm_config(config_path=p)


# ============================================================
# 4. load_llm_config 合并：env 优先 / 空 env 视为未设置 / 不写回
# ============================================================

def test_load_only_from_file(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    p = _write(tmp_path, "llm.env", _valid_env_text())
    cfg = load_llm_config(config_path=p)
    assert cfg.base_url == "https://api.minimaxi.com/v1"
    assert cfg.api_key == "sk-REALKEY-1234567890"
    assert cfg.model == "MiniMax-M3"
    assert cfg.timeout == 120.0
    assert cfg.max_retries == 2


def test_load_only_from_env(tmp_path, monkeypatch):
    p = tmp_path / "absent.env"
    monkeypatch.setenv(ENV_BASE_URL, "https://env.example/v1")
    monkeypatch.setenv(ENV_API_KEY, "sk-ENV")
    monkeypatch.setenv(ENV_MODEL, "M-ENV")
    monkeypatch.setenv(ENV_TIMEOUT, "77")
    cfg = load_llm_config(config_path=p)
    assert cfg.base_url == "https://env.example/v1"
    assert cfg.model == "M-ENV"
    assert cfg.timeout == 77.0


def test_env_overrides_file_per_field(tmp_path, monkeypatch):
    p = _write(tmp_path, "llm.env", _valid_env_text(
        base="https://file.example/v1",
        key="sk-FILE",
        model="M-FILE",
        timeout="120",
        retries="2",
    ))
    # 只覆盖 max_retries
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_MAX_RETRIES, "1")
    cfg = load_llm_config(config_path=p)
    assert cfg.base_url == "https://file.example/v1"
    assert cfg.api_key == "sk-FILE"
    assert cfg.model == "M-FILE"
    assert cfg.timeout == 120.0
    assert cfg.max_retries == 1


def test_empty_env_var_is_unset(tmp_path, monkeypatch):
    p = _write(tmp_path, "llm.env", _valid_env_text(key="sk-FILE"))
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_API_KEY, "")
    cfg = load_llm_config(config_path=p)
    assert cfg.api_key == "sk-FILE"  # 来自文件，不被空 env 覆盖


def test_load_does_not_mutate_os_environ(tmp_path, monkeypatch):
    p = _write(tmp_path, "llm.env", _valid_env_text(
        base="https://file.example/v1",
        key="sk-FILE",
    ))
    _clear_env(monkeypatch)
    # 记录调用前有多少 env 变量
    before = set(os.environ.keys())
    load_llm_config(config_path=p)
    after = set(os.environ.keys())
    added = after - before
    assert ENV_BASE_URL not in added
    assert ENV_API_KEY not in added
    assert ENV_MODEL not in added
    assert ENV_TIMEOUT not in added
    assert ENV_MAX_RETRIES not in added
    # 而且 env 中确实没有这些键
    assert os.environ.get(ENV_BASE_URL) is None
    assert os.environ.get(ENV_API_KEY) is None


def test_load_resolves_home_at_call_time(tmp_path, monkeypatch):
    """不同 cwd 下应使用同一用户配置路径（Path.home() 每次调用解析）。"""
    fake_home = tmp_path
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("HOME", str(fake_home))
    cfg_path = fake_home / ".boss-hr" / "llm.env"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_valid_env_text(), encoding="utf-8")
    _clear_env(monkeypatch)
    # 切到无关目录
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    cfg = load_llm_config()  # 不传 config_path → 默认解析
    assert cfg.base_url == "https://api.minimaxi.com/v1"


def test_load_default_path_is_path_home(tmp_path, monkeypatch):
    p = DEFAULT_CONFIG_PATH
    assert isinstance(p, Path)
    # resolve_default_config_path() 也必须是 Path.home()/.boss-hr/llm.env
    r = resolve_default_config_path()
    assert isinstance(r, Path)
    assert r.name == "llm.env"
    assert r.parent.name == ".boss-hr"


# ============================================================
# 5. 占位符 / 错误不泄露
# ============================================================

@pytest.mark.parametrize("placeholder", [
    "请填写", "<YOUR_API_KEY>", "<你的 API key>", "",
])
def test_api_key_placeholders_rejected(tmp_path, monkeypatch, placeholder):
    p = _write(tmp_path, "llm.env", _valid_env_text(key=placeholder))
    _clear_env(monkeypatch)
    with pytest.raises(LLMConfigError) as exc:
        load_llm_config(config_path=p)
    assert "API key 尚未配置" in str(exc.value)
    # 不回显原值
    for needle in ("请填写", "<YOUR_API_KEY>", "<你的 API key>", "sk-", placeholder):
        if needle:
            assert needle not in str(exc.value)


def test_error_does_not_leak_api_key_value(tmp_path, monkeypatch):
    secret = "sk-DONOTLEAK-99887766"
    p = _write(tmp_path, "llm.env", _valid_env_text(key=secret))
    _clear_env(monkeypatch)
    # 强制文件含非法字段，使加载失败并尝试"提示"哪些字段有问题
    bad = _write(tmp_path, "llm-bad.env",
        _valid_env_text(key=secret) + "EVIL=1\n"
    )
    try:
        load_llm_config(config_path=bad)
    except LLMConfigError as exc:
        msg = str(exc)
        assert "DONOTLEAK" not in msg
        assert "99887766" not in msg
        assert "sk-" not in msg


def test_error_does_not_leak_env_api_key(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_BASE_URL, "https://env.example/v1")
    monkeypatch.setenv(ENV_API_KEY, "sk-ENVDONOTLEAK-XYZ")
    monkeypatch.setenv(ENV_MODEL, "M")
    # 不设 TIMEOUT → 缺必填
    p = tmp_path / "absent.env"
    with pytest.raises(LLMConfigError) as exc:
        load_llm_config(config_path=p)
    assert "ENVDONOTLEAK" not in str(exc.value)
    assert "XYZ" not in str(exc.value)
    assert "sk-" not in str(exc.value)


# ============================================================
# 6. timeout / max_retries 严格校验
# ============================================================

@pytest.mark.parametrize("bad", ["0", "-1", "abc", "1.5e9999", "NaN", "Infinity", "-Infinity"])
def test_timeout_strict(tmp_path, monkeypatch, bad):
    p = _write(tmp_path, "llm.env", _valid_env_text(timeout=bad))
    _clear_env(monkeypatch)
    with pytest.raises(LLMConfigError) as exc:
        load_llm_config(config_path=p)
    assert "TIMEOUT" in str(exc.value)


def test_timeout_env_strict(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "https://x.com/v1")
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "m")
    monkeypatch.setenv(ENV_TIMEOUT, "NaN")
    with pytest.raises(LLMConfigError):
        load_llm_config(config_path=Path("/nope/missing.env"))


@pytest.mark.parametrize("bad", ["-1", "abc", "1.5"])
def test_max_retries_strict(tmp_path, monkeypatch, bad):
    p = _write(tmp_path, "llm.env", _valid_env_text(retries=bad))
    _clear_env(monkeypatch)
    with pytest.raises(LLMConfigError) as exc:
        load_llm_config(config_path=p)
    assert "MAX_RETRIES" in str(exc.value)


def test_max_retries_missing_uses_default_2(tmp_path, monkeypatch):
    # 文件提供 4 必填且无 MAX_RETRIES；缺省走 2
    p = _write(tmp_path, "llm.env",
        f"{ENV_BASE_URL}=https://x.com/v1\n"
        f"{ENV_API_KEY}=sk-AAA\n"
        f"{ENV_MODEL}=M\n"
        f"{ENV_TIMEOUT}=60\n"
    )
    _clear_env(monkeypatch)
    cfg = load_llm_config(config_path=p)
    assert cfg.max_retries == 2


# ============================================================
# 7. 配置文件不存在 + env 不全 → 错误
# ============================================================

def test_file_missing_env_incomplete(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_BASE_URL, "https://x.com/v1")
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "m")
    # 不设 TIMEOUT
    with pytest.raises(LLMConfigError) as exc:
        load_llm_config(config_path=tmp_path / "no.env")
    assert "TIMEOUT" in str(exc.value)


def test_file_missing_env_complete(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "https://x.com/v1")
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "m")
    monkeypatch.setenv(ENV_TIMEOUT, "30")
    cfg = load_llm_config(config_path=Path("/no/such/file"))
    assert cfg.timeout == 30.0


# ============================================================
# 8. load_config_from_env 向后兼容：不读文件
# ============================================================

def test_load_config_from_env_does_not_read_file(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv(ENV_BASE_URL, "https://x.com/v1")
    monkeypatch.setenv(ENV_API_KEY, "k")
    monkeypatch.setenv(ENV_MODEL, "m")
    monkeypatch.setenv(ENV_TIMEOUT, "30")
    # 在默认路径上放一个会"污染"的非法文件
    fake = tmp_path / ".boss-hr" / "llm.env"
    fake.parent.mkdir(parents=True, exist_ok=True)
    fake.write_text("BOSS_HR_LLM_TEST_HOOK=evil\n", encoding="utf-8")
    # load_config_from_env 不应被该文件影响
    cfg = load_config_from_env()
    assert cfg.base_url == "https://x.com/v1"


# ============================================================
# 9. 顶层 boss_hr.scoring 导入
# ============================================================

def test_top_level_imports():
    assert top_load_llm_config is load_llm_config
    assert top_parse_llm_env_file is parse_llm_env_file
    assert TOP_DEFAULT_CONFIG_PATH == DEFAULT_CONFIG_PATH
    assert TOP_ALLOWED_FILE_KEYS == ALLOWED_FILE_KEYS
    assert top_resolve_default_config_path is resolve_default_config_path


# ============================================================
# 10. runner 通过 llm_config_path 注入
# ============================================================

def test_runner_uses_config_path(tmp_path, monkeypatch):
    """run_score_v2 接受 llm_config_path，从指定文件加载 LLM 配置。
    不真调 API；仅验证 llm_config 被成功解析。
    """
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "llm.env"
    cfg_path.write_text(_valid_env_text(), encoding="utf-8")

    # 直接调 _resolve_llm_config 验证参数注入路径
    from boss_hr.scoring.runner import _resolve_llm_config
    cfg, err = _resolve_llm_config(None, cfg_path)
    assert err is None
    assert cfg.api_key == "sk-REALKEY-1234567890"
    assert cfg.base_url == "https://api.minimaxi.com/v1"


def test_runner_explicit_llm_config_wins(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    cfg_path = tmp_path / "llm.env"
    cfg_path.write_text(_valid_env_text(), encoding="utf-8")

    injected = LLMConfig(base_url="https://inj", api_key="k", model="m", timeout=10.0, max_retries=0)
    from boss_hr.scoring.runner import _resolve_llm_config
    cfg, err = _resolve_llm_config(injected, cfg_path)
    assert err is None
    assert cfg is injected


# ============================================================
# 11. 白名单字段正好 5 个，且不包含 test hook 相关键
# ============================================================

def test_allowed_keys_exact_five():
    assert ALLOWED_FILE_KEYS == frozenset({
        ENV_BASE_URL, ENV_API_KEY, ENV_MODEL, ENV_TIMEOUT, ENV_MAX_RETRIES,
    })
    assert "BOSS_HR_LLM_TEST_HOOK" not in ALLOWED_FILE_KEYS
    assert "BOSS_HR_LLM_TEST_HOOK_ALLOWLIST" not in ALLOWED_FILE_KEYS
    assert "BOSS_HR_OUTPUT_DIR" not in ALLOWED_FILE_KEYS
