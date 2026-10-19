<p align="center">
  <img src="docs/assets/boss-hr-poster.png" alt="BOSS HR Agent Toolkit：让招聘筛选，更有章法。岗位解析、简历获取、逐份评分与可视化报告。" width="100%">
</p>

<h1 align="center">BOSS HR Agent Toolkit</h1>

<p align="center"><strong>从岗位需求到候选人报告，把招聘筛选变成一条清晰的工作流。</strong></p>

<p align="center">
  <a href="#快速开始">快速开始</a> ·
  <a href="boss-hr-auto/SKILL.md">智能体 Skill</a> ·
  <a href="docs/CLI_WORKFLOW.md">使用指南</a> ·
  <a href="https://github.com/1xiaoyueryuer/boss-hr-agent-toolkit/releases">下载源码</a>
</p>

<p align="center">Windows · Python 3.10+ · Microsoft Edge · MIT License</p>

---

**BOSS HR Agent Toolkit** 是面向 BOSS 直聘招聘端的 AI 自动化工具包。它将岗位解析、候选人简历获取、逐份分析、评分排序和 HTML 报告串成一条流程，并支持在用户明确授权后打招呼。

你提出岗位和筛选需求，智能体负责理解与分析，本地 CLI 执行具体操作。**一个 Skill 配合一套本地命令，让筛选有依据、过程可追踪，关键操作由你掌控。**

## 从一份岗位需求开始

> “帮我为结构工程师筛选 10 份简历，生成报告，告诉我每个人值得聊什么。”

工具包围绕这类任务工作，最终交付可在浏览器中打开的 HTML 报告：候选人排名、各维度分数、亮点与顾虑，以及针对每个人的沟通建议。

```mermaid
flowchart LR
    A[理解岗位与 JD] --> B[你确认筛选条件]
    B --> C[获取候选人简历]
    C --> D[智能体逐份分析]
    D --> E[生成候选人报告]
    E -. 你明确授权 .-> F[可选：打招呼]
```

## 为招聘筛选准备的能力

| 能力 | 帮你完成什么 |
|---|---|
| **读懂岗位** | 从招聘端实时目录解析岗位名称或 ID，提取 JD，为分析提供岗位依据 |
| **获取简历** | 按推荐牛人页面的筛选条件获取列表和在线简历，记录实际下载与失败数量 |
| **逐份分析** | 结合 JD 分析经验、技能、项目与专业；本地代码统一校准学历、计算总分和档位 |
| **交付报告** | 将排名、维度分数、亮点、顾虑与沟通建议整理为 HTML，便于查看和复核 |
| **授权招呼** | 明确授权后定位候选人并打招呼；支持先用 dry-run 只检查定位 |
| **追踪任务** | 每次筛选使用独立 run_id，保存任务状态、评分文件和过程产物 |

**分析有依据。** 逐位处理候选人，将评分理由与沟通建议一起交付，方便回到原始证据核对。

**流程可掌控。** 登录由你完成，推荐筛选条件由你确认，真实招呼需要明确授权。报告生成后，也可以直接结束任务。

**结果留在本地。** CLI 将简历文件、评分产物和报告写入本机目录。智能体分析时会使用你选择的模型服务，请按自己的数据使用要求配置宿主。

## 快速开始

准备 Windows、Python 3.10+、Microsoft Edge，以及一个可以使用的 BOSS 直聘招聘者账号。

### 1. 安装本地工具包

```powershell
git clone --branch stable/v1.1 https://github.com/1xiaoyueryuer/boss-hr-agent-toolkit.git
cd boss-hr-agent-toolkit
python -m pip install -e .
boss-hr --help
```

也可以下载 [Release 源码包](https://github.com/1xiaoyueryuer/boss-hr-agent-toolkit/releases)，解压后运行 `install-windows.bat`。

这是源码工具包，采用 **editable install**。请保留完整源码目录，移动后重新安装；目前不是独立 wheel，也不通过 PyPI 分发。无需额外下载 Chromium，运行时使用本机 Edge。

### 2. 让智能体读取一个 Skill

将 [`boss-hr-auto/SKILL.md`](boss-hr-auto/SKILL.md) 配置到你使用的智能体宿主，或让它在执行任务前读取该文件。宿主需要能够执行本地命令，并读写 CLI 指定的评分文件。

然后给出任务，例如：

> 按 boss-hr-auto 的流程，为“结构工程师”新建任务，获取 10 份简历并生成报告。

模型分析由宿主智能体完成；工具包自身不内置模型 API，无需在本项目中单独配置模型 Key。

### 3. 登录、确认，再查看报告

智能体会从 `boss-hr start "结构工程师"` 开始：

1. **登录**：需要时启动专用 Edge，并提示人工登录招聘端。登录后重试同一条 start。
2. **确认**：在推荐牛人页面调整筛选条件，回复“继续”后开始获取简历。
3. **查看**：智能体逐份完成评分，生成报告并提供文件路径。
4. **可选发送**：只有你明确要求时，才进入打招呼步骤。

正常使用无需预先手动启动 Edge。若 `boss-hr` 不在 PATH，可使用 `python -m boss_hr`。

## 八个命令，一条工作流

| 命令 | 用途 |
|---|---|
| `boss-hr start` | 实时解析岗位，开始一次新的筛选任务 |
| `boss-hr confirm` | 记录用户已确认推荐筛选条件 |
| `boss-hr fetch` | 获取候选人列表与简历 |
| `boss-hr score` | 协调逐份评分，校准并汇总结果 |
| `boss-hr report` | 生成 HTML 报告 |
| `boss-hr greet` | 执行经授权的打招呼，支持 --dry-run |
| `boss-hr status` | 查看明确指定任务的状态 |
| `boss-hr doctor` | 检查本地环境、浏览器与登录态 |

后续命令使用本次 start 返回的岗位信息和 run_id。完整参数、评分文件协议与错误处理见 [CLI 使用指南](docs/CLI_WORKFLOW.md)；智能体应始终按照 [Skill](boss-hr-auto/SKILL.md) 执行。

只检查本地环境、完全不连接浏览器时：

```powershell
boss-hr doctor --skip-browser
```

## 报告如何得出

当前评分维度为学历 25%、经验 25%、技能 25%、项目 15%、专业 10%。智能体根据简历和 JD 给出后四维的分数及理由；学历由内置学校表校准，本地代码计算总分与推荐档位。

总分 ≥70 为推荐、≥60 为待定，其余为不推荐。评分细则见 [Skill 中的评分协议](boss-hr-auto/SKILL.md#评分协议)。报告用于辅助分析，招聘人员应结合原始简历与沟通结果复核。

## 本地输出与配置

```text
boss-hr-output/
└── <encryptJobId>/
    ├── state/                         候选人、下载与评分状态
    └── runs/<run_id>/
        ├── <run_id>_screening_report.html
        ├── run.json
        └── process/                   JD、简历、评分与操作日志
```

默认输出根目录为 `~/Desktop/boss-hr-output`，报告路径以 CLI 返回值为准。详细目录约定见 [文件管理](FILE_MANAGEMENT.md)。

| 环境变量 | 用途 |
|---|---|
| `BOSS_HR_OUTPUT_DIR` | 自定义输出根目录 |
| `BOSS_HR_ENCRYPT_JOB_ID` | 岗位 ID 备用来源；推荐显式传参 |
| `BOSS_HR_JOBS_REGISTRY` | 自定义岗位元数据注册表路径 |

浏览器默认连接本地 CDP 9222，专用配置目录为 `%LOCALAPPDATA%\boss-hr-edge-profile`。

## 适用范围

当前支持一次完整的新筛选任务，不支持 continue、batch、多批累计或同岗位并发 run。实际下载数量受招聘端限制和操作结果影响；页面操作需要有效登录态，也可能受平台页面、接口与限制变化影响。dry-run 仍需要浏览器。

支持与验证范围见 [行为边界](docs/BEHAVIOR_V1.md)。开发者可通过 [维护与验证](docs/ARCHITECTURE.md)了解调用关系和离线测试方式。

---

[使用指南](docs/CLI_WORKFLOW.md) · [更新记录](CHANGELOG.md) · [MIT License](LICENSE)
