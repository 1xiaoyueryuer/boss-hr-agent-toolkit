# -*- coding: utf-8 -*-
"""HTML 报告生成（统一方案）

设计核心：
  - 通用 schema：接受任意 score_resumes.py 输出的 screening_results.json
  - 视觉沿用原 generate_report.py 风格（黑蓝渐变 + 玻璃质感 + stat-card 配色）
  - 字段全部 .get() 处理，缺失自动跳过
  - LLM 自由填写的亮点/顾虑/建议直接渲染

输入：screening_results.json（score_resumes.py 输出）
  {
    "job_name": str,
    "meta": {
      "title", "subtitle",
      "job": { "name", "company", "location", "salary", "experience_required", "degree_required" },
      "type_judgment": { "type", "reason" },
      "core_requirements": [str, ...]
    },
    "summary": { "total", "recommend", "pending", "reject" },
    "dimension_labels": [str, ...],
    "candidates": [
      {
        "rank", "name", "tier", "total",
        "school", "work_years", "current_role",
        "hard_pass", "hard_reason",
        "dimensions": [{"pct", "weighted", "weight", "reason"}],
        "highlights": [str, ...],
        "concerns": [str, ...]
      }
    ],
    "actions": {
      "recommend": [{"name", "score", "background", "action"}],
      "pending": [{"name", "score", "strengths", "action"}],
      "reject": [{"name", "score", "concerns"}]
    }
  }
"""
import json
import os
import sys
import io
import argparse
import datetime
from pathlib import Path

# 注意：禁止在 import 时重写 sys.stdout（会导致被 import 的驱动脚本 print 报
# "I/O operation on closed file"）。改为仅在需要时安全地 reconfigure 编码，
# 不替换对象本身，避免破坏外层脚本的 stdout。
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


# === CSS（沿用 generate_report.py 视觉风格）===
CSS = """
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif; background:#f5f7fa; color:#1f2937; line-height:1.6; padding:20px; }
.container { max-width:1200px; margin:0 auto; }
.header { background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%); color:#fff; padding:36px 32px; border-radius:12px; margin-bottom:24px; box-shadow:0 4px 12px rgba(0,0,0,0.15); }
.header h1 { font-size:28px; margin-bottom:8px; }
.header .subtitle { opacity:0.85; margin-bottom:20px; font-size:14px; }
.header .run-badge { display:inline-block; background:rgba(255,255,255,0.12); padding:6px 14px; border-radius:20px; font-size:12px; margin-bottom:14px; font-family:monospace; letter-spacing:0.3px; }
.meta-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-top:16px; }
.meta-item { background:rgba(255,255,255,0.08); padding:12px 16px; border-radius:8px; backdrop-filter:blur(10px); }
.meta-item .label { font-size:11px; opacity:0.75; text-transform:uppercase; letter-spacing:0.5px; }
.meta-item .value { font-size:16px; font-weight:600; margin-top:4px; }
.section { background:#fff; border-radius:12px; padding:28px; margin-bottom:20px; box-shadow:0 1px 3px rgba(0,0,0,0.06); }
.section h2 { font-size:20px; margin-bottom:20px; color:#1a1a2e; border-bottom:2px solid #e5e7eb; padding-bottom:10px; }
.overview { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:16px; }
.stat-card { padding:20px; border-radius:10px; text-align:center; border-left:4px solid; }
.stat-card.green { background:#ecfdf5; border-color:#059669; }
.stat-card.yellow { background:#fffbeb; border-color:#d97706; }
.stat-card.red { background:#fef2f2; border-color:#dc2626; }
.stat-card.blue { background:#eff6ff; border-color:#3b82f6; }
.stat-card .num { font-size:32px; font-weight:700; margin-bottom:4px; }
.stat-card.green .num { color:#059669; }
.stat-card.yellow .num { color:#d97706; }
.stat-card.red .num { color:#dc2626; }
.stat-card.blue .num { color:#3b82f6; }
.stat-card .label { font-size:13px; color:#6b7280; }
table { width:100%; border-collapse:collapse; }
th { background:#f9fafb; padding:12px 10px; text-align:left; font-size:13px; color:#6b7280; font-weight:600; border-bottom:2px solid #e5e7eb; }
td { padding:12px 10px; border-bottom:1px solid #f3f4f6; font-size:14px; }
tr:hover { background:#f9fafb; }
.rank { font-weight:600; color:#6b7280; }
.total { font-weight:700; color:#1a1a2e; font-size:15px; }
.row-rejected { opacity:0.6; }
.badge { display:inline-block; padding:3px 10px; border-radius:12px; font-size:12px; font-weight:600; }
.badge-green { background:#d1fae5; color:#065f46; }
.badge-yellow { background:#fef3c7; color:#92400e; }
.badge-red { background:#fee2e2; color:#991b1b; }
.card { background:#fff; border:1px solid #e5e7eb; border-radius:10px; padding:20px; margin-bottom:16px; }
.card.rejected { background:#fafafa; opacity:0.7; }
.card-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:12px; }
.card-title { display:flex; align-items:center; gap:12px; }
.card-title h3 { font-size:18px; color:#1a1a2e; }
.total-score { font-size:24px; font-weight:700; color:#0f3460; }
.meta { display:flex; flex-wrap:wrap; gap:14px; margin-bottom:16px; font-size:13px; color:#6b7280; }
.reject-reason { padding:10px; background:#fee2e2; color:#991b1b; border-radius:6px; font-size:13px; }
.dim-row { margin-bottom:14px; }
.dim-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:4px; font-size:13px; }
.dim-label { color:#4b5563; }
.dim-weight { color:#9ca3af; font-size:11px; margin-left:4px; }
.dim-score { color:#6b7280; }
.dim-score strong { color:#1a1a2e; }
.bar { height:4px; background:#f3f4f6; border-radius:2px; overflow:hidden; margin-bottom:4px; }
.bar-fill { height:100%; border-radius:2px; transition:width 0.3s; }
.dim-reason { font-size:12px; color:#9ca3af; font-style:italic; }
.custom-section { margin-top:12px; padding:10px 14px; border-radius:6px; }
.custom-section.highlights { background:#ecfdf5; border-left:3px solid #059669; }
.custom-section.concerns { background:#fef2f2; border-left:3px solid #dc2626; }
.custom-label { font-size:13px; font-weight:600; margin-bottom:6px; }
.custom-section.highlights .custom-label { color:#065f46; }
.custom-section.concerns .custom-label { color:#991b1b; }
.custom-section ul { margin-left:20px; font-size:13px; line-height:1.7; }
.custom-section li { margin-bottom:3px; }
.action-group { margin-bottom:24px; }
.action-group h3 { font-size:16px; margin-bottom:12px; color:#1a1a2e; }
.action-card { background:#f9fafb; border-left:4px solid #0f3460; padding:16px; border-radius:6px; margin-bottom:12px; }
.action-header { display:flex; justify-content:space-between; margin-bottom:10px; }
.action-name { font-weight:700; font-size:15px; color:#1a1a2e; }
.action-score { color:#0f3460; font-weight:600; }
.action-section { font-size:13px; margin-bottom:8px; color:#374151; line-height:1.7; }
.action-section:last-child { margin-bottom:0; }
.reject-item { padding:8px 12px; background:#fafafa; border-radius:4px; margin-bottom:6px; font-size:13px; color:#4b5563; }
.jd-summary { background:#f9fafb; padding:16px; border-radius:8px; margin-top:12px; }
.jd-summary h4 { font-size:14px; color:#1a1a2e; margin-bottom:8px; }
.jd-summary ul { margin-left:20px; font-size:13px; color:#4b5563; }
.jd-summary li { margin-bottom:4px; }
.footer { text-align:center; color:#9ca3af; font-size:12px; padding:20px; }
"""


# === 视觉工具函数 ===
def bar_color(pct):
    if pct >= 70:
        return "#059669"
    if pct >= 50:
        return "#d97706"
    return "#dc2626"


def tier_badge(tier):
    return {
        "推荐": '<span class="badge badge-green">✅ 推荐</span>',
        "待定": '<span class="badge badge-yellow">📌 待定</span>',
        "不推荐": '<span class="badge badge-red">❌ 不推荐</span>',
        "硬淘汰": '<span class="badge badge-red">硬门槛淘汰</span>',
    }.get(tier, "")


# === 渲染函数 ===
def render_candidate(c, labels, rank):
    if not c.get("hard_pass", True):
        return f"""
    <div class="card rejected">
      <div class="card-header">
        <div class="card-title">
          <span class="rank">❌</span>
          <h3>{c['name']}</h3>
          {tier_badge('硬淘汰')}
        </div>
        <div class="total-score">—</div>
      </div>
      <div class="reject-reason">🚫 {c.get('hard_reason', '未通过硬门槛')}</div>
    </div>"""

    dims_html = ""
    for i, d in enumerate(c.get("dimensions", [])):
        label = labels[i] if i < len(labels) else f"维度{i+1}"
        color = bar_color(d.get("pct", 0))
        dims_html += f"""
        <div class="dim-row">
          <div class="dim-header">
            <span class="dim-label">{label} <span class="dim-weight">({d.get('weight', 0)}%)</span></span>
            <span class="dim-score">{d.get('pct', 0)}% → <strong>{d.get('weighted', 0):.2f}</strong> 分</span>
          </div>
          <div class="bar"><div class="bar-fill" style="width:{d.get('pct', 0)}%;background:{color}"></div></div>
          <div class="dim-reason">{d.get('reason', '')}</div>
        </div>"""

    custom_html = ""
    if c.get("highlights"):
        items = "".join(f"<li>{h}</li>" for h in c["highlights"])
        custom_html += f"""
        <div class="custom-section highlights">
          <div class="custom-label">✨ 亮点</div>
          <ul>{items}</ul>
        </div>"""
    if c.get("concerns"):
        items = "".join(f"<li>{x}</li>" for x in c["concerns"])
        custom_html += f"""
        <div class="custom-section concerns">
          <div class="custom-label">⚠ 顾虑</div>
          <ul>{items}</ul>
        </div>"""

    return f"""
    <div class="card">
      <div class="card-header">
        <div class="card-title">
          <span class="rank">#{rank}</span>
          <h3>{c['name']}</h3>
          {tier_badge(c.get('tier', ''))}
        </div>
        <div class="total-score">{c.get('total', 0):.2f}</div>
      </div>
      <div class="meta">
        <span>🎓 {c.get('school', '')}</span>
        <span>💼 {c.get('work_years', '')} · {c.get('current_role', '')}</span>
      </div>
      <div class="dims">{dims_html}</div>
      {custom_html}
    </div>"""


def render_action(name, score, body):
    return f"""
    <div class="action-card">
      <div class="action-header">
        <span class="action-name">{name}</span>
        <span class="action-score">{score:.2f} 分</span>
      </div>
      {body}
    </div>"""


def _tier_badge_v2(decision):
    return {
        "A": '<span class="badge badge-green">✅ 各项都很合适，建议直接进入面试</span>',
        "B": '<span class="badge badge-yellow">📌 部分技能仍需确认，但适合进入面试</span>',
        "C": '<span class="badge badge-red">❌ 完全不合适，不用考虑</span>',
    }.get(decision, "")


def _candidate_html_v2(c, rank, scorecard=None):
    dims_html = ""
    jm = c.get("job_match_score", 0)
    edu = c.get("education_score", 0)
    final = c.get("final_score", 0)
    decision = c.get("decision", "")
    decision_label = c.get("decision_label", "")
    decision_reasons = c.get("decision_reasons", [])
    best_track = c.get("best_track", "")

    dims_html = f"""
    <div class="dim-row">
      <div class="dim-header">
        <span class="dim-label">岗位匹配分</span>
        <span class="dim-score"><strong>{jm:.2f}</strong></span>
      </div>
      <div class="bar"><div class="bar-fill" style="width:{jm}%;background:{bar_color(jm)}"></div></div>
    </div>
    <div class="dim-row">
      <div class="dim-header">
        <span class="dim-label">学历偏好分</span>
        <span class="dim-score"><strong>{edu:.2f}</strong></span>
      </div>
      <div class="bar"><div class="bar-fill" style="width:{edu}%;background:{bar_color(edu)}"></div></div>
    </div>
    <div class="dim-row">
      <div class="dim-header">
        <span class="dim-label">最终分（85% 岗位 + 15% 学历）</span>
        <span class="dim-score"><strong>{final:.2f}</strong></span>
      </div>
      <div class="bar"><div class="bar-fill" style="width:{final}%;background:{bar_color(final)}"></div></div>
    </div>
    """

    custom_html = ""
    if c.get("highlights"):
        items = "".join(f"<li>{h}</li>" for h in c["highlights"])
        custom_html += f"""
        <div class="custom-section highlights">
          <div class="custom-label">✨ 亮点</div>
          <ul>{items}</ul>
        </div>"""
    if c.get("concerns"):
        items = "".join(f"<li>{x}</li>" for x in c["concerns"])
        custom_html += f"""
        <div class="custom-section concerns">
          <div class="custom-label">⚠ 顾虑</div>
          <ul>{items}</ul>
        </div>"""

    vq_html = ""
    if c.get("verification_questions"):
        items = "".join(f"<li>{q}</li>" for q in c["verification_questions"])
        vq_html = f"""
        <div class="custom-section" style="background:#fffbeb;border-left:3px solid #d97706;">
          <div class="custom-label">❓ 面试确认问题</div>
          <ul>{items}</ul>
        </div>"""

    reasons_html = ""
    if decision_reasons:
        items = "".join(f"<li>{r}</li>" for r in decision_reasons)
        reasons_html = f"""
        <div class="custom-section" style="background:#eef2ff;border-left:3px solid #6366f1;">
          <div class="custom-label">判定理由</div>
          <ul>{items}</ul>
        </div>"""

    contact = "✅ 建议招呼" if c.get("contact_recommended") else "❌ 不建议招呼"
    tier_html = c.get("decision_label", "")

    return f"""
    <div class="card">
      <div class="card-header">
        <div class="card-title">
          <span class="rank">#{rank}</span>
          <h3>{c.get('name','')}</h3>
          {_tier_badge_v2(decision)}
        </div>
        <div class="total-score">{final:.2f}</div>
      </div>
      <div class="meta">
        <span>🎓 {c.get('education_tier','')}</span>
        <span>🛤 最佳方向: {best_track or '-'}</span>
        <span>📞 {contact}</span>
      </div>
      <div class="dims">{dims_html}{custom_html}{vq_html}{reasons_html}</div>
    </div>"""


def _render_v2(data: dict) -> str:
    """v2 schema 的 HTML 报告（A/B/C 分组）。"""
    meta = data.get("meta", {}) or {}
    job = data.get("job", {}) or (meta.get("job", {}) if isinstance(meta, dict) else {})
    summary = data.get("summary", {}) or {}
    scorecard = data.get("scorecard", {}) or {}

    candidates = data.get("candidates", []) or []
    A_list = [c for c in candidates if c.get("decision") == "A"]
    B_list = [c for c in candidates if c.get("decision") == "B"]
    C_list = [c for c in candidates if c.get("decision") == "C"]

    # 渲染 A/B/C 分组
    def _section(title, color, lst, rank_offset=0):
        if not lst:
            return ""
        rows = []
        for i, c in enumerate(lst):
            rank = rank_offset + i + 1
            rows.append(f'<tr><td class="rank">#{rank}</td><td><strong>{c.get("name","")}</strong></td>'
                       f'<td class="total">{c.get("final_score", 0):.2f}</td>'
                       f'<td>{c.get("job_match_score", 0):.2f}</td>'
                       f'<td>{c.get("education_score", 0):.2f}</td>'
                       f'<td>{_tier_badge_v2(c.get("decision",""))}</td></tr>')
        return (f'<section class="section">'
                f'<h2 style="color:{color}">{title}（{len(lst)} 人）</h2>'
                f'<table><thead><tr><th>排名</th><th>姓名</th><th>最终分</th>'
                f'<th>岗位匹配</th><th>学历偏好</th><th>建议</th></tr></thead>'
                f'<tbody>{"".join(rows)}</tbody></table></section>')

    sections_html = (
        _section("✅ A：各项都很合适，建议直接进入面试", "#059669", A_list, 0)
        + _section("📌 B：部分技能仍需确认，但适合进入面试", "#d97706", B_list,
                   len(A_list))
        + _section("❌ C：完全不合适，不用考虑", "#dc2626", C_list,
                   len(A_list) + len(B_list))
    )

    # 候选人详情卡片
    detail_cards = "".join(
        _candidate_html_v2(c, idx + 1, scorecard)
        for idx, c in enumerate(candidates)
    )

    # 评分卡摘要
    scorecard_html = ""
    if scorecard:
        common = scorecard.get("common_requirements", [])
        tracks = scorecard.get("alternative_tracks", [])
        items_html = ""
        for req in common:
            items_html += f'<li><strong>{req.get("id")}:</strong> {req.get("description","")} <em>({req.get("importance","")})</em></li>'
        track_html = ""
        for t in tracks:
            track_html += f'<h4>{t.get("name","")}</h4><ul>'
            for req in t.get("requirements", []):
                track_html += f'<li><strong>{req.get("id")}:</strong> {req.get("description","")} <em>({req.get("importance","")})</em></li>'
            track_html += "</ul>"
        edu_req = scorecard.get("education_requirements", {})
        edu_html = f'<p>最低学历：{edu_req.get("minimum_degree","未规定")}（来源：{edu_req.get("source_evidence","-")}）</p>' if edu_req else ""
        weight_html = f'<p>权重：岗位 {data.get("policy_version", "abc-education-v1")} 0.85 / 学历 0.15</p>'
        scorecard_html = f"""
        <section class="section">
          <h2>📋 岗位评分卡</h2>
          <h3>共同要求</h3>
          <ul>{items_html}</ul>
          <h3>可选方向</h3>
          {track_html}
          <h3>学历要求</h3>
          {edu_html}
          {weight_html}
        </section>
        """

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>简历筛选报告（v2 · A/B/C）</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<header class="header">
  <h1>📊 {data.get('job_name','简历筛选报告')} · A/B/C 面试结论</h1>
  <div class="subtitle">独立 LLM worker + Python 确定性评分（v2 协议）</div>
  {f'<div class="run-badge">🆔 run_id: {data.get("run_id","")}　|　🕐 生成时间: {datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>' if data.get("run_id") else ''}
  <div class="meta-grid">
    <div class="meta-item"><div class="label">总候选人</div><div class="value">{summary.get("total", len(candidates))}</div></div>
    <div class="meta-item"><div class="label">A 类（直接面试）</div><div class="value">{summary.get("A", len(A_list))}</div></div>
    <div class="meta-item"><div class="label">B 类（需确认）</div><div class="value">{summary.get("B", len(B_list))}</div></div>
    <div class="meta-item"><div class="label">C 类（不考虑）</div><div class="value">{summary.get("C", len(C_list))}</div></div>
    <div class="meta-item"><div class="label">建议招呼</div><div class="value">{summary.get("contact_recommended", 0)}</div></div>
  </div>
</header>

{scorecard_html}

<section class="section">
  <h2>🎯 A/B/C 分组</h2>
  {sections_html if sections_html else '<p>暂无候选人。</p>'}
</section>

<section class="section">
  <h2>👤 候选人详情</h2>
  {detail_cards if detail_cards else '<p>暂无候选人。</p>'}
</section>

<div class="footer">生成时间：{datetime.datetime.now().strftime("%Y-%m-%d %H:%M")} · v2 协议</div>
</div>
</body>
</html>"""
    meta = data.get("meta", {})
    summary = data.get("summary", {})
    labels = data.get("dimension_labels", ["学历", "工作经验", "专业技能", "项目经历", "专业匹配"])
    candidates = data.get("candidates", [])
    actions = data.get("actions", {})

    # 排名表
    rank_rows = ""
    for c in candidates:
        if not c.get("hard_pass", True):
            empty_cells = "".join("<td>—</td>" for _ in labels)
            rank_rows += f'<tr class="row-rejected"><td>❌</td><td>{c["name"]}</td><td>—</td>{empty_cells}<td>{tier_badge("硬淘汰")}</td></tr>'
        else:
            dim_cells = "".join(f"<td>{d.get('weighted', 0):.2f}</td>" for d in c.get("dimensions", []))
            rank_rows += f'<tr><td class="rank">#{c.get("rank", "?")}</td><td><strong>{c["name"]}</strong></td><td class="total">{c.get("total", 0):.2f}</td>{dim_cells}<td>{tier_badge(c.get("tier", ""))}</td></tr>'

    cand_cards = "".join(render_candidate(c, labels, c.get("rank", i + 1)) for i, c in enumerate(candidates))

    # 行动建议
    recommend_html = ""
    if actions.get("recommend"):
        rows = "".join(render_action(
            a["name"], a["score"],
            f"<div class='action-section'><strong>📋 候选人背景：</strong>{a.get('background', '')}</div>"
            f"<div class='action-section'><strong>🎯 沟通方向：</strong>{a.get('action', '')}</div>"
        ) for a in actions["recommend"])
        recommend_html = f'<section class="action-group"><h3>✅ 强烈推荐面试（≥70 分）</h3>{rows}</section>'

    pending_html = ""
    if actions.get("pending"):
        rows = "".join(render_action(
            a["name"], a["score"],
            f"<div class='action-section'><strong>✅ 优势：</strong>{a.get('strengths', '')}</div>"
            f"<div class='action-section'><strong>❓ 需确认问题：</strong>{a.get('action', '')}</div>"
        ) for a in actions["pending"])
        pending_html = f'<section class="action-group"><h3>📌 待沟通确认（60-69 分）</h3>{rows}</section>'

    reject_html = ""
    if actions.get("reject"):
        items = "".join(
            f"<div class='reject-item'><strong>{r['name']}</strong>（{r['score']:.1f} 分）— {r.get('concerns', '')}</div>"
            for r in actions["reject"]
        )
        reject_html = f'<section class="action-group"><h3>❌ 不推荐</h3>{items}</section>'

    job = meta.get("job", {})
    type_judge = meta.get("type_judgment", {})
    th_cells = "".join(
        f'<th>{labels[i]} {w}%</th>'
        for i, w in enumerate([25, 25, 25, 15, 10][:len(labels)])
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{meta.get('title', '简历筛选报告')}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<header class="header">
  <h1>📊 {meta.get('title', '简历筛选报告')}</h1>
  <div class="subtitle">{meta.get('subtitle', '')}</div>
  {f'<div class="run-badge">🆔 run_id: {data.get("run_id") or meta.get("run_id", "")}　|　🕐 生成时间: {data.get("generated_at") or meta.get("generated_at") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>' if (data.get('run_id') or meta.get('run_id')) else ''}
  <div class="meta-grid">
    <div class="meta-item"><div class="label">岗位</div><div class="value">{job.get('name', '')}</div></div>
    <div class="meta-item"><div class="label">公司</div><div class="value">{job.get('company', '')}</div></div>
    <div class="meta-item"><div class="label">地点</div><div class="value">{job.get('location', '')}</div></div>
    <div class="meta-item"><div class="label">薪资</div><div class="value">{job.get('salary', '')}</div></div>
    <div class="meta-item"><div class="label">经验要求</div><div class="value">{job.get('experience_required', '')}</div></div>
    <div class="meta-item"><div class="label">学历要求</div><div class="value">{job.get('degree_required', '')}</div></div>
  </div>
</header>

<section class="section">
  <h2>🎯 岗位类型判定</h2>
  <p><strong>{type_judge.get('type', '')}</strong> —— {type_judge.get('reason', '')}</p>
  <div class="jd-summary">
    <h4>核心任职要求</h4>
    <ul>{''.join(f'<li>{r}</li>' for r in meta.get('core_requirements', []))}</ul>
  </div>
</section>

<section class="section">
  <h2>📈 筛选总览</h2>
  <div class="overview">
    <div class="stat-card green"><div class="num">{summary.get('recommend', 0)}</div><div class="label">✅ 推荐面试 (≥70)</div></div>
    <div class="stat-card yellow"><div class="num">{summary.get('pending', 0)}</div><div class="label">📌 待沟通确认 (60-69)</div></div>
    <div class="stat-card red"><div class="num">{summary.get('reject', 0)}</div><div class="label">❌ 不推荐 (&lt;60)</div></div>
    <div class="stat-card blue"><div class="num">{summary.get('total', 0)}</div><div class="label">📊 总候选人</div></div>
  </div>
</section>

<section class="section">
  <h2>🏆 候选人排名</h2>
  <table>
    <thead>
      <tr>
        <th>排名</th><th>姓名</th><th>总分</th>
        {th_cells}
        <th>建议</th>
      </tr>
    </thead>
    <tbody>{rank_rows}</tbody>
  </table>
</section>

<section class="section">
  <h2>👤 候选人详情</h2>
  {cand_cards}
</section>

<section class="section">
  <h2>🎯 行动建议</h2>
  {recommend_html}
  {pending_html}
  {reject_html}
</section>

<div class="footer">{data.get('footer', f'生成时间：{datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}')}</div>
</div>
</body>
</html>"""


# === CLI 入口 ===
def render(data: dict) -> str:
    """v1/v2 自动分支：v2 schema 用 A/B/C 渲染。"""
    if data.get("schema_version") == "2.0":
        return _render_v2(data)
    return _render_v1(data)


def _render_v1(data: dict) -> str:
    meta = data.get("meta", {})
    summary = data.get("summary", {})
    labels = data.get("dimension_labels", ["学历", "工作经验", "专业技能", "项目经历", "专业匹配"])
    candidates = data.get("candidates", [])
    actions = data.get("actions", {})

    # 排名表
    rank_rows = ""
    for c in candidates:
        if not c.get("hard_pass", True):
            empty_cells = "".join("<td>—</td>" for _ in labels)
            rank_rows += f'<tr class="row-rejected"><td>❌</td><td>{c["name"]}</td><td>—</td>{empty_cells}<td>{tier_badge("硬淘汰")}</td></tr>'
        else:
            dim_cells = "".join(f'<td>{d.get("weighted", 0):.2f}</td>' for d in c.get("dimensions", []))
            rank_rows += f'<tr><td class="rank">#{c.get("rank", "?")}</td><td><strong>{c["name"]}</strong></td><td class="total">{c.get("total", 0):.2f}</td>{dim_cells}<td>{tier_badge(c.get("tier", ""))}</td></tr>'

    cand_cards = "".join(render_candidate(c, labels, c.get("rank", i + 1)) for i, c in enumerate(candidates))

    # 行动建议
    recommend_html = ""
    if actions.get("recommend"):
        rows = "".join(render_action(
            a["name"], a["score"],
            f"<div class='action-section'><strong>📋 候选人背景：</strong>{a.get('background', '')}</div>"
            f"<div class='action-section'><strong>🎯 沟通方向：</strong>{a.get('action', '')}</div>"
        ) for a in actions["recommend"])
        recommend_html = f'<section class="action-group"><h3>✅ 强烈推荐面试（≥70 分）</h3>{rows}</section>'

    pending_html = ""
    if actions.get("pending"):
        rows = "".join(render_action(
            a["name"], a["score"],
            f"<div class='action-section'><strong>✅ 优势：</strong>{a.get('strengths', '')}</div>"
            f"<div class='action-section'><strong>❓ 需确认问题：</strong>{a.get('action', '')}</div>"
        ) for a in actions["pending"])
        pending_html = f'<section class="action-group"><h3>📌 待沟通确认（60-69 分）</h3>{rows}</section>'

    reject_html = ""
    if actions.get("reject"):
        items = "".join(
            f'<div class="reject-item"><strong>{r["name"]}</strong>（{r["score"]:.1f} 分）— {r.get("concerns", "")}</div>'
            for r in actions["reject"]
        )
        reject_html = f'<section class="action-group"><h3>❌ 不推荐</h3>{items}</section>'

    job = meta.get("job", {})
    type_judge = meta.get("type_judgment", {})
    th_cells = "".join(
        f'<th>{labels[i]} {w}%</th>'
        for i, w in enumerate([25, 25, 25, 15, 10][:len(labels)])
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{meta.get('title', '简历筛选报告')}</title>
<style>{CSS}</style>
</head>
<body>
<div class="container">

<header class="header">
  <h1>📊 {meta.get('title', '简历筛选报告')}</h1>
  <div class="subtitle">{meta.get('subtitle', '')}</div>
  {f'<div class="run-badge">🆔 run_id: {data.get("run_id") or meta.get("run_id", "")}　|　🕐 生成时间: {data.get("generated_at") or meta.get("generated_at") or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>' if (data.get('run_id') or meta.get('run_id')) else ''}
  <div class="meta-grid">
    <div class="meta-item"><div class="label">岗位</div><div class="value">{job.get('name', '')}</div></div>
    <div class="meta-item"><div class="label">公司</div><div class="value">{job.get('company', '')}</div></div>
    <div class="meta-item"><div class="label">地点</div><div class="value">{job.get('location', '')}</div></div>
    <div class="meta-item"><div class="label">薪资</div><div class="value">{job.get('salary', '')}</div></div>
    <div class="meta-item"><div class="label">经验要求</div><div class="value">{job.get('experience_required', '')}</div></div>
    <div class="meta-item"><div class="label">学历要求</div><div class="value">{job.get('degree_required', '')}</div></div>
  </div>
</header>

<section class="section">
  <h2>🎯 岗位类型判定</h2>
  <p><strong>{type_judge.get('type', '')}</strong> —— {type_judge.get('reason', '')}</p>
  <div class="jd-summary">
    <h4>核心任职要求</h4>
    <ul>{''.join(f'<li>{r}</li>' for r in meta.get('core_requirements', []))}</ul>
  </div>
</section>

<section class="section">
  <h2>📈 筛选总览</h2>
  <div class="overview">
    <div class="stat-card green"><div class="num">{summary.get('recommend', 0)}</div><div class="label">✅ 推荐面试 (≥70)</div></div>
    <div class="stat-card yellow"><div class="num">{summary.get('pending', 0)}</div><div class="label">📌 待沟通确认 (60-69)</div></div>
    <div class="stat-card red"><div class="num">{summary.get('reject', 0)}</div><div class="label">❌ 不推荐 (&lt;60)</div></div>
    <div class="stat-card blue"><div class="num">{summary.get('total', 0)}</div><div class="label">📊 总候选人</div></div>
  </div>
</section>

<section class="section">
  <h2>🏆 候选人排名</h2>
  <table>
    <thead>
      <tr>
        <th>排名</th><th>姓名</th><th>总分</th>
        {th_cells}
        <th>建议</th>
      </tr>
    </thead>
    <tbody>{rank_rows}</tbody>
  </table>
</section>

<section class="section">
  <h2>👤 候选人详情</h2>
  {cand_cards}
</section>

<section class="section">
  <h2>🎯 行动建议</h2>
  {recommend_html}
  {pending_html}
  {reject_html}
</section>

<div class="footer">{data.get('footer', f'生成时间：{datetime.datetime.now().strftime("%Y-%m-%d %H:%M")}')}</div>
</div>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="HTML 报告生成（统一方案）")
    ap.add_argument("--input", default=None, help="score_resumes.py 输出的 screening_results.json。不传则按 orchestrator 自动定位")
    ap.add_argument("--output", default=None, help="HTML 报告输出路径。不传则按 orchestrator 自动定位")
    ap.add_argument("--job-name", default=None, help="岗位名（jobs.json metadata）")
    ap.add_argument("--encrypt-job-id", default=None,
                    help="BOSS encryptJobId（推荐；新设计目录名依此定位；亦可走 env BOSS_HR_ENCRYPT_JOB_ID）")
    ap.add_argument("--run-id", required=True, help="【必填】run_id 是数据边界。新任务先跑 boss_jd.py 创建 run；不传直接报错。")
    args = ap.parse_args()

    # 默认走 orchestrator，确保 HTML 报告落到跟前面 Step 同一 run 目录
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))
    from output_manager import JobOutputManager, resolve_encrypt_job_id
    from run_orchestrator import RunOrchestrator

    # 新设计：encrypt_job_id 必传。兼容模式仍可用，但不推荐静默回退。
    encrypt_job_id = resolve_encrypt_job_id(args.encrypt_job_id)

    job_name = args.job_name
    if not job_name:
        if args.input and not encrypt_job_id:
            # 仅兼容模式可以从 input 路径反推岗位名
            job_name = Path(args.input).parent.parent.parent.parent.name
        if not job_name:
            raise SystemExit("需要 --job-name 或 --input 指向 runs/<run_id>/process/screening_results.json")
        if not encrypt_job_id:
            # 兼容模式：用 job_name 反查 encrypt_job_id
            encrypt_job_id = resolve_encrypt_job_id() or None

    if encrypt_job_id is None:
        # 没有任何 encrypt_job_id 来源 → 报错
        raise ValueError("缺少 encrypt_job_id。\n  传 --encrypt-job-id，或设置 env BOSS_HR_ENCRYPT_JOB_ID")

    orch = RunOrchestrator(job_name, encrypt_job_id=encrypt_job_id)
    # 2026-07-30 重构：run_id 是数据边界，必须显式传（--run-id required=True）
    run_id = orch.bind_existing_run(args.run_id)
    out = JobOutputManager(job_name, encrypt_job_id=encrypt_job_id, run_id=run_id)

    if not args.input:
        args.input = str(out.screening_results_path)
        print(f'[orchestrator] --input 默认: {args.input}')
    if not args.output:
        args.output = str(out.report_path)
        print(f'[orchestrator] --output 默认: {args.output}')

    # 2026-07-30 数据边界：当前 run 缺 screening_results.json → 直接报错，
    # 绝不读其他 run / 桌面 / state/ 下的旧报告。
    if not os.path.exists(args.input):
        print(json.dumps({
            "status": "blocked",
            "exit_code": 27,
            "run_id": run_id,
            "message": (f"当前 run={run_id} 缺少评分结果 {args.input}。"
                         "HTML 报告脚本只读当前 run 的 process/，不会跨 run 或扫桌面找旧文件。"
                         "请先跑 Step 3 (score_resumes.py) 生成 screening_results.json。"),
        }, ensure_ascii=False))
        raise SystemExit(27)

    data = json.load(open(args.input, encoding="utf-8"))
    html = render(data)

    Path(args.output).write_text(html, encoding="utf-8")
    print(f"✅ HTML 报告已生成: {args.output}")
    print(f"   文件大小: {Path(args.output).stat().st_size} 字节")
    print(f"   候选人: {len(data.get('candidates', []))} 人")

    # 标记报告完成，但不 finish()——Step 5 (greet) 还在同一 run 里
    # finish() 由 step 5 结束或用户主动调用
    orch.mark_done('report', run_id=run_id)


if __name__ == "__main__":
    main()
