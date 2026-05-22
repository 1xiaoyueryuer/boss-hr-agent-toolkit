---
name: boss-hr-auto
description: |
  **这是整个 BOSS 直聘 HR Skill 包的唯一入口。** BOSS 直聘 HR 简历筛选全流程自动化编排。当用户要求"筛选简历"、走完整流程时使用。

  **触发场景**：
  - "筛选简历" / "筛一下这个岗位" / "帮我筛选候选人"
  - 需要从 BOSS 岗位提取 JD → 下载简历 → 评分 → 生成报告
  - 任何"一条龙"简历筛选需求

  **不触发场景**：
  - 仅问单条消息怎么回复（直接用 message 工具）
  - 非 BOSS 直聘的其他招聘平台

  **子 Skill 说明**：本项目的 boss-job-detail、boss-resume-downloader、resume-screener、boss-cli 均为此编排流程的子步骤，不应作为入口直接加载。请始终先加载本 Skill 获取完整工作流，再按 Step 顺序调用子 Skill。
type: workflow
---

# BOSS 直聘 HR 简历筛选全流程

> **🚪 入口声明**：本 Skill 是 hrskill 项目唯一入口。下文的 4 个子 Skill（boss-job-detail、boss-resume-downloader、resume-screener、boss-cli）均应按本 Skill 的编排顺序调用，不得作为独立入口加载。

## 流程总览

```
用户提供岗位ID/链接
     │
     ▼
[Step 1] 提取 JD ──── 使用 skill: boss-job-detail
     │
     ▼
[Step 2] 下载简历 ── 使用 skill: boss-resume-downloader
     │
     ▼
[Step 3] 评分 ────── 使用 skill: resume-screener
     │
     ▼
[Step 4] 生成报告 ── 汇总评分结构报告 + 沟通建议
```

**❌ 不做：自动回复/打招呼**（CLI reply 权限不足，由用户在 BOSS 网页端操作）

---

## 用到的 Skill 列表

| # | Skill | 路径 | 在流程中的作用 |
|:-:|:------|:-----|:-------------|
| 1 | **boss-job-detail** | `skills/boss-job-detail/` | Step 1：CDP+iframe 提取完整岗位 JD |
| 2 | **boss-resume-downloader** | `skills/boss-resume-downloader/` | Step 2：批量下载候选人简历，防封去重 |
| 3 | **resume-screener** | `skills/resume-screener/` | Step 3：岗位类型判断→硬门槛过滤→加权评分→学历分级 |
| - | **boss-cli** | `skills/boss-cli/` | 基础：CLI 命令参考、双模式登录 |

---

## Step 1: 提取 JD

**执行 skill：** `boss-job-detail`

**怎么做：** 读取 `~/.openclaw-autoclaw/skills/boss-job-detail/SKILL.md` 获取完整说明。

**核心操作：**
```powershell
python "$env:USERPROFILE\.openclaw-autoclaw\skills\boss-job-detail\scripts\boss_jd.py" <encryptJobId 或 jobId>
```

**前置条件：**
- 招聘者身份已登录
- Edge 以 `--remote-debugging-port=9222 --remote-allow-origins=*` 运行

**输出：** 结构化 JD 数据（岗位名、学历、专业、经验、职责、技能栈），用于 Step 3 评分。

---

## Step 2: 下载候选人简历

**执行 skill：** `boss-resume-downloader`

**怎么做：** 读取 `~/.openclaw-autoclaw/skills/boss-resume-downloader/SKILL.md` 获取完整说明。

**核心操作：**
```powershell
# 查看岗位列表获取 jobId
boss --cdp-url http://localhost:9222 --role recruiter hr jobs list

# 分批下载（--max 是累计处理数，含已跳过）
python "$env:USERPROFILE\.openclaw-autoclaw\skills\boss-resume-downloader\scripts\sync_boss_resumes.py" sync-job --job-id <jobId> --max 10
```

**分批策略：**
- 第一轮 `--max 10` → 第二轮 `--max 20` → 第三轮 `--max 30`
- 脚本自带随机延迟防封

**输出目录：** `%USERPROFILE%\WorkBuddy\boss-resumes\jobs\<岗位名>\resumes\`

**已知限制：**
- 投递池中约 2% 候选人能解析 securityId 下载（实测 17/850）
- 只下载 BOSS 在线简历 JSON，不含 PDF 附件

---

## Step 3: 评分

**执行 skill：** `resume-screener`

**怎么做：** 读取 `~/.openclaw-autoclaw/skills/resume-screener/SKILL.md` 获取评分方法论。

**4 步执行：**

1. **岗位类型判断** — 技术岗 / 管培&非技术岗
2. **硬门槛过滤** — 学历不符 / 毕业年份不匹配 / 专业不相关 → 淘汰
3. **加权评分** — 按岗位类型选择 Mode A 或 Mode B 权重
4. **总分排名** — 结构化输出每个候选人的评分明细 + 排名表

**⚠️ 多批筛选规则：**
- 用户指定总分阈值时（如 ≥55 分保留），跨批次统一应用
- 第一批推荐的候选人需跨批次记住，最终汇总给出完整排名
- 两批都评分完成后才统一输出沟通建议

---

## Step 4: 生成报告

输出内容：
- 岗位基本信息 + JD 摘要
- 岗位类型判定 + 使用权重说明
- 硬门槛过滤结果表
- 每个候选人分维度评分明细表
- 总分排名表（ASCII 字符表格）
- 沟通建议：对推荐人选给出消息模板和联系方式

---

## 不做的步骤

| 步骤 | 原因 |
|:----|:-----|
| ❌ 自动发消息给候选人 | CLI `hr reply` 返回 `sent: false`，权限不足 |
| ❌ 自动请求简历 | CLI `request-resume` 返回"权限不足" |
| ❌ 自动打招呼 | 由用户自行在 BOSS 网页端操作 |

---

## 文件结构

```
%USERPROFILE%\WorkBuddy\boss-resumes\
├── jd\
│   └── <encryptJobId>.json                  # JD 数据
├── jobs\<岗位名>\
│   ├── candidate_index.json                 # 候选索引
│   └── resumes\
│       ├── <姓名>_<friendId>\
│       │   ├── resume.md                    # 可读简历
│       │   └── raw_response.json            # BOSS API 原始 JSON
│       └── ...
```

---

## ⚠️ 重要规则

### 登录
- 必须两段式登录：CDP 扫码 → `boss login` 拾取 session
- 验证：`boss me` 返回真实用户信息
- `boss status` 不可靠（可能假阳性）

### 登录后验证
```powershell
boss --cdp-url http://localhost:9222 --role recruiter me
```
必须返回真实招聘者信息才算登录成功。

### 编码
- BOSS CLI stdout 为 GBK 编码
- 禁止用 PowerShell 管道（`2>&1 |`）处理中文 → 乱码
- 看到乱码直接如实报告，不要猜测中文内容
- 需要保存到文件时用 JSON 写入，避免编码问题

### 模式切换
- 求职者模式：`boss ...`（默认）
- 招聘者（HR）模式：`boss --role recruiter hr ...`
- 不同模式命令不同，模式不对会超时或返回空

### 防封
- 简历下载每次只下一份，脚本自带随机延迟
- 不要连续快速操作同一接口
