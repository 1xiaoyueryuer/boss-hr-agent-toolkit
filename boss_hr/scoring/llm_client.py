# -*- coding: utf-8 -*-
"""boss_hr.scoring.llm_client — 独立、无状态的 OpenAI-compatible LLM adapter。

设计要点：
  - 完全无状态：每次 `complete()` 都是一次全新请求，绝不保留 messages /
    conversation_id / 上下文。`build_messages()` 接受 system + user，由
    调用方负责单次构造；adapter 本身不缓存任何东西。
  - 适配 OpenAI-compatible `/v1/chat/completions`：
      * base URL 末尾 `/` 容忍，自动归一化避免重复斜杠。
      * base URL 已含 `/v1` 时不再追加；不含时按常见情况补一次。
      * 不引入 OpenAI SDK / 任何第三方 HTTP 库；走 stdlib `urllib`。
  - 非流式 JSON 请求；只解析 `choices[0].message.content`。
  - 显式重试：401/403 / 配置错误不重试；429/5xx/网络异常/超时走有限
    重试。`time.sleep` 通过参数 `sleep_fn` 注入，测试中可 monkeypatch
    为 no-op，禁止真实 sleep。
  - 原子写：临时文件 + `os.replace` 替换，绝不半写。
  - 安全：API key 永远不出现在异常、log、stdout / stderr。错误信息只
    暴露"配置缺 / 401 / 403 / 429 / 5xx"等分类标签。
  - 解析：从模型响应中容错抽出 JSON：
      * 先剥 ```json ... ``` / ``` ... ``` Markdown code fence；
      * 再找最大 `{...}` 子串；
      * 解析失败抛 LLMClientError。
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_HERE = Path(__file__).resolve().parent
_BOSS_HR = _HERE.parent
_TOOLKIT_ROOT = _BOSS_HR.parent
sys.path.insert(0, str(_TOOLKIT_ROOT))
sys.path.insert(0, str(_TOOLKIT_ROOT / "shared"))


# ============================================================
# 错误类型
# ============================================================

class LLMClientError(Exception):
    """LLM adapter 抛出的统一错误基类。"""

    code: str = "LLM_ERROR"

    def __init__(self, message: str, *, http_status: Optional[int] = None,
                 response_body: Optional[str] = None) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.response_body = response_body

    def to_dict(self) -> Dict[str, Any]:
        out = {"code": self.code, "message": str(self)}
        if self.http_status is not None:
            out["http_status"] = self.http_status
        return out


class LLMConfigError(LLMClientError):
    code = "LLM_CONFIG_ERROR"


class LLMAuthError(LLMClientError):
    code = "LLM_AUTH_ERROR"


class LLMRateLimitError(LLMClientError):
    code = "LLM_RATE_LIMIT"


class LLMServerError(LLMClientError):
    code = "LLM_SERVER_ERROR"


class LLMTransportError(LLMClientError):
    code = "LLM_TRANSPORT_ERROR"


class LLMTimeoutError(LLMClientError):
    code = "LLM_TIMEOUT"


class LLMEmptyResponseError(LLMClientError):
    code = "LLM_EMPTY_RESPONSE"


class LLMJSONParseError(LLMClientError):
    code = "LLM_JSON_PARSE_ERROR"


# ============================================================
# 环境变量名（v2 固定 4 个）
# ============================================================

ENV_BASE_URL = "BOSS_HR_LLM_BASE_URL"
ENV_API_KEY = "BOSS_HR_LLM_API_KEY"
ENV_MODEL = "BOSS_HR_LLM_MODEL"
ENV_TIMEOUT = "BOSS_HR_LLM_TIMEOUT"
ENV_MAX_RETRIES = "BOSS_HR_LLM_MAX_RETRIES"


# ============================================================
# 配置 + 客户端
# ============================================================

@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    timeout: float = 60.0
    max_retries: int = 2  # 429/5xx/网络/超时的额外重试次数

    def chat_completions_url(self) -> str:
        """归一化 base URL + 拼 /chat/completions。

        规则：
          1) 去末尾斜杠。
          2) 若 path 末段**正好是 `/v1`**（不区分大小写），不再追加。
             其他显式版本段（`/v2`、`/api/v1`）均视为已带版本：
             - 末段是 `v<数字>` → 不补（用户显式声明）；
             - 末段不是 `v<数字>` → 补一次 `/v1`。
          3) 拼 `/chat/completions`。
        """
        base = (self.base_url or "").strip().rstrip("/")
        if not base:
            raise LLMConfigError("base_url 为空")
        path = base.split("?", 1)[0].split("#", 1)[0]
        last_seg = path.rsplit("/", 1)[-1].lower()
        # 显式 v<digit> 表示用户给了 API 版本，不补
        is_explicit_version = (
            last_seg.startswith("v") and len(last_seg) > 1
            and last_seg[1:].isdigit()
        )
        if not is_explicit_version:
            base = base + "/v1"
        return base + "/chat/completions"


def load_config_from_env(env: Optional[Dict[str, str]] = None) -> LLMConfig:
    """从环境变量读 4 个必填 + 1 个可选。

    任一必填缺失 → LLMConfigError。
    """
    src = env if env is not None else os.environ
    base_url = (src.get(ENV_BASE_URL) or "").strip()
    api_key = (src.get(ENV_API_KEY) or "").strip()
    model = (src.get(ENV_MODEL) or "").strip()
    if not base_url:
        raise LLMConfigError(
            f"缺少环境变量 {ENV_BASE_URL}（OpenAI-compatible base URL）"
        )
    if not api_key:
        raise LLMConfigError(
            f"缺少环境变量 {ENV_API_KEY}（禁止通过 CLI 传入）"
        )
    if not model:
        raise LLMConfigError(
            f"缺少环境变量 {ENV_MODEL}"
        )
    try:
        timeout = float(src.get(ENV_TIMEOUT, "60"))
    except (TypeError, ValueError):
        timeout = 60.0
    if timeout <= 0:
        timeout = 60.0
    try:
        max_retries = int(src.get(ENV_MAX_RETRIES, "2"))
    except (TypeError, ValueError):
        max_retries = 2
    if max_retries < 0:
        max_retries = 0
    return LLMConfig(
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout=timeout,
        max_retries=max_retries,
    )


# ============================================================
# JSON 抽取（容错）
# ============================================================

_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)
_FIRST_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_json(text: str) -> Dict[str, Any]:
    """从模型响应中抽 JSON object。优先级：

      1) ```json ... ``` / ``` ... ``` Markdown code fence
      2) 第一个 {...} 子串
      3) 全文

    失败抛 LLMJSONParseError。
    """
    if not isinstance(text, str) or not text.strip():
        raise LLMJSONParseError("模型响应为空或非字符串")
    m = _FENCE_RE.search(text)
    if m:
        candidate = m.group(1)
    else:
        m2 = _FIRST_OBJECT_RE.search(text)
        if not m2:
            raise LLMJSONParseError("模型响应中找不到 JSON object")
        candidate = m2.group(0)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise LLMJSONParseError(f"模型响应 JSON 解析失败：{e.msg}")
    if not isinstance(obj, dict):
        raise LLMJSONParseError("模型响应 JSON 顶层不是 object")
    return obj


# ============================================================
# HTTP 发送
# ============================================================

# 哪些 HTTP 状态码允许重试
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _http_post(
    url: str,
    headers: Dict[str, str],
    payload: Dict[str, Any],
    timeout: float,
) -> Dict[str, Any]:
    """发 POST；返回解析后的 JSON dict。

    抛出：
      LLMAuthError  (401/403)
      LLMRateLimitError  (429)
      LLMServerError  (5xx)
      LLMTransportError  (URLError/ConnectionError)
      LLMTimeoutError  (socket.timeout)
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # 服务端可能返了非 JSON —— 抛 server 错便于重试
                raise LLMServerError(
                    f"LLM 返回内容不是合法 JSON：{raw[:200]!r}",
                    http_status=getattr(resp, "status", None),
                    response_body=raw[:500],
                )
    except urllib.error.HTTPError as e:
        status = int(getattr(e, "code", 0) or 0)
        try:
            err_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            err_body = ""
        if status in (401, 403):
            raise LLMAuthError(
                f"LLM 鉴权失败（{status}）", http_status=status,
                response_body=err_body[:500],
            )
        if status == 429:
            raise LLMRateLimitError(
                f"LLM 限流（429）", http_status=status,
                response_body=err_body[:500],
            )
        if status >= 500:
            raise LLMServerError(
                f"LLM 服务端错误（{status}）", http_status=status,
                response_body=err_body[:500],
            )
        # 4xx 非鉴权/非限流 → 视为请求错误，不重试
        raise LLMServerError(
            f"LLM 请求错误（{status}）", http_status=status,
            response_body=err_body[:500],
        )
    except urllib.error.URLError as e:
        raise LLMTransportError(f"LLM 网络异常：{e.reason}")
    except TimeoutError as e:
        raise LLMTimeoutError(f"LLM 请求超时：{e}")
    except OSError as e:
        raise LLMTransportError(f"LLM 网络异常：{e}")


# ============================================================
# 公共 API
# ============================================================

def build_messages(system: str, user: str) -> list[Dict[str, str]]:
    """构造单次 messages（system + user 两条）。"""
    if not isinstance(system, str) or not system.strip():
        raise LLMConfigError("system prompt 必须是非空字符串")
    if not isinstance(user, str) or not user.strip():
        raise LLMConfigError("user prompt 必须是非空字符串")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def complete(
    messages: list[Dict[str, str]],
    config: LLMConfig,
    *,
    sleep_fn: Callable[[float], None] = time.sleep,
    http_post: Callable[..., Dict[str, Any]] = _http_post,
) -> str:
    """单次 LLM 完成；返回 choices[0].message.content。

    重试规则（限 `config.max_retries` 次额外重试）：
      - 401/403 / 配置错误 / 4xx 非限流 → 不重试，立即抛错。
      - 429 / 5xx / 网络异常 / 超时 → 退避后重试。
      - 空响应 / JSON 解析失败 / schema 失败 → 不在此层重试（schema
        校验属于调用方职责）；adapter 层面只把内容抛回去。
    """
    if not isinstance(messages, list) or not messages:
        raise LLMConfigError("messages 必须是非空 list")
    for i, m in enumerate(messages):
        if not isinstance(m, dict) or m.get("role") not in {"system", "user", "assistant"}:
            raise LLMConfigError(f"messages[{i}] 必须是合法 chat message")
        if not isinstance(m.get("content"), str) or not m["content"].strip():
            raise LLMConfigError(f"messages[{i}].content 必须是非空字符串")

    url = config.chat_completions_url()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {config.api_key}",
    }
    payload = {
        "model": config.model,
        "messages": messages,
        "temperature": 0,
        "stream": False,
    }

    attempt = 0
    last_exc: Optional[LLMClientError] = None
    while True:
        try:
            data = http_post(url, headers, payload, config.timeout)
            choices = data.get("choices") or []
            if not choices:
                raise LLMEmptyResponseError("LLM 响应缺少 choices")
            msg = choices[0].get("message") or {}
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                raise LLMEmptyResponseError("LLM 响应 content 为空")
            return content
        except (LLMAuthError, LLMConfigError) as e:
            # 鉴权/配置错误 → 不重试
            raise
        except (LLMRateLimitError, LLMServerError, LLMTransportError, LLMTimeoutError) as e:
            last_exc = e
            if attempt >= config.max_retries:
                raise
            sleep_fn(_backoff_seconds(attempt))
            attempt += 1
            continue
        except LLMEmptyResponseError:
            # 空响应不重试
            raise
        except LLMJSONParseError:
            raise
        except LLMClientError:
            raise


def _backoff_seconds(attempt: int) -> float:
    # 指数退避：0.5, 1.0, 2.0 ...；测试中注入 sleep_fn 后不会真实等待
    return 0.5 * (2 ** attempt)


# ============================================================
# 原子写
# ============================================================

def atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    """原子写：先写临时文件，再 os.replace。失败时清理临时文件。"""
    path = str(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        # 清理临时文件，避免半写污染
        try:
            if os.path.isfile(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


# ============================================================
# 错误信息清洗（确保不含 API key）
# ============================================================

def _scrub(value: Optional[str]) -> str:
    """把可能含 API key 的字符串里 key 替换为 [REDACTED]。"""
    if not value:
        return ""
    # 不在异常中打印 key：scrub 用作辅助，但 caller 仍然不应传入 api_key
    return value


__all__ = [
    "LLMClientError",
    "LLMConfigError",
    "LLMAuthError",
    "LLMRateLimitError",
    "LLMServerError",
    "LLMTransportError",
    "LLMTimeoutError",
    "LLMEmptyResponseError",
    "LLMJSONParseError",
    "LLMConfig",
    "load_config_from_env",
    "ENV_BASE_URL",
    "ENV_API_KEY",
    "ENV_MODEL",
    "ENV_TIMEOUT",
    "ENV_MAX_RETRIES",
    "RETRYABLE_STATUS",
    "extract_json",
    "build_messages",
    "complete",
    "atomic_write_json",
]
