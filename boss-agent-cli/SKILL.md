---
name: boss-agent-cli
description: |
  BOSS 直聘 CLI 命令参考手册 — 34 个顶层命令，招聘者/求职者双模式。
  本 Skill 是 boss-hr-auto 编排流程的参考引用，不应作为入口直接加载。
type: reference
---

# boss-agent-cli

> AI Agent 专用的 BOSS 直聘本地辅助 CLI 工具 — 34 个顶层命令，默认低风险模式聚焦本地辅助、只读优先、用户主动触发，不做自动触达、批量操作或平台数据抓取。

## Install

```bash
uv tool install boss-agent-cli
# 或 pipx install boss-agent-cli
```

## 登录流程（双阶段策略）

**原则：** 有 Cookie 直接注入，无 Cookie 提示用户扫码。

### 第1步：检查 CLI 登录态

```bash
boss status                    # 检查 CLI 登录态
boss --role recruiter status   # 招聘者模式
```

如果返回 `logged_in: true`，说明 CLI 已有登录凭证 **→ 尝试提取 Cookie 注入到 CDP 浏览器**。

### 第2步（优先）：Cookie 注入（CLI 已登录时）

```bash
# 第一步：用 IDA 获取 Cookie
# 第二步：将 Cookie 通过 CDP add_cookies 注入浏览器
# 第三步：刷新 BOSS 页面验证是否自动登录
```

关键 Cookie 字段：`zp_at`（认证令牌）、`wt2`（登录态）、`__zp_seo_uuid__`、`bst`

注入后导航到目标页面即可，无需用户扫码。

### 第3步（兜底）：提示用户扫码（CLI 未登录 / Cookie 注入失败时）

1. **启动 CDP 浏览器：**
   ```bash
   # Windows Edge（推荐）
   "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" \
     --remote-debugging-port=9222 \
     --user-data-dir="%USERPROFILE%\.workbuddy\chrome-profiles\boss-cdp" \
     --remote-allow-origins=*
   ```

2. **打开 BOSS 登录页：**
   - 导航到 `https://www.zhipin.com/web/user/?ka=header-login`
   - **→ 明确告知用户「登录页面已打开，请扫码登录」**

3. **等待用户确认登录成功后**，再进行后续操作（JD 提取 / 简历下载等）

### 登录后验证

```bash
boss --role recruiter me                   # 验证招聘者信息
boss --role recruiter hr jobs list         # 验证可看到在线职位
```

### 常见问题

- `boss status` 可能假阳性（返回登录但实际 token 已过期），用 `boss me` 或 `boss hr jobs list` 验证更可靠
- Windows 上 PYTHONHOME 环境变量可能冲突，运行命令前加 `PYTHONHOME=""`

## Recruiter Workflow

| Command | Description |
|---------|-------------|
| `boss hr applications` | 查看候选人投递申请 |
| `boss hr candidates <keyword>` | 搜索候选人 |
| `boss hr chat` | 招聘者沟通列表 |
| `boss hr resume` | 查看/请求候选人简历 |
| `boss hr reply <friend_id> <message>` | 回复候选人消息 |
| `boss hr request-resume <friend_id> --job-id <id>` | 请求候选人附件简历 |
| `boss hr jobs list/online/offline` | 职位列表与上下线管理 |

## Key Commands

| Command | Description |
|---------|-------------|
| `boss schema` | 返回全部命令 JSON（Agent 首先调用） |
| `boss search <query>` | 搜索职位（8 维筛选） |
| `boss detail <security_id>` | 职位详情 |
| `boss hr resume <encryptGeekId> --job-id <encryptJobId> --security-id <securityId>` | 查看候选人简历 |
| `boss login` | 四级降级登录 |
| `boss status` | 检查登录态 |
| `boss doctor` | 诊断环境 |

## Output Conventions

- **stdout**: JSON only (structured envelope)
- **stderr**: Logs and progress (controlled by `--log-level`)
- **exit 0**: Success (`ok: true`)
- **exit 1**: Failure (`ok: false`)

## Platform & Role

```bash
# 招聘者模式
boss --role recruiter --platform zhipin --cdp-url http://localhost:9222 <command>

# 求职者模式（默认）
boss ... (no flags)
```

## Safety

- 不自动发消息（CLI 权限不足）
- 默认低风险模式阻断批量操作
- 候选人数据链路默认阻断敏感操作
