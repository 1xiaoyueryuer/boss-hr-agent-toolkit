"""boss_hr.scoring — 评分 v2 子包。

公开模块：
  policy       单一评分策略源（scoring-policy.json + hash）
  llm_client   OpenAI-compatible LLM adapter（无状态 + 原子写 + 重试）
  scorecard    JD 评分卡生成与复用（hash 校验）
  evaluator    候选人证据评估 + Python 确定性评分
  decision     A/B/C 判定 + decision_label
  runner       boss-hr score v2 状态机入口（被 application 调）
"""
from . import policy, llm_client, scorecard, evaluator, decision, runner  # noqa: F401
