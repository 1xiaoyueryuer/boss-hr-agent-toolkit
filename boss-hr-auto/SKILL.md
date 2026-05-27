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

  **子 Skill 说明**：本包的 boss-job-detail、boss-resume-downloader、resume-screener、boss-agent-cli 均为此编排流程的子步骤，不应作为入口直接加载。请始终先加载本 Skill 获取完整工作流，再按 Step 顺序调用子 Skill。
type: workflow
---

# BOSS 直聘 HR 简历筛选全流程

> **🚪 入口声明**：本 Skill 是 boss-hr-agent-toolkit 项目唯一入口。下文的 4 个子 Skill（boss-job-detail、boss-resume-downloader、resume-screener、boss-agent-cli）均应按本 Skill 的编排顺序调用，不得作为独立入口加载。

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

## 🔧 辅助脚本

| 脚本 | 位置 | 用途 |
|:----|:----|:----|
| `gen_handover_doc.py` | `scripts/gen_handover_doc.py` | 生成系统对接文档（docx），交接工作时用 |

---

## 🚀 从零开始：完整的环境准备（首次使用先做这个）

如果你电脑上什么都没有（没 Python、没 boss CLI、没装过任何东西），按以下顺序走一遍：

### 0.1 安装 Python 3.10+

```bash
# Windows: 从 python.org 下载安装包，安装时勾选 "Add Python to PATH"
# 验证：
python --version    # 应显示 Python 3.10+
```

### 0.2 安装 uv（Python 包管理，比 pip 快 10 倍）

```bash
# Windows PowerShell（管理员）：
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 或 pip install uv
# 验证：
uv --version
```

### 0.3 安装 boss-agent-cli

```bash
uv tool install boss-agent-cli
# 验证：
boss --help
```

### 0.4 安装 patchright（不下载浏览器，只用作 CDP 客户端）

```bash
pip install patchright
```

> 注意：浏览器是系统自带 Edge/Chrome，不需要额外下载。

---

**每次跑流程时，智能体自动完成以下步骤：**

### 启动 CDP 浏览器

**🤖 智能体自动做：** 用 `terminal(background=true)` 启动 Edge/Chrome，带上远程调试端口：

```bash
# Windows Edge
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%USERPROFILE%\.workbuddy\chrome-profiles\boss-cdp"

# 或 Chrome
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%USERPROFILE%\.workbuddy\chrome-profiles\boss-cdp"
```

启动后智能体验证 `http://localhost:9222/json/version` 是否返回浏览器信息。若浏览器已运行则跳过。

### 登录 BOSS 直聘

参见 `boss-agent-cli` skill 中「登录流程（双阶段策略）」章节：
- 有 Cookie → 注入浏览器
- 无 Cookie → 打开登录页，提示用户扫码

### 0.7 环境修复

```bash
# Windows 上 PYTHONHOME 可能冲突，必须清除
export PYTHONHOME=""
# 或在 ~/.profile 添加以上命令永久生效
```

---

## 用到的 Skill 列表

| # | Skill | 在流程中的作用 |
|:-:|:------|:-------------|
| 1 | **boss-job-detail** | Step 1：CDP+iframe 提取完整岗位 JD |
| 2 | **boss-resume-downloader** | Step 2：批量下载候选人简历，防封去重 |
| 3 | **resume-screener** | Step 3：岗位类型判断→硬门槛过滤→加权评分→学历分级 |
| - | **boss-agent-cli** | 基础：CLI 命令参考、双模式登录 |

---

## 登录流程

> 参考 `boss-agent-cli` skill 中的「登录流程（双阶段策略）」章节。
>
> **核心原则：** 有 Cookie 直接注入 → 无 Cookie 提示用户扫码。
>
> 不要在无 Cookie 时自行尝试注入或操控登录页面，直接告知用户「登录页面已打开，请扫码登录」。

## Step 1: 提取 JD

**执行 skill：** `boss-job-detail`

**前置条件：**
- 招聘者身份已登录
- Edge 以 `--remote-debugging-port=9222 --remote-allow-origins=*` 运行

**环境修复（Windows 兼容）：**
```bash
# PYTHONHOME 环境变量在 Windows 上可能冲突，需要清除
export PYTHONHOME=""
```

**核心操作：**
```bash
PYTHONHOME="" python scripts/boss_jd.py <encryptJobId 或 jobId 或 岗位名>
```

**输出：** 结构化 JD 数据（岗位名、学历、专业、经验、职责、技能栈），用于 Step 3 评分。

---

## Step 2: 下载候选人简历

**执行 skill：** `boss-resume-downloader`

**核心操作：**
```bash
# 查看岗位列表获取 jobId
boss --role recruiter --platform zhipin --cdp-url http://localhost:9222 hr jobs list

# 分批下载（--max 是累计处理数，含已跳过）
python scripts/sync_boss_resumes.py sync-job --job-id <jobId> --max 10
```

**分批策略：**
- 第一轮 `--max 10` → 第二轮 `--max 20` → 第三轮 `--max 30`
- 脚本自带随机延迟防封

**输出目录：** `%USERPROFILE%/WorkBuddy/boss-resumes/jobs/<岗位名>/resumes/`

---

## Step 3: 评分

**执行 skill：** `resume-screener`

**4 步执行：**
1. **岗位类型判断** — 技术岗 / 管培&非技术岗
2. **硬门槛过滤** — 学历不符 / 毕业年份不匹配 / 专业不相关 → 淘汰
3. **加权评分** — 按岗位类型选择 Mode A 或 Mode B 权重
4. **总分排名** — 结构化输出每个候选人的评分明细 + 排名表

---

## Step 4: 生成报告

**格式要求：** 生成**独立 HTML 文档**，不要将报告内容直接输出到聊天窗口。用户偏好排版清晰的可视化文档。不要将原始得分数据先输出到聊天窗口再生成文件。

**输出位置：** `~/Desktop/hermes产生文件/<岗位名>_简历筛选报告.html`

**模板参考：** 加载 `html-report` skill 获取模板和设计规范，参见其 `templates/report.html`。在该模板基础上填入实际数据生成最终报告。

**输出内容：**
- 岗位基本信息 + JD 摘要（渐变色头部卡片）
- 岗位类型判定 + 使用权重说明
- 硬门槛过滤结果表
- 总分排名表（带颜色编码：绿色推荐/黄色待定/红色不推荐）
- 每个候选人分维度评分明细表（含进度条可视化）
- 沟通建议：按优先级排列，对推荐人选给出理由和行动指引

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
%USERPROFILE%/WorkBuddy/boss-resumes/
├── jd/
│   └── <encryptJobId>.json                  # JD 数据
├── jobs/<岗位名>/
│   ├── candidate_index.json                 # 候选索引
│   └── resumes/
│       ├── <姓名>_<friendId>/
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

### 编码
- BOSS CLI stdout 为 GBK 编码
- 禁止用 PowerShell 管道处理中文 → 乱码
- 看到乱码直接如实报告，不要猜测中文内容

### 模式切换
- 求职者模式：`boss ...`（默认）
- 招聘者（HR）模式：`boss --role recruiter hr ...`
- 不同模式命令不同，模式不对会超时或返回空

### 防封
- 简历下载每次只下一份，脚本自带随机延迟
- 不要连续快速操作同一接口
