# 维护与验证

## 调用链

`boss-hr-auto/SKILL.md` 是唯一智能体入口，描述操作顺序、人工确认门和评分协议；所有可执行操作通过 `boss-hr` 完成。

```text
boss-hr-auto/SKILL.md
        ↓
boss_hr/cli.py → commands/ → application/
                                ↓
                    adapters/legacy_runner.py
                                ↓
                     shared/cli_runner.py
                                ↓
                     内部业务脚本 + shared/
```

CLI 负责参数、状态校验、编排和 JSON 结果，业务脚本负责浏览器操作、评分计算与报告生成。当前仍使用子进程隔离 stdout、退出码和脚本运行环境。底层脚本是正在使用的实现。

| 公开命令 | 内部实现 |
|---|---|
| `start` | 实时岗位目录、`boss-job-detail/scripts/boss_jd.py` |
| `confirm` | `shared/confirm_run.py` |
| `fetch` | `boss-recommend-downloader/scripts/recommend_list.py`、`recommend_download.py` |
| `score` | `resume-screener/scripts/prepare_scoring_inputs.py`、`collect_llm_scores.py`、`score_resumes.py`、`school_tier.py` |
| `report` | `html-report/scripts/generate_html_report.py` |
| `greet` | `boss-hr-greet/scripts/auto_greet.py` |
| `status` | `boss_hr/application/status_service.py` |
| `doctor` | `boss_hr/application/doctor_service.py` |

`shared/` 提供输出路径、岗位元数据、run 边界、候选人状态、CDP、编码与鼠标交互。`shared/cli_runner.py` 的白名单列出了仍在执行的 9 个内部工具。

评分合并与校准共用 `shared/score_identity.py`，以本轮原始简历校验候选人身份。报告转换保留逐人的 advice 和 ID；HTML 生成器只渲染模板结构，外部字段统一按文本转义。

`shared/report_context.py` 将本 run 的 `job_detail.json` 原始表单归一化为报告岗位信息，并从身份已核验的原始简历补齐评分中缺失的学校、工作年限及最近职位。岗位描述不依赖某一个任职要求标题，完整正文始终保留；评分理由兼容历史 `exp/skill/proj/major.reasons`。自动补选计分学校以 run 日期的最高已完成学历为准；无法唯一选校时展示原教育经历并保留60分，注明待复核。

正式报告以 `summary` 展示综合评价，同时展示亮点、顾虑和逐人的 `advice`，不包含恢复说明或版本对比。行动区保留候选人事实、综合评价和实际填写的沟通建议；不会把评价冒充建议，也不生成统一话术。缺少的可选内容不绘制空行或缺失提示。评语先进行HTML转义，再只将成对Markdown加粗和换行转为固定模板标签；姓名等身份文本不作格式转换。

## v1.1.4 清理依据

- 6 个原子 skill：操作已由 CLI 编排，评分说明已合并到唯一 skill，删除。
- `boss-recommend-downloader/scripts/run_all.py`：与 `fetch` 的 list → download 编排重复，且不在执行白名单内，删除。
- `html-report/templates/report.html`：报告生成器使用内置 HTML/CSS，没有读取此模板，删除。
- `tools/`、`docs/refactor/`、`docs/debug/`：旧重构阶段的基线、审计与现场记录，不属于运行依赖或当前测试入口，删除；历史可从 Git 中查询。
- shared/cli_runner.py 的旧 --spec-file 命令行入口和未使用异常/解码分支：统一 CLI 不再调用，删除，仅保留白名单子进程执行 API。
- 正在被服务调用的业务脚本、共享模块与回归测试：保留，不复制另一套实现。

## 不登录的验证方式

在仓库根目录执行：

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q --basetemp .pytest-tmp
python -m boss_hr --help
python -m boss_hr doctor --skip-browser
```

测试使用临时输出目录和浏览器替身，覆盖命令参数、确认门、run 边界、评分文件转换、报告和招呼定位。`--help` 与 `doctor --skip-browser` 不启动 Edge、不连接 BOSS。

这能验证代码契约，不能证明 BOSS 当前网页、接口或真实账号仍可用。v1.1.4 未进行招聘端登录、下载或真实打招呼测试。

## 发布

稳定线为 `stable/v1.2`，本次版本 `1.2.2`，标签 `v1.2.2`。历史标签保持不变。发布源码工具包，使用 editable install；目前不提供包含全部运行资源的独立 wheel，也不发布到 PyPI。移动源码后重新安装。
