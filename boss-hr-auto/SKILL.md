---
name: boss-hr-auto
description: |
  BOSS 直聘 HR 简历筛选全流程编排。当用户要求"筛选简历"、"跑 5 步流程"、
  "从岗位到报告"时使用。

  **触发场景**：
  - "筛选简历" / "筛一下这个岗位" / "帮我筛选候选人"
  - BOSS 直聘 HR 工具包全流程一次跑完

  **不触发场景**：
  - 仅问单条消息怎么回复（直接用 message 工具）
  - 非 BOSS 直聘的其他招聘平台

  **行为边界**：详见 [docs/BEHAVIOR_V1.md](../docs/BEHAVIOR_V1.md)。
  v2 评分协议：见本 Skill "v2 评分协议" 章节。
  智能体只通过统一 CLI `boss-hr` 调用；不直接调底层脚本。

  **编排入口**：统一 CLI `boss-hr`。
type: workflow
---

# BOSS HR 统一筛选流程（v2 评分）

> 入口：`boss-hr` 统一 CLI。下文按步骤顺序逐个调用公开命令。
>
> **v2 关键变化**：评分由 `boss-hr score` 内部独立 LLM worker 完成。**父 agent 不再**
> 读 `input_file` / 写 `output_file` / 调用任何模型。一次 score 调一次 LLM 评一位候选人，
> 状态在 `candidate_scored` 与 `scoring_complete` 之间循环。

## 行为边界

**支持**：

- 一次完整的新筛选任务；
- start → confirm → fetch → score → report；
- 用户明确要求时执行 greet。

**不支持**：

- continue / batch / 多批累计；
- 自动查找最新 run；
- 从其他 run 补数据；
- 自动跳过人工确认门；
- 父 agent 自行调 LLM 给候选人打分；
- `--only-names` 把 C 类候选人加入招呼名单（v2 强制 C 永排除）；
- 任何 OpenAI / Anthropic / Claude / GPT / Gemini / ChatGPT / Cursor /
  MiniMax Code / Claude Code 专属代码路径。

## 公开命令（v2）

| 命令 | 作用 |
|---|---|
| `boss-hr doctor` | 环境健康检查（首次或环境未知时先调） |
| `boss-hr start` | 创建新 run，停在人工确认门 |
| `boss-hr confirm` | 把 `confirmed` 翻 true |
| `boss-hr fetch --count N` | 拉候选人列表 + 下载 N 份简历 |
| `boss-hr score` | v2 评分（每调一次评一位，状态 candidate_scored / scoring_complete） |
| `boss-hr report` | 生成 A/B/C HTML 报告 |
| `boss-hr greet` | 给 v2 A/B 候选人打招呼（C 永远排除） |
| `boss-hr status` | 读 `runs/<run_id>/run.json` + process 目录 |

**禁止调用旧脚本**：`boss_jd.py` / `confirm_run.py` / `recommend_list.py` /
`recommend_download.py` / `prepare_scoring_inputs.py` / `collect_llm_scores.py` /
`score_resumes.py` / `generate_html_report.py` / `auto_greet.py` /
`cli_runner.py` / spec JSON。

**禁止**：

- 直接调用 LLM API 评分；
- 自己写候选人 output 文件；
- 读取 `runs/<run_id>/process/scoring/outputs/candidate_*.json`（父 agent 看不到这个）；
- 设置 `boss-hr` 任何未公开的内部参数；
- 任何对 7 个旧业务脚本的 subprocess 调用。

## v2 评分协议

`boss-hr score` 一次最多处理一位候选人。状态机：

```
candidate_scored  ←→ repeat_score（评完一位）
       ↓
scoring_complete → report（全部完成）
```

- 父 agent 不参与候选人评分；
- 评分上下文**只在 CLI 内部**，不写入聊天历史；
- 每次 `boss-hr score` 必须是**完全相同**的命令字面（`--job-name` / `--encrypt-job-id` / `--run-id`）；
- 父 agent 收到 `candidate_scored` → 重跑完全相同 `boss-hr score`；
- 父 agent 收到 `scoring_complete` → 调 `boss-hr report`；
- 父 agent **不能**自己调 `boss-hr score` 然后读 `output_file` 改写评分。

## v2 评分数据流（父 agent 视角）

```
boss-hr score
  ├─ 读 job_scorecard.json（若不存在则由 score CLI 内部调用 LLM 生成；不暴露）
  ├─ 读 scoring/manifest.json（候选人清单）
  ├─ 读一位候选人 inputs/candidate_<geek_id>.json（CLI 内部完成，父 agent 看不到）
  ├─ LLM 评估（独立 worker，无状态）+ Python 确定性算分 + A/B/C 决策
  ├─ 写 scoring/outputs/candidate_<geek_id>.json（v2 schema，原子写）
  └─ 返回 status=candidate_scored

boss-hr score（再次）
  ├─ 全部合法 → finalize：合成 screening_results.json（v2 schema）
  └─ 返回 status=scoring_complete
```

## 浏览器与登录态

正常流程直接 `boss-hr start`，不需先 doctor。start / fetch / greet 自动启动专用 Edge
（`%LOCALAPPDATA%\boss-hr-edge-profile` + `--remote-debugging-port=9222`），
并在 BOSS 登录页轮询等待（默认 20s）。超时返回 `status=waiting_user_login`（**不是错误**），
`next_action=retry_same_command`，**不创建 run**。

`boss-hr doctor` 仍是诊断工具，仅在自动启动失败时排查。

## 标准流程

### 1. 开始任务

```bash
boss-hr start "<岗位名称 | jobId | encryptJobId>" \
  --job-name "<BOSS 真名>" --encrypt-job-id "<id>"
```

**start 不接受 `--run-id`**。每次 start 必须创建新 run。
期望返回 `status=waiting_user_confirmation` + `run_id`。**智能体立即停下**，告知用户
在 BOSS 推荐牛人页面调整筛选条件。

### 2. 用户回复"继续"

```bash
boss-hr confirm --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
boss-hr fetch   --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>" --count N
```

### 3. 评分循环

```bash
boss-hr score --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
# 返回 status=candidate_scored, data.candidate_id, data.decision, data.final_score, data.remaining
# 智能体只需重跑完全相同的命令
boss-hr score --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
# 直到 status=scoring_complete, data.scored, data.screening_results_file
```

**不要**：读取 input_file / 自己调 LLM / 写 output_file。

### 4. 报告

```bash
boss-hr report --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
```

期望返回 `status=report_ready` + `data.report_file`。**report 不自动调 greet**。

### 5. 打招呼（用户明确要求时）

```bash
boss-hr greet --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>" \
  [--only-names "张三,李四"] [--threshold 70] [--max 10] [--dry-run]
```

v2 行为：
- 仅 A / B 类候选人入选；**C 永远排除**（即使 `--threshold=0`）；
- `--only-names` 不能把 C 类加入名单；
- v1 旧 run 的 screening_results.json 仍按 `score >= threshold` 兼容；
- encryptGeekId 是唯一身份；姓名仅作 fallback；
- 渐进扫描、不刷新推荐页、同名防误发、partial_success 语义保持不变。

## 铁律

| # | 规则 |
|---|------|
| 1 | 新任务必须调 `start`；start 不接受旧 run_id |
| 2 | start 后必须停下，等用户回复"继续" |
| 3 | 所有下游命令必须显式使用同一个 `run_id` |
| 4 | 禁止扫描 `runs/` 猜 run_id |
| 5 | 禁止读 `current_run.json`（已废弃） |
| 6 | 禁止借用其他 run 的产物 |
| 7 | 禁止创建 `spec_*.json` 模板 |
| 8 | 禁止直接调旧业务脚本 |
| 9 | 禁止调 `cli_runner` 或 `shared.cli_runner.run_python_cli` |
| 10 | 禁止自动 greet（必须用户明确批准） |
| 11 | 禁止 continue / batch / 多批累计 |
| 12 | **禁止父 agent 参与候选人评分**（v2 强制） |
| 13 | 禁止 `--only-names` 把 C 类加入招呼名单（v2 强制） |
| 14 | 禁止为不同 agent 写 LLM adapter（项目内已实现 OpenAI-compatible 唯一 adapter） |

## 状态处理

每个命令返回的 `status` 字段决定下一步：

| 返回 status | 含义 | 智能体动作 |
|---|---|---|
| `waiting_user_confirmation` | start 完成，等用户回复"继续" | **停下**，告知用户去 BOSS 推荐牛人页面调整筛选 |
| `waiting_user_login` | start 自动启动 Edge 后用户未登录超时 | **停下**，告知用户在专用 Edge 中登录后重试同一条 start |
| `confirmed` | confirm 完成 | 进入 fetch |
| `candidates_fetched` | fetch 完成 | 进入 score 循环 |
| `candidate_scored` | score 评完一位候选人 | 重跑完全相同的 `boss-hr score` |
| `scoring_complete` | 评分收尾完成 | 进入 report |
| `report_ready` | HTML 报告已生成 | 把 `report_file` 告诉用户；**不**自动 greet |
| `greet_complete` / `no_candidates` | 本次 greet 命令结束 | 任务结束 |
| （任意 `ok=false`） | 错误 | 见下方错误处理 |

`next_action="done"` 只表示"当前 CLI 工作流无下一项自动动作"，**不等于** `run.json.finished=true`。
只有 `maybe_finish()` 在 `greeted>=1` 且 `orch.finish(run_id=...)` 成功时被设置
`run.json.finished=true`。

## 错误处理

统一 CLI 返回非零退出码时：

1. **先**读取 `error.code`：
   - `EDGE_NOT_FOUND` / `CDP_NOT_RUNNING` / `CDP_CONNECT_FAILED` → 让用户按 `remediation` 启动 Edge / 重连
   - `BOSS_LOGIN_REQUIRED` → 让用户在专用 Edge 中扫码登录
   - `BOSS_PAGE_REQUIRED` → 让用户打开 BOSS 招聘者页面
   - `JOB_NOT_FOUND` / `JOB_AMBIGUOUS` / `JOB_ID_MISMATCH` → 按 `data.candidates` 或 `remediation.instructions` 重新提供 query
   - `LLM_CONFIG_ERROR` → 让用户设置 `BOSS_HR_LLM_BASE_URL` / `_API_KEY` / `_MODEL` 三个环境变量（参考 §手工 smoke 验证）
2. 检查 `error.recoverable`：若 `true` 才有可执行恢复路径
3. 按 `error.next_action` / `error.remediation` 引导用户

**禁止**：

- 读 `boss_hr` 源码
- 直接调用旧业务脚本
- 用历史 JSON（`jobs.json` / `run.json` / `job_detail.json`）绕过错误
- 把 run 状态（`confirmed` / `finished`）手工改写

常见退出码：`1`（业务层）/ `2`（argparse 缺必填）/ `20`（未 confirm 跑 fetch）/
`23`（run 不存在）/ `24`（run 与岗位不匹配）/ `26`（缺输入文件）/ `27`（缺输出文件）/
`99`（内部错误，含 LLM 调用失败 / 评分 schema 不合法 / 校验错误等）。

## 手工 smoke 验证（用户如何手动验证 v2 评分）

v2 评分需要 LLM API；用户手工验证请设置以下环境变量：

```powershell
$env:BOSS_HR_LLM_BASE_URL="https://你的-OpenAI-compatible-端点/v1"
$env:BOSS_HR_LLM_API_KEY="你的-API-key"
$env:BOSS_HR_LLM_MODEL="模型名"
$env:BOSS_HR_LLM_TIMEOUT="120"
```

四个变量是必填的，**API key 不允许通过 CLI 参数传入**。

然后：

```powershell
# 1. start → confirm → fetch（与 v1 一致）
boss-hr start "<岗位名>" --job-name "<>" --encrypt-job-id "<>"
boss-hr confirm --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
boss-hr fetch --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>" --count 5

# 2. score 循环
boss-hr score --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
# → status=candidate_scored，重复到 status=scoring_complete

# 3. 检查产物
Get-Content "<workspace>\<eid>\runs\<rid>\process\scoring\job_scorecard.json"
Get-Content "<workspace>\<eid>\runs\<rid>\process\scoring\outputs\candidate_*.json"
Get-Content "<workspace>\<eid>\runs\<rid>\process\_llm_scores.json"  # v2 不再写此文件
Get-Content "<workspace>\<eid>\runs\<rid>\process\screening_results.json"

# 4. report → greet
boss-hr report --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
boss-hr greet --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>" --dry-run
```

## 与 v1 的差异

| 项 | v1 | v2 |
|---|---|---|
| 评分主体 | 父 agent 读 input_file + 调自己的 LLM + 写 output_file | `boss-hr score` 内部独立 worker |
| `input_file` / `output_file` 暴露给父 agent | 是 | **否**（v2 隐藏） |
| 评分维度 | exp/skill/proj/major 4 个 0-100 | requirement 证据映射 + Python 算分 |
| 公式 | edu 25% + exp 25% + skill 25% + proj 15% + major 10% | `job_match_score × 0.85 + education_score × 0.15` |
| Tier | 推荐 ≥70 / 待定 60-69 / 不推荐 <60 | A / B / C 决策（不仅看 final_score） |
| education 角色 | 一维加权和 | 硬门槛（JD 规定时）+ 15% 软偏好 |
| score 状态 | `waiting_llm` 循环 | `candidate_scored` 循环 |
| collect 步骤 | `collect_llm_scores.py` | v2 不再调 |
| LLM adapter | 由 agent 自己提供 | 项目内 OpenAI-compatible 唯一 adapter |
| greet 资格 | score >= threshold | v2 用 `contact_recommended`（A/B）；C 永排除 |

## v1 旧 run 的兼容性

- v1 run 的 `screening_results.json`（v1 schema：推荐/待定/不推荐 + total）仍可读；
- HTML 报告 v2 渲染器自动检测 `schema_version`：
  - `2.0` → A/B/C 分组 + 评分卡摘要
  - 其它 / 缺省 → v1 旧渲染（推荐/待定/不推荐 + 五维）
- greet v2 兼容：v1 run 按 `score >= threshold` 入选；v2 run 按 `decision in (A, B)` 入选。
- v1 旧 run 不会被迁移到 v2 schema（**只读不迁移**）。
