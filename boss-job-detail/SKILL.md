---
name: boss-job-detail
description: |
  通过 CDP 浏览器提取 BOSS 直聘岗位完整 JD（职责、要求等）。

  **本 Skill 是 boss-hr-auto 编排流程的子步骤（Step 1），通常在 boss-hr-auto 工作流中调用，不应作为入口 Skill 直接加载。**
---

# BOSS 直聘岗位 JD 提取

通过 CDP 连接到已登录的浏览器，自动导航到岗位编辑页提取完整 JD。

## 前提条件

- Edge/Chrome 以 `--remote-debugging-port=9222` 启动
- Boss 招聘者 session 已登录

## 用法

```bash
python scripts/boss_jd.py <查询条件>
```

查询条件支持三种：
- `加密岗位ID` — `a2bb7b1e7a76f0440nZ-39S0E1NS`
- `数字 jobId` — `524499312`
- `岗位名称` — `管培生`（精确匹配优先，否则模糊匹配）

## 工作流程

1. 通过 boss CLI 获取岗位列表，匹配查询条件
2. 用 patchright 连接 CDP 浏览器
3. 导航到岗位编辑页（`/web/chat/job/edit?encryptId=...`）
4. 等待 iframe 加载完成后提取表单内容
5. 输出到 `WorkBuddy/boss-resumes/jd/<encryptJobId>.json`

## 输出格式

```json
{
  "jobName": "管培生",
  "encryptJobId": "a2bb7b1e...",
  "bodyText": "完整页面文本",
  "formValues": ["岗位职责：...", "职位名称", "..."],
  "parsed": {
    "title": "",
    "positionType": "",
    "location": "",
    "description": "岗位职责 + 任职要求 原文"
  }
}
```

## 技术要点

- BOSS 管理后台为 iframe 架构：主页面是导航壳，表单内容在 `src="/web/frame/job/edit?..."` 的子框架中
- 使用 `domcontentloaded` 而非 `networkidle` 以提速（5-8s）
- boss CLI 输出为 GBK 编码，需 `decode('gbk')`
