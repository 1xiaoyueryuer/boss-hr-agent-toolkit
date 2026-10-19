# BOSS HR Agent Toolkit

**一个 Skill，八个 CLI 命令，把 BOSS 直聘招聘端的简历筛选流程串起来。**

智能体负责理解岗位、逐份分析简历和解释结果；本地 CLI 负责岗位解析、简历获取、评分校准、报告生成和经授权的打招呼操作。安装一个 [`boss-hr-auto`](boss-hr-auto/SKILL.md) skill 即可使用。

当前稳定版：**1.1.4** · 分支：**`stable/v1.1`** · Windows / Python 3.10+ / Microsoft Edge

```mermaid
flowchart LR
    A[岗位名称或 ID] --> B[start：解析岗位与 JD]
    B --> C[用户调整推荐筛选条件]
    C --> D[confirm + fetch：获取简历]
    D --> E[score：智能体逐份评分]
    E --> F[report：HTML 报告]
    F -. 用户明确授权 .-> G[greet：打招呼]
```

## 能做什么

| 环节 | 结果 |
|---|---|
| 岗位解析 | 从招聘端实时目录解析岗位名称、jobId 或 encryptJobId，提取 JD |
| 简历获取 | 按当前推荐页面条件获取候选人列表与在线简历，保留失败记录 |
| 逐份评分 | 智能体分析经验、技能、项目、专业；本地代码校准学历、加权并排序 |
| 筛选报告 | 生成包含排名、维度分数、亮点、顾虑和沟通建议的 HTML |
| 可选招呼 | 用户明确授权后，通过浏览器定位并点击打招呼；支持只定位的 dry-run |
| 任务追踪 | 每次任务使用明确的 run_id，保存状态与过程产物 |

## 安装

这是 **GitHub 源码工具包 + editable install**。请保留完整源码目录；移动后重新安装。目前不是独立 wheel，不提供 PyPI 安装。

```powershell
git clone --branch stable/v1.1 https://github.com/1xiaoyueryuer/boss-hr-agent-toolkit.git
cd boss-hr-agent-toolkit
python -m pip install -e .
boss-hr --help
```

也可运行 `install-windows.bat`。无需下载额外 Chromium；运行时使用本机 Edge。若命令未加入 PATH，可用 `python -m boss_hr` 替代 `boss-hr`。

### 接入智能体

将 [`boss-hr-auto/SKILL.md`](boss-hr-auto/SKILL.md) 配置为智能体的 skill，或让智能体先读取这份文件，并确保它能够执行本地命令、读写 CLI 指定的评分文件。不同宿主的 skill 安装方式不同；本项目不绑定某一种智能体平台。

例如告诉智能体：

> 按 boss-hr-auto 的流程，为“结构工程师”新建筛选任务，获取 10 份简历并生成报告。

`score` 是与智能体配合的文件协议；CLI 自身不调用模型 API，也不需要在本项目里配置模型 Key。

## 一次筛选怎么运行

下面的命令按阶段执行，**不要一次性粘贴整段运行**。

```powershell
# 1. 开始新任务，岗位也可使用 jobId / encryptJobId
boss-hr start "结构工程师"
```

首次使用时，`start` 会尝试启动专用 Edge，并打开招聘端登录页。若返回 `waiting_user_login`，先在该窗口人工登录，再重试同一条 `start`。默认不阻塞等待扫码，此时不会创建 run。

返回 `waiting_user_confirmation` 后，先在推荐牛人页面调整筛选条件，再告诉智能体“继续”。后续命令始终使用本次返回的 `job_name`、`encrypt_job_id` 和 `run_id`：

```powershell
# 2. 用户确认筛选条件后
boss-hr confirm --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<本次run_id>"
boss-hr fetch --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<本次run_id>" --count 10

# 3. 每次 score 返回一位待评候选人；智能体读 input_file，写 output_file，再调相同命令
boss-hr score --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<本次run_id>"

# 4. score 返回 scoring_complete 后生成报告
boss-hr report --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<本次run_id>"

# 随时查看指定任务
boss-hr status --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<本次run_id>"
```

报告路径由 `data.report_file` 返回。报告生成后流程可以结束；只有用户明确要求打招呼时才执行：

```powershell
boss-hr greet --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<本次run_id>" --dry-run
# 核对结果、获得发送授权后，去掉 --dry-run 执行真实操作
```

诊断环境可用 `boss-hr doctor`；只检查本地 Python 和依赖、完全不连接浏览器时，使用 `boss-hr doctor --skip-browser`。正常使用直接从 `start` 开始。

参数、返回状态和错误处理见 [CLI 工作流](docs/CLI_WORKFLOW.md)。

## 评分与边界

当前权重为学历 25%、经验 25%、技能 25%、项目 15%、专业 10%。智能体仅评后四维的 0–100 最终分，学历由内置学校表覆盖；表外或缺失学校按 60 分计并记录原因。当前代码没有硕博额外加分。总分 ≥70 为推荐、≥60 为待定，其余为不推荐。

评分协议和锚点集中在[唯一 skill](boss-hr-auto/SKILL.md#评分协议)。评分结果仍需招聘人员结合原始证据复核。

- 一次 `start` 创建一次新任务；不支持 `continue`、`batch`、多批累计或同岗位并发 run。
- 获取数量是目标值，实际下载可能因平台限制或失败而减少。
- 浏览器操作依赖可用的招聘者账号、当前页面和登录态；节流与真实浏览器不保证避开平台限制。
- `greet` 是真实发送操作；`--dry-run` 仍需浏览器，但不会点击发送或修改 run 状态。
- **1.1.4 仅做离线代码与文档验证，未登录招聘端，也未完成真实下载或招呼复测。**

详细契约见 [行为边界](docs/BEHAVIOR_V1.md)。

## 项目结构

```text
boss-hr-auto/SKILL.md          唯一智能体入口：流程 + 评分协议
boss_hr/                      统一 CLI、命令服务、适配器与返回契约
shared/                       子进程执行、CDP、路径、run 与候选人状态
boss-job-detail/scripts/      内部 JD 实现
boss-recommend-downloader/scripts/  内部列表与简历获取实现
resume-screener/scripts/      内部评分准备、收集、校准与学校表
html-report/scripts/         内部 HTML 报告实现
boss-hr-greet/scripts/        内部招呼实现
tests/                        离线回归测试
docs/                         使用与维护文档
```

保留的业务脚本仍由 CLI 在本地调用；用户与智能体统一使用 `boss-hr`。目录不再代表独立 skill。调用关系和本次删除依据见 [维护与验证](docs/ARCHITECTURE.md)。

## 数据与配置

默认输出为 `~/Desktop/boss-hr-output/<encryptJobId>/runs/<run_id>/`，HTML 放在 run 根目录，过程 JSON 放在 `process/`，跨任务候选人状态放在岗位的 `state/`。参见[文件管理](FILE_MANAGEMENT.md)。

| 环境变量 | 用途 |
|---|---|
| `BOSS_HR_OUTPUT_DIR` | 输出根目录，默认 `~/Desktop/boss-hr-output` |
| `BOSS_HR_ENCRYPT_JOB_ID` | 岗位 ID 备用来源；推荐显式传 CLI 参数 |
| `BOSS_HR_JOBS_REGISTRY` | 可选岗位元数据注册表路径，默认输出根目录下 `jobs.json` |

浏览器默认使用本地 CDP 9222 和 `%LOCALAPPDATA%\boss-hr-edge-profile` 专用配置目录。简历、评分、Cookie 与浏览器配置属于本地运行数据，不要提交到仓库。

开发验证：`python -m pip install -e ".[dev]"`，再运行 `python -m pytest -q --basetemp .pytest-tmp`。

[更新记录](CHANGELOG.md) · [MIT License](LICENSE)
