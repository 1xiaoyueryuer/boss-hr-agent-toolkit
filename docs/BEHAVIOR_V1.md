# 稳定版行为边界

当前稳定版为 1.2.2。本文说明现有契约；具体操作见 [CLI 工作流](CLI_WORKFLOW.md)，智能体统一读取 [boss-hr-auto](../boss-hr-auto/SKILL.md)。

## 任务与确认

- 每次 start 创建新 run，不接受旧 run_id；未登录时不创建 run。
- start 实时解析岗位，成功后返回 waiting_user_confirmation；人工调整筛选条件、回复继续后才 confirm 和 fetch。
- confirm、fetch、score、report、greet、status 必须显式指定本次 run_id；禁止猜最近任务、跨 run 取输入或手工修改 confirmed/finished。
- continue、batch、多批累计、同岗位并发 run 不受支持。同一任务的 score 协调命令可反复调用完成评分文件协议，这不等于支持通用断点续跑。

## 数据与评分

fetch 内部先 list 再 download，不自动进入评分或发送。实际下载数量可能小于请求数量；失败保留记录，无跨任务补偿或事务回滚。

score 每次协调一位候选人，收集全部有效输出后执行本地校准：edu 25%、exp 25%、skill 25%、proj 15%、major 10%；总分≥70推荐、≥60待定，其余不推荐。学历分由学校表覆盖，学校缺失或表外按60计；没有硕博额外加分。评分锚点、输出字段和沟通建议只在唯一 skill 中维护。

CLI 不内置模型 API，也不自动推断所有 JD 硬门槛。报告是辅助分析结果，应核对原始证据。

评分文件的姓名、geek_id 和 job_id 必须与 manifest 对应的本轮原始简历一致。漏写 ID 时从原始简历补齐；写错身份时返回 waiting_llm 和具体校验错误，覆盖该评分文件后重试。同名候选人按 ID 区分。报告保留候选人 ID 和沟通建议；简历、JD、评分理由等外部内容均按纯文本显示，不解析其中的 HTML。

advice 来自智能体对每位候选人的分析，CLI 逐人传递，不生成默认话术。推荐、待定者的 actions.action 使用该候选人的 advice；不推荐者展示岗位相关顾虑。未提供 advice 时保持为空，不自动补成统一内容。

## 浏览器与发送

运行 start / fetch / greet 需要有效的招聘者登录态。默认连接本机 CDP 9222，必要时启动专用 Edge。start 默认不等待扫码，返回 waiting_user_login 后由用户登录并重复 start；fetch / greet 保留原有登录轮询路径。重复调用复用已有 BOSS 登录或验证页，不反复导航、刷新二维码；官方登录页未能显示内容时报告加载错误，保留页面供人工检查网络或安全验证。

report 不自动触发 greet。用户明确授权后才发送；默认按总分≥70选取，--only-names 是绕过默认筛选的高级参数，智能体必须先自行核对目标资格，禁止借此给不推荐者发送。

dry-run 仍需浏览器，仅定位、不点击、不改 run 状态；greet 可能产生本地日志。候选人不在当前列表时返回相应状态与原因，不保证每个目标都能找到，不刷新推荐页。

next_action=done 与 run.finished 含义不同。真实招呼成功且收尾条件满足时才 finish；partial_success 不自动完成任务。

## 发布与验证范围

采用 GitHub 源码工具包 + editable install，业务脚本仍为 CLI 运行依赖，不能只复制 boss_hr/ 或当作完整独立 wheel 部署。运行环境以 Windows 为主，其他系统未完成验证。

1.2.1 覆盖离线回归、Windows 全新虚拟环境安装和真实 Edge 未登录页面验证。未使用招聘者账号完成扫码后的登录、简历下载或打招呼验证。页面、接口、登录和平台限制的实时兼容性仍需在对应账号下验证。

1.2.2 是评分与报告的小范围修复，验证覆盖身份错配、同名候选人、逐人不同建议到最终 HTML，以及外部文本转义；未新增真实账号在线业务验证。
