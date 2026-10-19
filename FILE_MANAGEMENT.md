# 文件与任务数据

输出由 shared/output_manager.py 和 shared/job_registry.py 定位，run 生命周期由 shared/run_orchestrator.py 管理。用户与智能体通过 boss-hr 访问，不直接改写任务状态。

默认根目录为 ~/Desktop/boss-hr-output，可通过 BOSS_HR_OUTPUT_DIR 覆盖。岗位目录名使用不可变的 encryptJobId，中文岗位名仅为元数据。

```text
boss-hr-output/
├── jobs.json
└── <encryptJobId>/
    ├── state/                       跨 run 的候选人、下载、评分等状态
    └── runs/
        └── <run_id>/
            ├── run.json             confirmed、steps_done、finished 等
            ├── <run_id>_screening_report.html
            └── process/
                ├── job_detail.json
                ├── recommend_geek_ids.json
                ├── new_resumes.json
                ├── failed_resumes.json
                ├── scoring/
                │   ├── manifest.json
                │   ├── inputs/candidate_<geek_id>.json
                │   └── outputs/candidate_<geek_id>.json
                ├── _llm_scores.json
                ├── screening_results.json
                └── greet_log.json
```

文件按实际完成阶段产生，不保证每个文件始终存在。新任务使用新 run_id；report_file、input_file 和 output_file 均以 CLI 返回路径为准。

- start 负责实时解析岗位与创建 run，不从 jobs.json 或历史文件找任务。
- 下游显式携带同一个 encrypt_job_id 和 run_id；只读取本 run 的输入。
- 智能体只把单份评分写到 score 返回的 output_file，其他状态由 CLI 管理。
- state 保存跨任务去重等运行状态，但不授权调用方从其他 run 借用产物。
- jobs.json 默认位于输出根目录；可用 BOSS_HR_JOBS_REGISTRY 指定独立位置。
- 简历、评分、登录态和本地输出不提交到源码仓库；卸载脚本只卸载 Python 入口，不删除源码、报告或浏览器配置。

更多契约见 [行为边界](docs/BEHAVIOR_V1.md)；调用与维护见 [维护与验证](docs/ARCHITECTURE.md)。
