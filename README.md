# BOSS 直聘 HR 智能体技能包

> 5 个 AI 智能体 Skill，基于 [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli)，实现 BOSS 直聘简历筛选全流程自动化。

## 🎯 功能概述

```
你有一个管培生岗位 → AI 自动提取 JD → 自动下载候选人简历 → 自动评分排名 → 输出面试建议
```

帮你从 2150 个候选人中，**几分钟内**筛出最匹配的 3-5 人。

---

## 📦 5 个 Skill 一览

> **🚪 boss-hr-auto 是唯一入口**，其余 4 个 Skill 是它的子步骤。使用时请始终先加载 boss-hr-auto。

| # | Skill | 角色 | 作用 |
|:-:|------|:----:|------|
| 0 | **boss-hr-auto** | 🚪 入口 | 编排全流程工作流（唯一入口） |
| 1 | boss-agent-cli | 📖 参考 | BOSS CLI 命令手册（被其他 Skill 引用） |
| 2 | boss-job-detail | Step 1 | 提取岗位 JD |
| 3 | boss-resume-downloader | Step 2 | 下载候选人简历 |
| 4 | resume-screener | Step 3 | 硬门槛过滤 + 加权评分 |

### 工作流

```
用户提供岗位ID
     │
     ▼
┌─────────────────────────────────────────────┐
│           boss-hr-auto（总控入口）            │
│                                              │
│  [Step 1] boss-job-detail       → 提取 JD   │
│  [Step 2] boss-resume-downloader → 下载简历  │
│  [Step 3] resume-screener       → 评分排名  │
│  [Step 4] 汇总报告 + 面试建议                │
│                                              │
│  参考：boss-agent-cli（CLI 命令手册）              │
└─────────────────────────────────────────────┘
     │
     ▼
输出筛选报告
```

---

## 🚀 快速开始

### 前提条件

> **无需打包任何二进制文件。** 以下依赖全部通过包管理器安装，浏览器用你电脑自带或已安装的即可。

| 依赖 | 说明 | 安装方式 |
|------|------|---------|
| Python 3.10+ | 运行脚本 | [python.org](https://python.org) |
| [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) | BOSS 直聘 CLI | `uv tool install boss-agent-cli` |
| patchright | CDP 客户端库（Python 包，**不含浏览器，不下载 Chromium**） | `pip install patchright` |
| Chrome 或 Edge | 任一 Chromium 内核浏览器即可（Windows 自带 Edge） | 已装直接跳过 |

### 1. 安装 boss CLI 工具

```bash
# 方式一：uv（推荐）
uv tool install boss-agent-cli

# 方式二：pipx
pipx install boss-agent-cli

# 验证安装
boss --help
```

### 2. 安装 Python 依赖

```bash
pip install patchright
```

### 3. 启动浏览器 CDP 调试模式

任选一个浏览器，用调试端口启动，后台保持运行即可。

> **不需要 `patchright install chromium`**，这个命令会下载一个 300MB 的 Chromium，
> 但本项目只连接你已有的浏览器，不需要额外下载任何浏览器。

**Windows（推荐用自带 Edge）：**

```powershell
# 关闭所有 Edge 窗口，然后启动 CDP 模式
Start-Process "msedge" -ArgumentList `
  "--remote-debugging-port=9222", `
  "--user-data-dir=$env:USERPROFILE\.workbuddy\chrome-profiles\boss-cdp"
```

**Windows（Chrome）：**

```bat
"C:\Program Files\Google\Chrome\Application\chrome.exe" ^
  --remote-debugging-port=9222 ^
  --user-data-dir="%USERPROFILE%\.workbuddy\chrome-profiles\boss-cdp"
```

**macOS：**

```bash
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.workbuddy/chrome-profiles/boss-cdp"
```

**Linux：**

```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.workbuddy/chrome-profiles/boss-cdp"
```

### 4. 登录 BOSS 直聘

```bash
# 在 Chrome 中登录 zhipin.com 后
boss login

# 或 CDP 扫码登录
boss --role recruiter --cdp-url http://localhost:9222 login --cdp

# 验证
boss --cdp-url http://localhost:9222 --role recruiter me
```

---

## 📖 使用指南

### Skill 调用规则

```
✅ 正确：@skill://boss-hr-auto 岗位链接       ← boss-hr-auto 自动加载子 Skill
❌ 错误：@skill://resume-screener 岗位链接     ← 入口不对，无法完成全流程
❌ 错误：@skill://boss-job-detail 岗位链接     ← 只做了 Step 1，不会继续
```

> **永远从 boss-hr-auto 开始**。4 个子 Skill 的 `description` 中已明确声明自己是子步骤，智能体会优先加载 boss-hr-auto。

### 方式一：在智能体中使用（推荐）

将本项目整个目录复制到你的智能体 skills 目录：

| 智能体平台 | Skills 目录 |
|-----------|------------|
| CodeBuddy | `~/.codebuddy/skills/` |
| Claude Code | `~/.claude/skills/` |
| Cursor | `~/.cursor/skills/` |

然后在对话中直接说：

```
@skill://boss-hr-auto https://www.zhipin.com/web/chat/job/edit?encryptId=你的岗位ID
```

或口语化：

```
帮我筛选这个岗位的简历
```

智能体会自动加载 boss-hr-auto → 按流程执行：提取 JD → 下载简历 → 评分排名 → 输出报告。

### 方式二：命令行手动执行

```bash
# Step 1: 提取 JD
cd boss-job-detail
python scripts/boss_jd.py "管培生"

# Step 2: 同步简历
cd boss-resume-downloader
python scripts/sync_boss_resumes.py sync-job --job-id 524499312 --max 10

# Step 3: 评分（由 AI 智能体执行 resume-screener 的 SKILL.md 方法论）
# Step 4: 汇总（由 AI 智能体执行 boss-hr-auto 的 SKILL.md 编排逻辑）
```

---

## 🧠 评分方法论

### 岗位类型自动判定

- **技术岗**：含编程语言/框架/数据库关键词 → 使用技术权重
- **管培岗**：含管培生/运营/市场/人力等 → 使用管培权重

### 管培岗权重（Mode B）

| 维度 | 权重 | 说明 |
|------|:----:|------|
| 学历 | 30% | 动态分档（C9=100%→二本=62%），专升本+1分 |
| 实习/项目 | 20% | 复杂度、完整度、与企业需求匹配 |
| 专业 | 15% | 对口=100%，相关=80%，无关=30-60% |
| 技能 | 15% | 办公软件、语言、专业证书 |
| 品格 | 10% | 领导力、沟通力、自驱力信号 |
| 竞赛/荣誉 | 10% | 国家级>省级>校级 |

### 输出格式

```
🥇 朱锴瑛  70.4分  ✅ 推荐面试
📌 刘跃平  51.0分  📌 待确认
📌 甄井鑫  49.25分 📌 待定
🚫 高雨琳  淘汰   🚫 24届非26届
🚫 赵哲圻  淘汰   🚫 专业不匹配
```

---

## 🛡️ 安全说明

| 规则 | 说明 |
|------|------|
| 不自动发消息 | CLI 权限不足，请在网页端操作 |
| 内建延迟 | 批量操作间隔 3-6 秒随机延迟 |
| 增量同步 | 已下载简历自动跳过，不重复请求 |
| 数量限制 | 单次 ≤ 10 个打招呼，避免触发风控 |
| Cookie 安全 | 优先浏览器本地提取，不手动传 cookie |

---

## ⚙️ 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BOSS_BIN` | boss CLI 路径（如果 `boss` 不在 PATH 中） | `boss` |
| `CDP_URL` | 浏览器 CDP 调试地址 | `http://localhost:9222` |

---

## 📁 项目结构

```
boss-hr-agent-toolkit/
├── README.md                    # 本文件
├── requirements.txt             # Python 依赖
├── .gitignore
│
├── boss-agent-cli/              # Skill 1：CLI 参考
│   └── SKILL.md                 # Skill 定义
│
├── boss-job-detail/             # Skill 2：JD 提取
│   ├── SKILL.md
│   └── scripts/
│       └── boss_jd.py           # CDP+patchright 提取 JD
│
├── boss-resume-downloader/      # Skill 3：简历下载
│   ├── SKILL.md
│   ├── scripts/
│   │   └── sync_boss_resumes.py # 批量同步简历
│   └── references/
│       └── boss_recruiter_notes.md
│
├── boss-hr-auto/                # Skill 4：全流程编排
│   └── SKILL.md
│
└── resume-screener/             # Skill 5：评分系统
    └── SKILL.md
```

---

## 🔗 相关链接

- [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) — 底层 CLI 工具
- [BOSS 直聘](https://www.zhipin.com) — 招聘平台
- [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) — Playwright 分支（CDP 浏览器控制）

---

## 📄 License

MIT
