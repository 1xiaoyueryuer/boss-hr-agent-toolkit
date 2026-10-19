# v1.1 行为边界

当前稳定版为1.1.4。本文说明现有契约；具体操作见 [CLI 工作流](CLI_WORKFLOW.md)，智能体统一读取 [boss-hr-auto](../boss-hr-auto/SKILL.md)。

## 任务与确认

- 每次 start 创建新 run，不接受旧 run_id；未登录时不创建 run。
- start 实时解析岗位，成功后返回 waiting_user_confirmation；人工调整筛选条件、回复继续后才 confirm 和 fetch。
- confirm、fetch、score、report、greet、status 必须显式指定本次 run_id；禁止猜最近任务、跨 run 取输入或手工修改 confirmed/finished。
- continue、batch、多批累计、同岗位并发 run 不受支持。同一任务的 score 协调命令可反复调用完成评分文件协议，这不等于支持通用断点续跑。

## 数据与评分

fetch 内部先 list 再 download，不自动进入评分或发送。实际下载数量可能小于请求数量；失败保留记录，无跨任务补偿或事务回滚。

score 每次协调一位候选人，收集全部有效输出后执行本地校准：edu 25%、exp 25%、skill 25%、proj 15%、major 10%；总分≥70推荐、≥60待定，其余不推荐。学历分由学校表覆盖，学校缺失或表外按60计；没有硕博额外加分。评分锚点、输出字段和沟通建议只在唯一 skill 中维护。

CLI 不内置模型 API，也不自动推断所有 JD 硬门槛。报告是辅助分析结果，应核对原始证据。

## 浏览器与发送

运行 start / fetch / greet 需要有效的招聘者登录态。默认连接本机 CDP 9222，必要时启动专用 Edge。start 默认不等待扫码，返回 waiting_user_login 后由用户登录并重复 start；fetch / greet 保留原有登录轮询路径。

report 不自动触发 greet。用户明确授权后才发送；默认按总分≥70选取，--only-names 是绕过默认筛选的高级参数，智能体必须先自行核对目标资格，禁止借此给不推荐者发送。

dry-run 仍需浏览器，仅定位、不点击、不改 run 状态；greet 可能产生本地日志。候选人不在当前列表时返回相应状态与原因，不保证每个目标都能找到，不刷新推荐页。

next_action=done 与 run.finished 含义不同。真实招呼成功且收尾条件满足时才 finish；partial_success 不自动完成任务。

## 发布与验证范围

采用 GitHub 源码工具包 + editable install，业务脚本仍为 CLI 运行依赖，不能只复制 boss_hr/ 或当作完整独立 wheel 部署。运行环境以 Windows 为主，其他系统未完成验证。

1.1.4 仅进行离线代码、文档和测试验证，未登录招聘端、未做真实下载或招呼复测。旧版本现场记录不构成本版新测试证据。页面、接口、登录和平台限制的实时兼容性仍需有可用账号时验证。
