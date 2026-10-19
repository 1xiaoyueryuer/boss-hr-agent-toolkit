# CLI 工作流

用户与智能体统一使用 `boss-hr`；智能体的流程和评分规则见 [boss-hr-auto](../boss-hr-auto/SKILL.md)。

## 命令与浏览器依赖

| 命令 | 必要参数 | 浏览器 | 成功结果 |
|---|---|---|---|
| start | query：名称、jobId 或 encryptJobId | 需要 | waiting_user_confirmation 或 waiting_user_login |
| confirm | --job-name、--encrypt-job-id、--run-id | 不需要 | confirmed |
| fetch | 同上，加 --count N | 需要 | candidates_fetched |
| score | --job-name、--encrypt-job-id、--run-id | 不需要 | waiting_llm 或 scoring_complete |
| report | 同上 | 不需要 | report_ready |
| greet | 同上 | 需要 | greet_complete、partial_success、no_candidates 或 dry-run 状态 |
| status | 同上 | 不需要 | status=ok，另有 workflow_state |
| doctor | 无；可选 --skip-browser / --launch-edge | 默认检查 CDP | 环境诊断结果 |

岗位 ID 可用 BOSS_HR_ENCRYPT_JOB_ID 兜底，但明确传参更便于追踪。各命令的完整参数以 `boss-hr <命令> --help` 为准；查看帮助不会连接浏览器。

## 按阶段执行

```powershell
boss-hr start "结构工程师"
```

start 实时解析岗位，不从 jobs.json 或旧 run 选 ID，不接受 --run-id。启动专用 Edge 后若未登录，默认立即返回 waiting_user_login、next_action=scan_login_then_repeat_start，不创建 run。人工登录后重试同一命令；只有 login_page_opened=true 才表示已打开登录页。

start 的 --login-wait-seconds 默认0；正数仅供人工调试等待，负数无效。--no-auto-launch 关闭自动启动；不会使 start 变成离线命令。fetch / greet 仍沿用原有最多20秒登录轮询，未就绪时返回对应错误与恢复信息。

拿到 waiting_user_confirmation 后停下，请用户调整推荐牛人页面筛选条件。用户确认后，使用 start 返回的同一组标识：

```powershell
boss-hr confirm --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<run_id>"
boss-hr fetch --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<run_id>" --count 10
boss-hr score --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<run_id>"
```

fetch 返回 requested_count、listed_count、downloaded_count、failed_count 和文件路径；下载不足时保留实际结果，不自动跨 run 补数据。

score 返回 waiting_llm 时，读取 data.input_file，岗位依据使用 start 返回的本 run 的 data.job_detail_file，按唯一 skill 的评分协议写单个 object 到 data.output_file，再执行同一条 score。每次处理一位，循环直到 scoring_complete。CLI 内部负责准备输入、校验输出、合并与重算分数；调用方不单独运行这些内部步骤。

```powershell
boss-hr report --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<run_id>"
boss-hr status --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<run_id>"
```

report 返回 data.report_file；报告就绪后交付用户。用户明确要求打招呼时才执行：

```powershell
boss-hr greet --job-name "<岗位名>" --encrypt-job-id "<岗位ID>" --run-id "<run_id>" --dry-run
```

dry-run 需要浏览器，只定位、不发送、不修改 run 状态。去掉 --dry-run 才是真实发送。默认 threshold=70、max=10；--only-names 会切换到按姓名选取并忽略 threshold/max，使用前必须核对评分与用户授权。部分目标不在当前推荐列表时应交付 partial_success，或 data.greet_log_status=all_not_found 与 next_action=review_warnings 的信息，不自动刷新页面或反复发送。

## 输出与错误

除 status 保留独立格式外，成功结果包含 ok、command、status，按命令附带 run_id、data、next_action；失败时包含 ok=false 与 error。退出码独立于 JSON，不要只检查文件是否存在。

status 成功时为 status=ok，任务阶段见 workflow_state；confirmed、finished、steps_done 和 paths 是顶层字段。它不使用通用的 ok/data 包装。

| 退出码 | 含义 |
|---|---|
| 0 | 命令成功；仍可能需要用户登录、确认、评分或复核 warnings |
| 1 | 业务或环境错误，读取 error |
| 2 | 参数错误或缺少 run_id |
| 20 | 尚未人工确认 |
| 23 / 24 | run 不存在 / 岗位不匹配 |
| 26 / 27 | 缺少输入 / 缺少输出 |

环境问题按 error.remediation 恢复；岗位歧义按 data.candidates 选择。禁止扫描最近 run、改写状态文件或调用内部脚本绕过边界。`next_action=done` 只表示本命令没有下一项自动动作，不表示 run.finished=true。

## 离线诊断

```powershell
boss-hr doctor --skip-browser
```

该选项只检查本地 Python 和 patchright，不连接 CDP、不启动 Edge。完整测试方式见 [维护与验证](ARCHITECTURE.md)。
