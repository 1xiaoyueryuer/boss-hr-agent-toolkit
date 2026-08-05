---
name: resume-screener
description: |
  简历筛选评分系统的**v2 实现参考**。v2 评分由 `boss-hr score` 命令内部的
  独立 LLM worker 完成。LLM 只做"岗位 requirement ↔ 简历证据"映射；Python
  确定性算 job_match_score / education_score / final_score / A/B/C 决策。

  **本 Skill 不是工作流入口**。通用智能体应只通过
  [boss-hr-auto](../boss-hr-auto/SKILL.md) → 统一 CLI `boss-hr score` 调用。
  本文档保留作为：v2 评分规则参考 / 算法实现参考 / LLM prompt 来源 / 单元测试依据。

  **v2 关键不变量**：
  - 评分 worker 是 `boss_hr.scoring` 子包内部能力
  - 不是 Claude Code / ChatGPT / Cursor / MiniMax Code 等父 agent 的子 agent
  - 不依赖任何 agent 框架的 subagent / session / thread API
  - 通用 agent 仍只调用 `boss-hr` CLI
  - 更换 OpenAI-compatible 模型只改环境变量
  - 不需要为不同 agent 写 adapter 代码
---

# Resume Screener（v2 实现参考）

## 评分数据流（v2）

```
new_resumes.json
  → prepare_scoring_inputs.py → scoring/{inputs,outputs,manifest.json}
  → boss-hr score（v2 内部，独立 LLM worker）
      ├─ ensure_scorecard：读 job_scorecard.json；不存在则调 LLM 生成
      ├─ 找下一位无合法 output 的候选人
      ├─ LLM 评估该候选人对每条 requirement 的 evidence mapping
      ├─ Python 算分：best_track、job_match_score、education_score、final_score
      ├─ Python 决策：A/B/C（基于 must_not_met、core_not_met、job_match_score、education_hard_pass）
      ├─ 原子写 scoring/outputs/candidate_<geek_id>.json（v2 schema）
      └─ 返回 status=candidate_scored
  → 全部合法后：合成 process/screening_results.json（v2 schema）
  → boss-hr report → HTML 报告（A/B/C 分组）
  → boss-hr greet → 只招呼 A/B（C 永排除）
```

## 评分公式（v2）

```
job_match_score = Σ(weight × status_value) / Σ(weight) × 100
  其中 weight ∈ {must: 5, core: 3, preferred: 1}
  status_value ∈ {met: 1.0, partial: 0.6, unverified: 0.5, not_met: 0.0}

final_score = job_match_score × 0.85 + education_score × 0.15
```

A/B/C 决策（不只看 final_score）：

- **A**：所有 must/core=met + job_match_score ≥ 85 + education_hard_pass
- **B**：must=not_met 缺 + job_match_score ≥ 60 + 允许 partial/unverified
- **C**：任何 must=not_met / job_match_score < 60 / 学历硬门槛失败 / 找不到合理方向

## LLM 输入约束

LLM 只做"requirement ↔ 简历证据"映射；不做：
- 输出 0-100 自由分（v1 旧行为）
- 替候选人做 A/B/C 决定
- 用公司名 / 学校名替代能力证据
- 复制粘贴相同 reason
- "建议转岗 / 培养 / 尝试"等软化话术

status 严格语义：
- **met**：直接、具体、可核对的经历证据。关键词命中 ≠ met。
- **partial**：覆盖部分但深度 / 范围 / 角色不足。
- **unverified**：简历没写 ≠ 不会。转化为 B 类面试问题。
- **not_met**：现有经历明确不符。

## JD 评分卡（每个 run 一次）

`scoring/job_scorecard.json`：

```json
{
  "schema_version": "2.0",
  "policy_version": "abc-education-v1",
  "policy_hash": "<sha256>",
  "source_jd_hash": "<sha256 of normalized JD>",
  "job_name": "...",
  "common_requirements": [
    {"id": "common_001", "description": "...", "importance": "must|core|preferred",
     "source_evidence": "JD 原文片段"}
  ],
  "alternative_tracks": [
    {"id": "braking", "name": "线控制动",
     "requirements": [{"id": "braking_001", "description": "...",
                       "importance": "...", "source_evidence": "..."}]}
  ],
  "education_requirements": {"minimum_degree": "本科及以上", "source_evidence": "..."},
  "generated_at": "..."
}
```

**多方向 JD**：`common_requirements`（共同）+ `alternative_tracks`（每个方向独立）。
单方向 JD 自动生成 `general` track。

**必须性**：`must` 只能来自 JD 明确"必须 / 硬性 / 最低 / 必备 / 要求"措辞；
不得因为"行业里一般很重要"就升为 must。

复用条件：scorecard 文件存在 + schema_version=2.0 + policy_hash 与当前策略一致
+ source_jd_hash 与当前 JD 规范化后一致。

## 候选人 evidence 评估

请求中必须要求模型分别评估所有 common_requirements 和每个 alternative_tracks 的 requirements。
**禁止**让 LLM 在 evidence 分析前凭岗位名称直接选定方向。

每条 assessment 字段：
- requirement_id（与 scorecard 完全一致）
- status（met/partial/unverified/not_met）
- evidence（具体事实；可空 list；不得编造）
- reason（一两句话，禁止套话）

## 确定性收尾

LLM 只输出 `assessments`。所有分数由 Python 算：
- 每个 track 分别算 job_match_score
- 选 best_track：score 最高且 must=not_met 为空
- 学历：school_tier 查表 + degree_bonus（硕士 +8 / 博士 +12，封顶 100）
- 未收录学校：使用 policy 的 `unknown_school_score` 中性分 + `education_review_required=true`
- final_score = job_match × 0.85 + education × 0.15
- 学历硬门槛：JD 规定最低学历时单独检查（与软偏好分离）

## candidate output schema（v2）

`scoring/outputs/candidate_<geek_id>.json`：

```json
{
  "schema_version": "2.0",
  "policy_version": "abc-education-v1",
  "policy_hash": "<sha256>",
  "job_scorecard_hash": "<sha256>",
  "name": "...",
  "geek_id": "...",
  "job_id": "...",
  "common_assessments": [...],
  "track_assessments": [...],
  "track_results": {"<track_id>": {"score": ..., "must_not_met": [...], "core_not_met": [...], ...}},
  "best_track": "<track_id>",
  "best_track_reason": "...",
  "job_match_score": 82.4,
  "education_score": 85,
  "education_tier": "985",
  "education_reason": "...",
  "education_review_required": false,
  "education_hard_pass": true,
  "final_score": 82.8,
  "decision": "A|B|C",
  "decision_label": "...",
  "decision_reasons": ["..."],
  "highlights": [...],
  "concerns": [...],
  "verification_questions": [...],
  "contact_recommended": true
}
```

校验：
- geek_id 必须与 manifest 一致
- job_id 必须与当前 encrypt_job_id 一致
- requirement ID 必须来自当前 scorecard
- 每个适用 requirement 必须出现且只能出现一次
- 未知 ID / 重复 ID 拒绝

## collect 与 finalize（v2 已退役）

- `collect_llm_scores.py`（v1）：v2 协议**不再调用**。保留脚本以兼容 v1 旧 run。
- `score_resumes.py`（v1 公式 edu25/exp25/skill25/proj15/major10）：v2 协议**不再调用**。
  v1 旧 run 仍可读 v1 旧 screening_results.json。
- v2 `screening_results.json` 由 `boss_hr.scoring.runner.run_score_v2` 内部合成。

## 评分策略单一来源

`resume-screener/scoring-policy.json`：

```json
{
  "schema_version": "1.0",
  "policy_version": "abc-education-v1",
  "job_match_weight": 0.85,
  "education_weight": 0.15,
  "unknown_school_score": 70,
  "importance_weights": {"must": 5, "core": 3, "preferred": 1},
  "status_values": {"met": 1.0, "partial": 0.6, "unverified": 0.5, "not_met": 0.0},
  "degree_bonus": {"硕士": 8, "博士": 12}
}
```

- `job_match_weight + education_weight == 1.0` 校验失败 → 拒绝启动；
- 任何脚本硬编码权重都是 bug；
- 策略变化 → `policy_hash` 变 → 旧 output 不得被当成本策略下有效结果。

## LLM 评分输入 schema（来自 scorecard 字段）

每条 requirement 评估结构：
```json
{
  "requirement_id": "<id>",
  "status": "met|partial|unverified|not_met",
  "evidence": ["简历中的具体事实"],
  "reason": "为什么这个 status"
}
```

每个适用 requirement 必须评估（不允许未知/重复/缺失）。

## 兼容性表

| 维度 | v1 | v2 |
|---|---|---|
| 评分主体 | 父 agent 自己调 LLM | `boss-hr score` 内部 worker |
| `input_file` / `output_file` 暴露 | 是 | 否 |
| 评分公式 | edu25/exp25/skill25/proj15/major10 | job_match×0.85 + education×0.15 |
| Tier | 推荐/待定/不推荐 | A/B/C |
| 学历 | 一维加权和 | 硬门槛 + 15% 软偏好 |
| collect_llm_scores | 必跑 | 退役（v2 不再调） |
| score_resumes | 必跑（v1 公式） | 退役（v2 runner 自己合成） |
| 政策 | WEIGHTS 硬编码 | scoring-policy.json（单一来源） |
| LLM 评估 | exp/skill/proj/major 4 维 | requirement evidence 映射 |

v1 run 的 `screening_results.json` 仍按 v1 公式生成；v2 不迁移也不改写。
