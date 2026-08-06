# Changelog

## v1.2.0 — 评分 v2 子包 + 持久化 LLM 配置 + 动态下载超时（2026-08-06）

首个整合"评分 v2"独立子包的稳定版本。业务流程兼容 v1，旧 screening / report / greet 数据只读兼容；本轮次不修改任何 v1 旧 run 的兼容性分支。

### 新增

- 独立、无状态、OpenAI-compatible 的 LLM 评分 worker：每次 `boss-hr score` 评一位候选人，每位候选人是全新的 `messages` 请求，候选人之间不共享聊天上下文。
- JD scorecard 生成、hash 校验与复用：`source_jd_hash` + `policy_hash` 一致时直接复用 scorecard，不重复调用 LLM。
- evidence mapping 与 Python 确定性算分：LLM 只做 requirement ↔ 简历证据映射，分数与 best_track 由 Python 端用 status_value × importance_weight 算分。
- A/B/C 决策及理由：必须给出 `decision_label` 与 `decision_reasons`；C 不得生成验证问题或软化建议。
- `~/.boss-hr/llm.env` 持久化 LLM 配置：标准库解析器（无 `dotenv` 依赖），仅接受 5 个白名单字段，整份失败，绝不部分生效；环境变量仍可逐字段覆盖文件。
- HTML v2 A/B/C 报告：自动按 `schema_version=2.0` 切换渲染器，按决策分组展示分数、亮点、顾虑、判定理由、面试确认问题。
- greet v2 资格：仅 `decision in (A, B) AND contact_recommended=true` 入选；C 永远排除；`--threshold` 不参与资格判断；`--only-names` 严格与本 run `screening_results.json` 取交集。
- 下载 subprocess 动态 timeout：list 子进程保持 600 秒；download 子进程按 `max(1800, 300 + count*60)` 线性放大，应对 BOSS 节流。

### 改进

- evaluator 提示词校准：明确禁止添加 JD 未写明的面试级证明标准（如复杂 SQL 优化、Nginx / CI/CD / 监控细节、生产规模量化、职位/职责字段等）；"仅会 / 不掌握"等简历自述被视为有效反证。
- 学历作为 15% 软偏好，但不能挽救岗位硬性能力不匹配（`must=not_met` 永远判 C，即使清华 / 北大硕士也不例外）。
- v2 `--only-names`、`--threshold`、C 排除边界由 `boss-hr-greet` 强制实施，与 v1 pool fallback 分支隔离。
- LLM 重试、错误脱敏、静态测试 hook 白名单：`401/403` / 4xx 非限流 / 配置错误不重试；异常信息不暴露 API key / Authorization；`BOSS_HR_LLM_TEST_HOOK` 仅允许仓库内 `tests.cli._mock_llm_hook`。
- v1 旧 screening / report / greet 数据只读兼容，CLI 外部签名不变。

### 验证

- 593 项自动测试通过：`tests/test_llm_client.py`、`tests/test_evaluator_status_rules.py`、`tests/test_scoring_*`、`tests/cli/test_score_*`、`tests/cli/test_fetch.py`、`tests/test_greet_v2.py`、`tests/test_html_report_v2.py` 等。
- `python -m compileall` 与 `git diff --check` 均通过。
- 真实 MiniMax 隔离 smoke：在 OS 临时目录用合成 Python 后端 JD + A/B/C 三名虚构候选人，验证新提示词实际产出 A / B / C 三类决策、scorecard 复用、finalize、HTML v2 报告、第二次 score 不触发新 LLM 调用的幂等性。

### 已知限制

- 本发布周期没有执行真实 BOSS fetch 与真实 greet 点击；评分 v2 已通过真实 MiniMax smoke 验证，浏览器/下载/招呼仍依赖下游 mock 与单元测试。
- 当前不承诺独立 PyPI wheel 脱离源码仓库运行；用户应在完整仓库目录内运行 `pip install -e .` 或 `install-windows.bat`，`fetch` / `report` / `greet` 等运行时仍依赖根目录脚本。
- LLM evidence mapping 仍可能存在小范围语义波动（如"仅会基础"是否判 partial 还是 unverified 取决于具体模型）。
- 旧 v1 数据不自动迁移至 v2，只读兼容；CLI 命令外部签名保持稳定。

## v1.1.3 — greet 定位算法收口 + dry-run 严格化（2026-08-05）

修复 v1.1.2 真实交互式 smoke 暴露的 greet not_found bug，并强化
`--dry-run` 的零副作用契约。

### 修复（来自真实 v1.1.2 smoke）

v1.1.2 smoke 真实 run `9a7759badfd95d350nFz3d-_F1NX` / `2026-08-04_164328`
出现 `targeted=4 greeted=3 not_found=1`：评分 rank 1（张庆祝）被标记为
not_found 但另外 3 位在同浏览器会话中真实点击发送成功。根因 + 修复：

1. **身份标识**：原 `scan_and_record_positions` 只按候选人姓名匹配，
   `screening_results.json` 不含 encryptGeekId。修复：
   - `load_high_score_candidates` 从 `recommend_geek_ids.json`
     （优先）/ `new_resumes.json` / `candidate_pool.json` 反查
     `encrypt_geek_id` 并填入每个目标。
   - DOM 卡片扫描 `_CARD_SCAN_JS` / `_FIND_CARD_JS` / `_FIND_BTN_JS`
     按 `data-geek` / `data-geekid`（实测真实 BOSS DOM）→
     `data-geek-id` / `data-uid` / href 查询参数的优先级收集。
2. **虚拟列表懒加载**：原一次性 `scrollHeight → scrollY=0` 扫描无法
   加载 BOSS 全部候选人卡片。修复：渐进式滚动 `scan_all_cards_progressively`
   每屏 sleep + 重扫 + `scrollHeight` 停止增长才算完成。
3. **未找到时的恢复路径**：原算法找到 `pos` 才招呼，找不到直接跳过。
   修复：`greet_one_by_id` 实时按 encrypt_geek_id 找 → 找不到则渐进
   滚动 ≤6 次 + `reached_bottom` 终止 → 仍无则 `status='not_found_after_full_scan'`。
   **不刷新页面**（删除原 `_refresh_page_once` 调用方；
   函数本身已彻底移除以防误用）。
4. **同名误发保护**：若 DOM 找到的卡片 encrypt_geek_id 与目标不一致，
   一律 `not_found_after_full_scan` + reason 含 `geekId mismatch`，
   绝不 click。
5. **partial_success 不被掩盖**：`_calc_summary` 区分顶层 status
   （complete / partial_success / all_not_found / no_candidates），
   `greet_service` 把 partial_success 映射为
   `status='partial_success'` + `next_action='review_warnings'` +
   `data.partial_success_warnings=True`，**不** 自动 finish run。

### 新增 / 强化

- **`boss-hr greet ... --dry-run` 严格化**：dry-run 路径下
  `greet_one_by_id` 找到卡片 + 按钮后立即返回 `status='dry_run'`；
  不调用 click、不调 `human_move`、不调 `mark_done('greet')`、
  不调 `auto_finish()`、不修改 `run.json`、不修改候选人 BOSS 状态。
  新 dry-run 顶层 status：
  - `no_candidates`    目标列表为空
  - `dry_run_complete` 全部目标按 encrypt_geek_id 定位到（仅定位，不 click）
  - `dry_run_review`   部分或全部 not_found_after_full_scan（需人工核对）
- **每条结果必填日志字段**：`target_encrypt_geek_id`、`match_by`
  （encrypt_geek_id / name / no_gid_in_dom / none）、
  `scroll_attempts`、`cards_scanned`、`unique_ids_seen`、
  `reached_bottom`、`reason`（脱敏）。
- **`_refresh_page_once` 已删除**：原"必要时刷新一次"恢复路径移除。
  生产代码默认禁止任何刷新 BOSS 推荐页的路径。

### 测试基线

**363 passed, pytest exit code 0**。

`tests/test_v113_greet_not_found_fix.py`：33 个测试
- `_calc_summary` 完整 + partial_success + all_not_found + no_candidates
- `_calc_summary` dry_run_complete + dry_run_review + no_candidates
- 非 dry-run 路径状态语义保持不变
- `_find_card_by_id` 按 encryptGeekId 锁定 / fallback name / not_found
- `_find_btn_by_card_id` 按 ID + dy 容差拒绝
- `greet_one_by_id` 同名不同 geekId 拒绝点击
- `greet_one_by_id` 完整扫描未找到 → not_found_after_full_scan
- `greet_one_by_id` 找到 → greeted
- `scan_all_cards_progressively` 滚动后加载新卡
- `_build_geek_id_index` 从 recommend_geek_ids + 兜底 new_resumes
- `greet_service` CLI 状态映射（4 路径 + 3 dry-run 路径）
- **端到端 4 目标 / 3 greeted / 1 not_found → partial_success**
- dry-run 零点击 / run.json byte-identical / `_calc_summary` 兼容新旧 status

### 已验证（真实 Windows，2026-08-04）

- ✅ `boss-hr start` 真实路径走通；`screening_results.json` 含 4 位
  ≥70 推荐候选人（张庆祝 82.2 / 樊晓林 76.8 / 赵宇奔 76.0 / 盼盼 75.8）。
- ✅ `boss-hr confirm / fetch / score / report` 完整业务流（10 张简历）。
- ✅ `boss-hr greet` 真实点击：**3 位候选人成功发送招呼**
  （盼盼 / 赵宇奔 / 樊晓林，按 BOSS 倒序处理）；张庆祝 not_found。
- ✅ `run.json.finished=true`、`finished_at='2026-08-04 16:59:53'`、
  `steps_done=['jd','download','score','report','greet']`。
- ✅ 真实 `boss-hr greet ... --dry-run` 验证：
  - 9222 持续 True（同一 Edge 实例）；
  - BOSS recommend 页 URL 不变（**未刷新页面**）；
  - `run.json` md5 在 dry-run 前后完全相同（**零 run 修改**）；
  - 4 目标 → `greeted=0`、`not_found=4`，无任何 click。

### 未验证（明确记录，避免误用）

- **本次 dry-run 中 4 位目标在 BOSS 当前推荐列表均找不到**。
  DOM 当时 122 张可招呼卡片的 `data-geek` 与目标 4 位的
  encryptGeekId（前 14 字符 `78180a558b99990`、`610cef328fe58e3`、
  `cc17c4b3a020e2`、`a71956ba6d9e54`）**无交集**。这是 BOSS 后端推荐
  列表随时间替换的事实，不是 v1.1.3 算法缺陷。
- **v1.1.3 新定位算法未对当前存在目标完成新的真实点击验证**
  （v1.1.2 真实 run 已 finished=true；当前 run 目标已不在 BOSS 推荐页）。
  v1.1.2 smoke 中 `greeted=3` 是用 v1.1.1 旧算法（按 doc_y + name 匹配）
  跑出的，不算 v1.1.3 新算法的真实点击证据。
- 真实 `greet` 端到端（v1.1.3 新算法下 ≥70 候选人真实点击发送）
  需要：新建 run（fetch 新简历）→ 全新目标在 BOSS 当前推荐列表 →
  实际扫码确认 → 跑 greet。如需验证，请在一个新的真实 run 上做。

### 当前限制（GitHub Preview）

- 不支持 `continue` / `batch` / `--batch-size` / 多批累计
- 真实 `greet` 端到端（含 ≥70 候选人真实点击发送）尚未在 v1.1.3
  新算法下完成受控验证
- 仅在 Windows + Git Bash 上完整测试过；macOS / Linux 需自测
- 当前发布形态：**GitHub 源码工具包 + editable install**，**不是**独立 wheel；
  移动源码后必须重新 `pip install -e .`
- 不发布到 PyPI；不在 wheel 中包含完整业务脚本

### 安装

```bash
git clone https://github.com/<owner>/boss-hr-agent-toolkit
cd boss-hr-agent-toolkit
python -m pip install -e .
boss-hr --help           # 验证 8 个公开命令（含 doctor）
```

或 Windows：

```bat
install-windows.bat
```

---

## v1.1.2 — 自动启动专用 Edge + waiting_user_login（CLI Preview，2026-08-04）

在 v1.1.0 / v1.1.1 之上，本版引入"无需先跑 doctor"的首次用户体验：
start / fetch / greet 共用 `boss_hr/adapters/browser_environment.ensure_browser_ready`，
9222 未监听时自动启动专用 Edge 并打开 BOSS 招聘者登录页；未登录则优雅返回
`status=waiting_user_login` 而非错误。

### 新增

- `boss_hr/adapters/browser_environment.py`
  - `ensure_browser_ready(auto_launch, login_wait_seconds)`：start/fetch/greet 共用
  - `launch_dedicated_edge()`：专用 Edge（`--user-data-dir=%LOCALAPPDATA%\boss-hr-edge-profile`
    + `--remote-debugging-port=9222`，**不污染**用户日常 Edge profile）
  - `_open_login_page()`：复用 `shared.cdp_preflight.connect_cdp` 的 session.page，
    导航到 BOSS 招聘者登录入口并 `bring_to_front`
- `boss-hr start --no-auto-launch`（调试/测试用；缺 CDP 时直接 `CDP_NOT_RUNNING`）
- `boss-hr start --login-wait-seconds N`（默认 20）
- 新 status：`waiting_user_login`（`ok=true`，`next_action=retry_same_command`，
  **不创建 run**）

### 修复（v1.1.2 fix 真实 smoke 暴露）

- `_open_login_page` 不再静默吞异常：返回 `(ok: bool, reason: str)`，reason 是
  异常类型名 + 短前缀，不含 Cookie / token / URL-secret
- 修复 `patchright` Sync API 在 asyncio loop 中冲突的 bug：
  `_open_login_page` 不再启动第二个 `sync_playwright()` 实例
- `ensure_browser_ready` 超时分支的 `message` 与 `remediation` 严格按
  `login_page_opened` 分支，**绝不伪称已打开登录页**
- `start_service` `waiting_user_login.message` 同步分支：
  - `login_page_opened=true` → "已为你打开专用 Edge，请在浏览器中扫码登录
    BOSS 招聘者后台。完成后回复"好了"，我会继续当前任务。"
  - `login_page_opened=false` → "已为你启动专用 Edge，但未能自动打开 BOSS
    招聘者登录页（<脱敏原因>）。请在已打开的专用 Edge 窗口中手动打开
    https://www.zhipin.com/web/chat/recommend 登录。完成后回复"好了"，
    我会继续当前任务。"

### 变更

- `boss-hr-auto/SKILL.md`：移除"首次必须先跑 doctor"的硬约束；新增
  `waiting_user_login` 智能体处理流程（直接告诉用户扫码登录并立即停止；
  用户回复"好了"后重试同一条 start，不传任何新参数）
- `README.md`：快速开始前置条件放宽（不再要求预先手动启动 Edge）
- `docs/CLI_WORKFLOW.md`：命令表扩为 8 个（含 doctor），每个标注浏览器依赖；
  §2 流程图加入自动启动 / `waiting_user_login` 分支；新增"waiting_user_login
  重试"子节
- `tests/test_v112_auto_browser.py`：25 个测试（v1.1.2 实现 18 个 +
  v1.1.2 fix 7 个回归保护）

### 测试基线

**336 passed, pytest exit code 0**。

### 已验证（真实 Windows 环境，2026-08-04）

- ✅ 9222 未监听 → start 自动启动专用 Edge（专用 profile + `--remote-debugging-port=9222`）
- ✅ `_open_login_page` 真实把专用 Edge 导航到 `https://www.zhipin.com/web/user/?ka=bticket`
  （BOSS 招聘者登录入口），并 `bring_to_front` 聚焦
- ✅ 未登录 → start 返回 `status=waiting_user_login`、`data.login_page_opened=true`、
  `data.login_page_open_error=""`、`run_id=null`（**不创建 run**）
- ✅ message 明确告诉用户扫码登录（与 SKILL.md 强制文案一致）
- ✅ 重试同一条命令（不传任何新参数）→ 行为幂等（再次 `waiting_user_login`，
  复用既有 Edge，再次成功导航到 zhipin.com）
- ✅ 不读取 `~/Desktop/boss-hr-output/jobs.json`（mtime 未变）
- ✅ confirm / score / report / status / greet 未触发
- ✅ `tests/test_v112_auto_browser.py` 25 个测试（含 7 个回归保护）全部通过
- ✅ 完整 pytest 336 passed

### 未验证（需真人 BOSS 招聘者扫码）

- ⏸ `status=waiting_user_confirmation` 的真实路径：本会话环境无 BOSS 招聘者
  账号扫码能力，`zp_at` / `wt2` / `bst` 三个关键 Cookie 全部缺失。第二次 start
  无法走通"实时解析岗位 → 创建 run → waiting_user_confirmation"完整路径
- ⏸ `boss-hr fetch --count N` 真实拉取 + 下载简历的端到端流程
- ⏸ `boss-hr greet` 真实点击打招呼（v1.1.0 起就标注"未受控验证"，本版不引入
  新风险）

如需在生产环境验证上述未验证路径，请在已扫码登录 BOSS 招聘者后台的
Windows + 9222 已就绪的环境执行：

```bash
boss-hr start "<encryptJobId|jobId|岗位名>"
# 第二次执行同一条命令后应返回 waiting_user_confirmation + 新 run_id
boss-hr status --job-name "<>" --encrypt-job-id "<>" --run-id "<rid>"
# 应返回 confirmed=false，process/job_detail.json 已存在
```

### 当前限制（GitHub Preview）

- 不支持 `continue` / `batch` / `--batch-size` / 多批累计
- 真实 `greet` 成功路径（≥70 分候选人浏览器实际点击发送）**尚未完成受控验证**
- 仅在 Windows + Git Bash 上完整测试过；macOS / Linux 需自测
- 当前发布形态：**GitHub 源码工具包 + editable install**，**不是**独立 wheel；
  移动源码后必须重新 `pip install -e .`
- 不发布到 PyPI；不在 wheel 中包含完整业务脚本

### 安装

```bash
git clone https://github.com/<owner>/boss-hr-agent-toolkit
cd boss-hr-agent-toolkit
python -m pip install -e .
boss-hr --help           # 验证 8 个公开命令（含 doctor）
```

或 Windows：

```bat
install-windows.bat
```

---

## v1.1.0 — 统一 CLI Preview（2026-08-04）

GitHub 首版发布状态：**v1.1.0 / CLI Preview**。

### 新增

- **统一 CLI `boss-hr`**（子包 `boss_hr/`），7 个公开命令：
  - `boss-hr start` — 创建新 run，停在人工确认门
  - `boss-hr confirm` — `confirmed` 翻 true；不入 `steps_done`
  - `boss-hr fetch --count N` — 拉候选人列表 + 下载 N 份简历
  - `boss-hr score` — 评分协调（一次返回 1 位候选人）
  - `boss-hr report` — 生成 HTML 报告
  - `boss-hr greet` — ≥70 分候选人打招呼（需用户明确批准）
  - `boss-hr status` — 读 `runs/<run_id>/run.json` + process 目录
- 统一 JSON 输出 schema；退出码语义保留旧脚本契约
- 人工确认门：start 后必须停下，等用户回复"继续"
- 单候选人评分循环：`score` 命令必须 LLM 多次调用，每次一位
- `boss_hr/__main__.py`：让 `python -m boss_hr` 工作
- `pyproject.toml`：唯一 console_script `boss-hr = "boss_hr.cli:main"`
- Windows 脚本：`install-windows.bat` / `uninstall-windows.bat`
- 文档：
  - `docs/CLI_WORKFLOW.md`（GitHub 用户/开发者向）
  - `docs/refactor/unified-cli/HANDOFF.md`（已迁移完成）
  - `docs/refactor/unified-cli/real-smoke-2026-08-04.md`（真实 smoke 脱敏记录）
  - `docs/debug/boss_jd-cache-audit-2026-08-04.md`（JD formValues 调查）

### 修复（auto_greet.py 旧业务缺陷）

- `edc6959 fix(greet): prevent deletion of existing run data`
  - 之前 atexit 调 `prune_if_empty()` 会 `rmtree` 整个 run 目录
  - 现在改为 `note_skip_if_unsaved()` 只写日志 + sentry，永不删文件
  - 23 例回归测试覆盖（11 prune + 12 maybe_finish）
- `ad243ad fix(greet): finish the explicit run after successful greeting`
  - 之前 `orch.finish()` 缺 `run_id` 实参 + `except Exception` 吞错
  - 现在用 `maybe_finish()` 显式传 `run_id`，异常不静默

### 变更

- `boss-hr-auto/SKILL.md` 重写为唯一面向智能体的工作流入口
  （350 行 → 187 行），不再指导调用旧业务脚本
- 其他子 SKILL.md 标记为"业务实现参考"，不再暗示是工作流入口
- `README.md` 头部增加"统一 CLI + editable install"产品定位

### 安装

```bash
git clone https://github.com/<owner>/boss-hr-agent-toolkit
cd boss-hr-agent-toolkit
python -m pip install -e .
boss-hr --help           # 验证 7 个公开命令
```

或 Windows：

```bat
install-windows.bat
```

### 测试基线

发布时：**272 passed, pytest exit code 0**。

包括：
- 业务测试（`tests/test_*`）：CLI 子层、run_id 边界、cli_runner、school_tier、job_registry
- 统一 CLI 测试（`tests/cli/test_*.py`）：每条命令都有专项覆盖
- 安装入口测试（`tests/test_install_entry.py`）：12 例

### 当前限制（GitHub 首版）

**必须如实记录**：

- 不支持 `continue` / `batch` / `--batch-size` / 多批累计
- 真实 `greet` 成功路径（≥70 分候选人浏览器实际点击发送）**尚未完成受控验证**
- 仅在 Windows + Git Bash 上完整测试过；macOS / Linux 需自测
- 当前发布形态：**GitHub 源码工具包 + editable install**，**不是**独立 wheel；
  移动源码后必须重新 `pip install -e .`
- 不发布到 PyPI；不在 wheel 中包含完整业务脚本

### 已知不修的旧脚本缺陷（按"无行为变化"约束保留）

- `auto_greet.py` 函数体引用只在 `__main__` 定义的全局 `args` — 新 CLI 走子进程规避

### 升级提示

从 v1.1-skill-stable（旧版直接调 9 个脚本）升级：

1. 旧脚本保留作为内部实现（cli_runner 子进程调用）
2. 所有调用改用 `boss-hr` 命令
3. 不再直接 `python boss_jd.py` / `python auto_greet.py`
4. 不再创建 `spec_*.json` 模板