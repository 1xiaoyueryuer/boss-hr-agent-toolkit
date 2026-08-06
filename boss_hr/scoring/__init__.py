"""boss_hr.scoring — 评分 v2 子包。

公开模块：
  policy       单一评分策略源（scoring-policy.json + hash）
  llm_client   OpenAI-compatible LLM adapter（无状态 + 原子写 + 重试）
               + 持久化配置文件加载（~/.boss-hr/llm.env）
  scorecard    JD 评分卡生成与复用（hash 校验）
  evaluator    候选人证据评估 + Python 确定性评分
  decision     A/B/C 判定 + decision_label
  runner       boss-hr score v2 状态机入口（被 application 调）
"""
from . import policy, llm_client, scorecard, evaluator, decision, runner  # noqa: F401

# 把持久化配置文件加载 API 提升到顶层，方便 application / 集成测试
# 直接 `from boss_hr.scoring import load_llm_config`。
from .llm_client import (  # noqa: F401
    ALLOWED_FILE_KEYS,
    DEFAULT_CONFIG_PATH,
    LLMClientError,
    LLMConfig,
    LLMConfigError,
    ENV_BASE_URL,
    ENV_API_KEY,
    ENV_MODEL,
    ENV_TIMEOUT,
    ENV_MAX_RETRIES,
    load_config_from_env,
    load_llm_config,
    load_llm_env_file,
    parse_llm_env_file,
    resolve_default_config_path,
)
