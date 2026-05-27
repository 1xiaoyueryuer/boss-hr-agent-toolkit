# BOSS 直聘 HR 智能体技能包

> 6 个 AI 智能体 Skill，基于 [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli)，实现 BOSS 直聘简历筛选全流程自动化。

## 🎯 功能概述

```
你有一个岗位 → AI 自动提取 JD → 自动下载候选人简历 → 自动评分排名 → 输出可视化报告 + 沟通建议
```

**一句话：** AI 帮你从几十个候选人中，几分钟筛出最匹配的那几个，并告诉你该和每个人聊什么。

---

## 📦 Skill 一览

> **🚪 `boss-hr-auto` 是唯一入口**，其余 5 个 Skill 是其子步骤。使用时始终从 boss-hr-auto 开始。

| # | Skill | 角色 | 作用 |
|:-:|------|:----:|------|
| 0 | **boss-hr-auto** | 🚪 入口 | 编排全流程工作流（唯一入口） |
| 1 | boss-agent-cli | 📖 参考 | BOSS CLI 命令手册（被其他 Skill 引用） |
| 2 | boss-job-detail | Step 1 | 提取岗位 JD |
| 3 | boss-resume-downloader | Step 2 | 下载候选人简历 |
| 4 | resume-screener | Step 3 | 硬门槛过滤 + 加权评分 + 学历分档 |
| 5 | html-report | Step 4 | 生成可视化 HTML 报告 + 沟通建议 |

### 工作流

```
用户：「帮我筛选这个岗位」
     │
     ▼
┌───────────────────────────────────────────────┐
│              boss-hr-auto（总控入口）            │
│                                                │
│  [Step 0] 验证 CLI 登录（`boss me` + `hr jobs list`）│
│  [Step 1] boss-job-detail        → 提取 JD     │
│  [Step 2] boss-resume-downloader  → 下载简历    │
│  [Step 3] resume-screener        → 评分排名    │
│  [Step 4] html-report            → 生成报告    │
│                                                │
│  参考：boss-agent-cli（CLI 命令手册）              │
└───────────────────────────────────────────────┘
     │
     ▼
📊 HTML 报告（含排名 + 6维度评分依据 + 行动建议 + 沟通策略）
```

---

## 🚀 快速开始

### 前置依赖

> **无需打包任何二进制文件。** 以下全部通过包管理器安装，浏览器用系统自带的即可。

| 依赖 | 说明 | 安装方式 |
|------|------|---------|
| Python 3.10+ | 运行脚本 | [python.org](https://python.org) |
| [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) | BOSS 直聘 CLI | `uv tool install boss-agent-cli` |
| patchright | CDP 客户端（**不含浏览器，不下载 Chromium**） | `pip install patchright` |
| Chrome / Edge | 系统自带即可 | 已装直接跳过 |

### 1. 安装

```bash
# boss CLI
uv tool install boss-agent-cli

# Python 依赖
pip install patchright
```

### 2. 启动 CDP 浏览器

**Windows（推荐自带 Edge）：**

```powershell
Start-Process "msedge" -ArgumentList `
  "--remote-debugging-port=9222", `
  "--user-data-dir=$env:USERPROFILE\.workbuddy\chrome-profiles\boss-cdp"
```

**macOS / Linux：**

```bash
google-chrome \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.workbuddy/chrome-profiles/boss-cdp"
```

### 3. 登录

```bash
# 浏览器扫码 → 提取到 CLI
boss login --cdp --timeout 30

# 验证（必须两步都通过）
boss --role recruiter me
boss --role recruiter --platform zhipin --cdp-url http://localhost:9222 hr jobs list
```

### 4. 使用

在智能体中：

```
@skill://boss-hr-auto 中台管培生
```

或直接说：

```
帮我筛选中台管培生的简历
```

---

## 🧠 评分系统

### 岗位类型自动判定

- **技术岗**：含编程语言/框架/数据库关键词 → 技术权重
- **管培岗**：含管培生/运营/市场/人力等 → 管培权重

### 管培岗权重（模式 B）

| 维度 | 权重 | 评分方法 |
|------|:----:|---------|
| 学历 | 30% | 动态分档（C9=100% → 二本=62%），硕士+8%，专升本+1分 |
| 实习/项目 | 20% | 复杂度、完整度、与岗位匹配度 |
| 专业 | 15% | 对口=100%，相关=80%，无关=30-60% |
| 技能 | 15% | 办公软件、语言能力、专业证书 |
| 品格 | 10% | 项目组长→领导力，学历进阶→自驱力，奖学金→刻苦 |
| 竞赛/荣誉 | 10% | 国家级>省级>校级 |

### 输出格式

```
📊 排名表（含6维加权分）+ 📋 每人评分依据 + 🎯 行动建议（推荐/待沟通含沟通策略）
```

---

## 🛡️ 安全规则

- ❌ 不自动发消息（CLI 权限不足）
- ✅ 批量操作间隔 3-6 秒随机延迟
- ✅ 增量同步（已下载简历自动跳过）
- ✅ Cookie 优先从浏览器本地提取

---

## ⚙️ 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `BOSS_BIN` | boss CLI 路径 | `boss` |
| `CDP_URL` | 浏览器 CDP 调试地址 | `http://localhost:9222` |

---

## 📁 项目结构

```
boss-hr-agent-toolkit/
├── README.md
├── .gitignore
│
├── boss-agent-cli/              # 📖 CLI 命令参考
│   └── SKILL.md
│
├── boss-job-detail/             # Step 1：JD 提取
│   ├── SKILL.md
│   └── scripts/boss_jd.py       # CDP + patchright 提取 iframe JD
│
├── boss-resume-downloader/      # Step 2：简历下载
│   ├── SKILL.md
│   ├── scripts/sync_boss_resumes.py
│   └── references/
│       ├── boss_recruiter_notes.md
│       └── resume-download-via-friend-list.md  # 推荐/主动沟通候选人回退方案
│
├── boss-hr-auto/                # 🚪 全流程编排（唯一入口）
│   └── SKILL.md
│
├── resume-screener/             # Step 3：评分系统
│   └── SKILL.md
│
└── html-report/                 # Step 4：报告生成
    ├── SKILL.md
    └── templates/report.html    # HTML 模板
```

---

## 🔗 相关链接

- [boss-agent-cli](https://github.com/can4hou6joeng4/boss-agent-cli) — 底层 CLI 工具
- [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright) — Playwright 分支（CDP 浏览器控制）

---

## 📄 License

MIT
