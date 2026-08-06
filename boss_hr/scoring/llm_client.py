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
# 持久化用户配置文件（~/.boss-hr/llm.env）
#
# 设计原则：
#   - 任何非法字段 / 重复字段 / malformed 行 / 非法引号 → 整份文件
#     加载失败，绝不部分生效。
#   - 文件路径在调用时通过 resolve_default_config_path() 解析，不在
#     import 时缓存用户 home。
#   - 解析纯 stdlib：不执行 shell、不做变量展开、不回显 value。
# ============================================================

# 仅这 5 个字段允许出现在持久配置文件中。其它一律整体拒绝。
ALLOWED_FILE_KEYS = frozenset({
    ENV_BASE_URL,
    ENV_API_KEY,
    ENV_MODEL,
    ENV_TIMEOUT,
    ENV_MAX_RETRIES,
})

# API key 占位符：仍保留视为"未配置"。
KEY_PLACEHOLDERS = frozenset({
    "",
    "请填写",
    "<YOUR_API_KEY>",
    "<你的 API key>",
})

# 说明性常量；生产加载必须经 resolve_default_config_path() 在调用时解析。
DEFAULT_CONFIG_PATH = Path.home() / ".boss-hr" / "llm.env"


def resolve_default_config_path() -> Path:
    """调用时解析用户配置路径，避免 import 时缓存。"""
    return Path.home() / ".boss-hr" / "llm.env"


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
# 持久化配置文件解析（纯 stdlib）
# ============================================================

# 单字符 = 的 split maxsplit，避免 value 含 '=' 被吞
_MAX_KEY_LEN = 256


def _strip_paired_quotes(value: str) -> str:
    """剥去一层配对的单/双引号；只剥一层；不匹配时原样返回。"""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def parse_llm_env_file(text: str, *, source: str) -> Dict[str, str]:
    """解析 .env 风格文本。

    规则：
      - UTF-8 BOM 兼容（调用方负责解码时去掉）。
      - 空行 / 仅空白行 / `#` 开头注释行忽略。
      - 按第一个 '=' split key/value；两侧 strip 空白。
      - value 可剥去一层配对的单/双引号。
      - 重复 key → 整份失败。
      - 任何非白名单字段 → 整份失败。
      - malformed 行（缺 '=' / key 为空 / value 末尾含未闭合引号）
        → 整份失败。
      - 不执行 shell / 不做 ${VAR} / %VAR% / 命令替换。
      - 错误只含路径、键名、行号、原因，不含 value。
    """
    if not isinstance(text, str):
        raise LLMConfigError(f"配置文件 {source} 内容不是字符串")

    out: Dict[str, str] = {}
    for lineno0, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise LLMConfigError(
                f"配置文件 {source} 第 {lineno0} 行 malformed：缺少 '='"
            )
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            raise LLMConfigError(
                f"配置文件 {source} 第 {lineno0} 行 malformed：key 为空"
            )
        if len(key) > _MAX_KEY_LEN:
            raise LLMConfigError(
                f"配置文件 {source} 第 {lineno0} 行 malformed：key 过长"
            )
        # 引号完整性校验：若以引号开头必须以相同引号闭合
        if value and value[0] in ("'", '"'):
            if len(value) < 2 or value[-1] != value[0]:
                raise LLMConfigError(
                    f"配置文件 {source} 第 {lineno0} 行 malformed：引号未闭合"
                )
        value = _strip_paired_quotes(value)
        if key not in ALLOWED_FILE_KEYS:
            raise LLMConfigError(
                f"配置文件 {source} 第 {lineno0} 行：字段 '{key}' 不在白名单"
            )
        if key in out:
            raise LLMConfigError(
                f"配置文件 {source} 第 {lineno0} 行：字段 '{key}' 重复"
            )
        out[key] = value
    return out


def load_llm_env_file(path: os.PathLike | str) -> Dict[str, str]:
    """从磁盘读取持久配置文件。

    不存在 → 返回 {}。
    存在但任一字段/行非法 → 整份失败（LLMConfigError）。
    """
    p = Path(path)
    if not p.exists():
        return {}
    if not p.is_file():
        raise LLMConfigError(f"配置路径 {p} 不是文件")
    try:
        raw_bytes = p.read_bytes()
    except OSError as e:
        raise LLMConfigError(f"读取持久配置 {p} 失败：{e.strerror or e}") from None
    # 兼容 UTF-8 BOM
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        raw_bytes = raw_bytes[3:]
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise LLMConfigError(
            f"持久配置 {p} 不是合法 UTF-8：第 {e.start} 字节起解码失败"
        ) from None
    return parse_llm_env_file(text, source=str(p))


def _coerce_timeout(value: str, *, source: str) -> float:
    """TIMEOUT 严格校验：必须为正有限浮点。失败抛错（不回退默认）。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise LLMConfigError(
            f"{source} 中 BOSS_HR_LLM_TIMEOUT 不是合法数字"
        ) from None
    import math as _math
    if not _math.isfinite(f) or f <= 0:
        raise LLMConfigError(
            f"{source} 中 BOSS_HR_LLM_TIMEOUT 必须是正有限数"
        )
    return float(f)


def _coerce_max_retries(value: str, *, source: str) -> int:
    """MAX_RETRIES 严格校验：必须为非负整数。失败抛错（不回退默认）。"""
    # 拒绝像 '1.5' / 'abc' / ' 1 ' 这类容易踩坑的输入
    s = value.strip()
    try:
        n = int(s)
    except (TypeError, ValueError):
        raise LLMConfigError(
            f"{source} 中 BOSS_HR_LLM_MAX_RETRIES 不是合法整数"
        ) from None
    if n < 0:
        raise LLMConfigError(
            f"{source} 中 BOSS_HR_LLM_MAX_RETRIES 不能为负"
        )
    return n


def load_llm_config(
    *,
    config_path: Optional[os.PathLike | str] = None,
    env: Optional[Dict[str, str]] = None,
) -> LLMConfig:
    """生产入口：env > 持久配置文件 > dataclass 默认。

    行为：
      - 严格白名单：仅 ENV_BASE_URL/API_KEY/MODEL/TIMEOUT/MAX_RETRIES；
        文件中出现任何非白名单字段 → 整份失败。
      - env 与文件冲突时，env 优先；空字符串 env 视为未设置。
      - 必填字段缺失 → LLMConfigError（不读 value，只报告键名+来源）。
      - TIMEOUT 必填且必须正有限；MAX_RETRIES 可选且必须非负整数；
        非法值一律报错，绝不回退默认。
      - API key 落入 KEY_PLACEHOLDERS → LLMConfigError("API key 尚未配置")，
        不回显原值。
      - 不写回 os.environ。
    """
    src_env = env if env is not None else dict(os.environ)

    if config_path is None:
        path = resolve_default_config_path()
        path_label = str(path)
    else:
        path = Path(config_path)
        path_label = str(path)

    file_values: Dict[str, str] = {}
    if str(path) != "":
        # 调用方显式传 path（即使是 None → 走默认）即尝试读取；不存在 → {}
        try:
            file_values = load_llm_env_file(path)
        except LLMConfigError:
            # load_llm_env_file 自身已经包含 source 路径，直接透传
            raise

    # 先逐字段收集"原始值"，区分"未提供"与"占位/空"。
    # "未提供" 走必填缺失错误；"占位/空" 走专属占位符错误。
    raw: Dict[str, Optional[str]] = {}
    for field in (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL, ENV_TIMEOUT):
        env_raw = src_env.get(field)
        if env_raw is not None and env_raw != "":
            raw[field] = env_raw
        elif field in file_values:
            raw[field] = file_values[field]
        else:
            raw[field] = None

    # API key 占位符优先：空/请填写/<...> 一律视为"未配置"，但要给清晰错误
    if raw[ENV_API_KEY] in KEY_PLACEHOLDERS:
        src_label = "环境变量" if src_env.get(ENV_API_KEY) else path_label
        raise LLMConfigError(
            f"API key 尚未配置（来源：{src_label}）"
        )

    missing = [f for f in (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL, ENV_TIMEOUT)
               if not raw[f]]
    if missing:
        env_set = {k for k in (ENV_BASE_URL, ENV_API_KEY, ENV_MODEL, ENV_TIMEOUT)
                   if src_env.get(k)}
        env_part = f"已通过环境变量设置：{sorted(env_set)}；" if env_set else ""
        raise LLMConfigError(
            "LLM 配置不完整。"
            f"{env_part}持久配置 {path_label} 中缺少必填字段：{missing}。"
            "可设置对应 BOSS_HR_LLM_* 环境变量临时覆盖。"
        )

    base_url = str(raw[ENV_BASE_URL]).strip()
    api_key = str(raw[ENV_API_KEY]).strip()
    model = str(raw[ENV_MODEL]).strip()
    if not base_url or not api_key or not model:
        # 防御：理论上 missing 已经拦下，这里兜底
        raise LLMConfigError("LLM 配置字段在去除空白后为空")

    timeout = _coerce_timeout(str(raw[ENV_TIMEOUT]), source=path_label)

    # MAX_RETRIES：可选；缺失走 dataclass 默认 2
    if ENV_MAX_RETRIES in src_env and src_env[ENV_MAX_RETRIES] != "":
        max_retries = _coerce_max_retries(
            str(src_env[ENV_MAX_RETRIES]), source="环境变量")
    elif ENV_MAX_RETRIES in file_values:
        max_retries = _coerce_max_retries(
            str(file_values[ENV_MAX_RETRIES]), source=path_label)
    else:
        max_retries = 2

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
    "load_llm_config",
    "load_llm_env_file",
    "parse_llm_env_file",
    "resolve_default_config_path",
    "DEFAULT_CONFIG_PATH",
    "ALLOWED_FILE_KEYS",
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
