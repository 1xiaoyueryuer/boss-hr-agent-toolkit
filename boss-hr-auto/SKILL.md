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

## 🔧 辅助脚本 & 参考

| 文件 | 位置 | 用途 |
|:----|:----|:----|
| `browser-fallback-when-cli-down.md` | `references/browser-fallback-when-cli-down.md` | CLI 假阳性时用 CDP 浏览器直接获取 JD+候选人的代码示例 |

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

### 0.5 环境变量配置（安装后必做）

安装完 boss-agent-cli 后，需要正确配置环境变量才能正常工作。

#### 一键启动（推荐）

```bash
# 在项目目录下执行，每次打开新终端先跑这个
source scripts/setup_env.sh
```

该脚本会自动设置 `PYTHONHOME=""` 和 `PATH`，并验证 boss CLI 是否可用。

#### 永久生效（一次配置，以后不用再跑）

把以下两行加到 `~/.bashrc` 末尾，以后每次打开终端自动生效：

```bash
echo 'export PYTHONHOME=""
export PATH="$PATH:$HOME/.local/bin"' >> ~/.bashrc
source ~/.bashrc
```

> ⚠️ 注意：`PYTHONHOME` 可能影响其他 Python 应用。如果遇到 Python 相关报错，注释掉 `~/.bashrc` 中的 `PYTHONHOME` 行，改为每次手动执行 `source scripts/setup_env.sh`。

#### 环境变量说明

| 变量 | 说明 | 默认值 | 何时需要修改 |
|------|------|--------|------------|
| `PYTHONHOME` | **必须清空**。Windows git-bash 下此变量若指向已删除的 Python，CLI 报 `Fatal Python error` | `""` | 每次运行 boss 命令前 |
| `PATH` | boss.exe 在 `~/.local/bin/`，但 git-bash 默认不在 PATH 中 | 追加 `$HOME/.local/bin` | 提示"command not found"时 |
| `CDP_URL` | 浏览器 CDP 调试地址 | `http://localhost:9222` | 9222 端口被占用时 |
| `BOSS_BIN` | boss CLI 完整路径 | `boss`（自动查找 PATH） | boss 不在 PATH 且无法添加时 |

---

**每次跑流程时，智能体自动完成以下步骤：**

### 0. 环境修复 + 验证 CLI 登录（每次执行前必做）

⚠️ **`boss status` 是假阳性检测器，不可信。必须用 `boss me` + `hr jobs list` 双重验证。**

```bash
# 步骤 A：初步检查
export PYTHONHOME=""
export PATH="$PATH:$HOME/.local/bin"
boss.exe status

# 步骤 B：验证是否真登录——必须有真实姓名
boss.exe --role recruiter me

# 步骤 C：验证是否能访问岗位数据（最重要）
boss.exe --role recruiter --platform zhipin --cdp-url http://localhost:9222 hr jobs list
```

**判定标准：**

| `hr jobs list` 结果 | `boss me` 结果 | 含义 | 操作 |
|:-------------------|:--------------|:----|:----|
| `data: [{...}]`（有数据） | `name: "真实姓名"` | ✅ 真登录 | 继续执行 |
| `data: {}` 或 `data: []`（空） | `name: ""`（空） | ❌ 假阳性 | **立即修复 → `boss login --cdp --timeout 30`** |
| 返回错误 / 命令失败 | — | ❌ 未登录 | 打开登录页让用户扫码，然后 `boss login --cdp` |

**假阳性修复命令（唯一正确方式）：**
```bash
boss login --cdp --timeout 30
```
修复后必须重新执行步骤 B 和 C 确认登录成功，才能继续。

> **绝对禁止的行为：** CLI 登录验证失败后，不要尝试用浏览器 API 拦截、CDP 直接操作、或任何其他替代方法来绕过 CLI。必须先把 CLI 修好。

### 1. 启动 CDP 浏览器

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

#### 🔧 CLI 不可用时的回退：CDP 浏览器直接提取

如果 `boss hr jobs list` 返回空数据（CLI 假阳性），`boss_jd.py` 脚本无法匹配岗位。此时**直接通过 CDP 浏览器导航到岗位编辑页**：

```python
from patchright.sync_api import sync_playwright
import json, time

# 用已知的 encryptJobId（可从聊天页候选人关联的岗位名获取）
encrypt_job_id = "4c297ca36277e0400nB-29m9EFpZ"  # 示例
target = f"https://www.zhipin.com/web/chat/job/edit?encryptId={encrypt_job_id}&jobCreateSource=0&enterSource=6"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = browser.contexts[0].pages[0]  # 复用已有页面，不 new_page()
    pg.goto(target, wait_until="networkidle", timeout=30000)
    time.sleep(3)

    iframe = pg.query_selector("iframe")
    frame = iframe.content_frame()
    body_text = frame.evaluate("document.body.innerText")

    # 提取表单完整值（含职位描述）
    form_vals = frame.evaluate("""() => {
        const r = [];
        document.querySelectorAll('input:not([type=hidden]), textarea, [contenteditable]').forEach(el => {
            const v = el.value || el.innerText || '';
            if (v && v.length > 2 && v !== '保存') r.push(v);
        });
        return r;
    }""")
```

**如果不知道 encryptJobId：** 从聊天页左侧列表中找到该岗位的候选人，点击后页面 URL 或 intercepted API 响应中会包含 `encryptJobId`。

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

#### 🔧 CLI 不可用时的回退：浏览器 API 响应拦截

当 `boss hr jobs list` 或 `boss hr applications` 返回空数据时，**用 CDP 浏览器的网络响应拦截**获取候选人和简历数据：

1. **获取候选列表 + securityId：** 拦截 `getBossFriendListV2` API 响应
2. **获取聊天消息：** 读取页面 text（所见即所得）
3. **注意：** 此方法只能拿到基本信息和聊天自述，无法获取完整简历 JSON

```python
from patchright.sync_api import sync_playwright
import json, time

captured = {}
def on_response(resp):
    url = resp.url
    try:
        if 'getBossFriendListV2' in url:
            captured['friend_list'] = resp.json()
    except:
        pass

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    pg = browser.contexts[0].pages[0]  # 复用 pages[0]，不 new_page()

    pg.on("response", on_response)
    pg.goto("https://www.zhipin.com/web/chat/index", wait_until="networkidle", timeout=30000)
    time.sleep(5)

    # 从响应中提取 candidate 数据
    fl_data = captured.get("friend_list", {}).get("zpData", {})
    friends = fl_data.get("friendList", [])  # 列表项含: name, securityId, encryptUid, uid, jobName, encryptJobId
    # 提取候选人信息
    for f in friends:
        name = f.get("name")
        sec_id = f.get("securityId", "")       # 简历下载所需
        encrypt_uid = f.get("encryptUid", "")  # 同 encryptGeekId
        uid = f.get("uid", 0)                  # 同 friendId

    # 从页面 text 提取聊天消息（每个候选人最后一条消息）
    page_text = pg.evaluate("document.body.innerText")
```

候选人的加密参数对应关系：
| 参数 | friendList 中的字段 | 用途 |
|:----|:------------------|:-----|
| encryptGeekId | `encryptUid` | `boss hr resume` 的参数 |
| securityId | `securityId` | `boss hr resume` 的参数 |
| encryptJobId | `encryptJobId` | `boss hr resume` 的参数 |
| friendId | `uid` | 聊天 API 参数 |

> ⚠️ 即便拿到这些参数，如果 CLI session 已过期，`boss hr resume` 也会返回空简历数据。此时评分只能基于聊天消息中的自述信息。

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

### CLI 假阳性检测（重要）
`boss status` 返回 `logged_in: true` 但实际 token 可能已过期。**确认方法：**
```bash
# ✅ 真阳性 — 能返回在线岗位数据
boss --role recruiter --platform zhipin --cdp-url http://localhost:9222 hr jobs list

# ❌ 假阳性 — 返回 data: {}（空对象），token 已过期
```
关键判断：如果 `hr jobs list` 返回 `"data": {}`（空 JSON 对象）而非岗位数组，说明 CLI session 已过期。**不要继续依赖 CLI**，立即切换到 CDP 浏览器直接操作。

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
