# -*- coding: utf-8 -*-
"""简历评分统一方案（LLM 主导 + school_tier 学历分档校准）

设计核心：
  - LLM 主导：4 维度（exp/skill/proj/major）由 LLM 真实分析完整简历
  - 脚本辅助：1 维度（edu/学校档次）由 school_tier 查表
  - 公式：5 维度 weighted 求和 = total
  - 通用：不限岗位
  - 可复现：公式 + 权重 + tier 阈值明确

工作流：
  1. LLM agent 读 test_resumes.json + JD
  2. LLM 真实分析每份简历，评 4 维度（exp/skill/proj/major）
  3. 对每份简历，agent 调 validate_score(score) 收尾
     - 自动用 school_tier 查 edu
     - 自动重算 weighted + total
     - 自动判定 tier
  4. agent 写 _llm_scores.json（4 维度 + signals + gaps + advice）
  5. python score_resumes.py --input _llm_scores.json --output screening_results.json
  6. python generate_html_report.py --input screening_results.json --output report.html

输入 schema（agent 提供，4 维度）：
  [
    {
      "name": str,
      "school_name": str,         # 纯校名（给 school_tier）
      "school": str,              # 报告展示用
      "work_years": str,
      "match_type": str,
      "dims": {
        "exp": 0-100,             # LLM 评
        "skill": 0-100,           # LLM 评
        "proj": 0-100,            # LLM 评
        "major": 0-100            # LLM 评
        # edu 会被自动覆盖
      },
      "signals": [...],            # 兼容旧 schema
      "gaps": [...],               # 兼容旧 schema
      "highlights": [...],         # 新 schema
      "concerns": [...],           # 新 schema
      "advice": str
    }
  ]

输出 schema（screening_results.json）：
  {
    "job_name": str,
    "meta": {
      "title": str, "subtitle": str,
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
        "highlights": [...],
        "concerns": [...]
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
import time
import argparse
from pathlib import Path

# ⚠️ 2026-08-03 重构：win32 的 sys.stdout reconfigure **不在模块顶层**做。
# 原因：旧实现 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)` 会替换
# sys.stdout 对象，破坏 pytest capture 的 tmpfile（pytest 退出时抛
# "ValueError: I/O operation on closed file"）。
# 改为只在 __main__ 入口里 reconfigure；被 import 时不触发副作用。
# 与 prepare_scoring_inputs.py / collect_llm_scores.py 保持一致。

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from school_tier import lookup as school_lookup

# === 5 维度权重（2026-07-21 调整：工作经验 30%→25%，专业匹配 5%→10%）===
WEIGHTS = {"edu": 0.25, "exp": 0.25, "skill": 0.25, "proj": 0.15, "major": 0.10}
WEIGHTS_PCT = {"edu": 25, "exp": 25, "skill": 25, "proj": 15, "major": 10}

# === 维度中文标签（顺序与 WEIGHTS 一致，供报告展示）===
DIM_LABELS = ["学历", "工作经验", "技能", "项目", "专业"]

# === Tier 阈值（SKILL.md 硬性规定）===
TIER_THRESHOLDS = {"推荐": 70, "待定": 60, "不推荐": 0}


# ============================================================
# 核心工具函数（agent 直接 import 调用）
# ============================================================

def calc_tier(total: float) -> str:
    """根据 total 判定 tier"""
    if total >= TIER_THRESHOLDS["推荐"]:
        return "推荐"
    if total >= TIER_THRESHOLDS["待定"]:
        return "待定"
    return "不推荐"


def calc_weighted(dims: dict) -> dict:
    """5 维度 weighted 计算（按 SKILL.md 公式）"""
    return {k: round(dims[k] * WEIGHTS[k], 2) for k in WEIGHTS}


def calc_total(weighted: dict) -> float:
    """5 维度 weighted 求和 = total"""
    return round(sum(weighted.values()), 1)


def _extract_school_name(score: dict) -> str:
    """从 score 智能提取纯校名（不依赖 LLM 守规矩）

    优先级：
    1. school_name 字段（如果非空）
    2. school 字段按常见分隔符拆分（取第一段）

    支持格式：
      - "辽宁工业大学"                       → "辽宁工业大学"
      - "江西理工大学/人工智能/本科"           → "江西理工大学"
      - "辽宁工业大学 · 车辆工程 · 本科"      → "辽宁工业大学"
      - "辽宁工业大学（车辆工程）"            → "辽宁工业大学"
      - "辽宁工业大学(车辆工程)"             → "辽宁工业大学"
    """
    school = score.get("school_name", "").strip()
    if school:
        return school
    school = score.get("school", "").strip()
    if not school:
        return ""
    # 鲁棒拆分：遇到 / · （ ( 中任一个就停
    import re
    m = re.match(r"^([^/·（(]+)", school)
    return m.group(1).strip() if m else school


def validate_score(score: dict) -> dict:
    """LLM 评分收尾（统一方案的核心）

    1. 用 school_tier 强制校准 edu（无论 LLM 是否评了 edu）
       - 自动从 school_name 或 school 提取纯校名（不依赖 LLM 拆字段）
    2. 重算 weighted + total（按公式）
    3. 判定 tier

    输入：5 维度（edu 可被忽略）或 4 维度的 LLM 评分
    输出：5 维度评分 + tier（edu 来自 school_tier）
    """
    school = _extract_school_name(score)
    if school:
        info = school_lookup(school)
        if info["score"] is not None:
            score["dims"]["edu"] = info["score"]
            score["dims_edu_reason"] = f"{info['tier']}（school_tier 查询：{school}）"
        else:
            score["dims"]["edu"] = 60
            score["dims_edu_reason"] = f"缺失（{school} 不在学校表，按60计）"
    else:
        score["dims"]["edu"] = 60
        score["dims_edu_reason"] = "缺失（无学校名，按60计）"

    score["weighted"] = calc_weighted(score["dims"])
    score["total"] = calc_total(score["weighted"])
    score["tier"] = calc_tier(score["total"])
    return score


# ============================================================
# Schema 转换
# ============================================================

def candidate_to_report(c: dict, rank: int) -> dict:
    """把 1 份评分（list 形式）转成报告 candidates[] 形式"""
    dims = c["dims"]
    weighted = c["weighted"]
    return {
        "rank": rank,
        "name": c["name"],
        "geek_id": c.get("geek_id", ""),
        "job_id": c.get("job_id", ""),
        "tier": c["tier"],
        "total": c["total"],
        "hard_pass": c.get("hard_pass", True),
        "hard_reason": c.get("hard_reason"),
        "school": c.get("school", c.get("school_name", "")),
        "work_years": c.get("work_years", ""),
        "current_role": c.get("match_type", ""),
        "dimensions": [
            {
                "pct": dims[k],
                "weighted": weighted[k],
                "weight": WEIGHTS_PCT[k],
                "reason": c.get(f"dims_{k}_reason", "")
            }
            for k in ["edu", "exp", "skill", "proj", "major"]
        ],
        "highlights": c.get("highlights", c.get("signals", [])),
        "concerns": c.get("concerns", c.get("gaps", [])),
        "advice": c.get("advice", ""),
    }


def build_actions(candidates: list) -> dict:
    """从 candidates 生成 actions 三段式"""
    recommend, pending, reject = [], [], []
    for c in candidates:
        item = {"name": c["name"], "score": c["total"]}
        signals = c.get("highlights", c.get("signals", []))
        gaps = c.get("concerns", c.get("gaps", []))
        if c["tier"] == "推荐":
            item["background"] = "、".join(signals[:3]) if signals else "—"
            item["action"] = c.get("advice", "")
            recommend.append(item)
        elif c["tier"] == "待定":
            item["strengths"] = "、".join(signals[:3]) if signals else "—"
            item["action"] = c.get("advice", "")
            pending.append(item)
        else:
            item["concerns"] = "、".join(gaps[:2]) if gaps else "—"
            reject.append(item)
    return {"recommend": recommend, "pending": pending, "reject": reject}


def build_meta(job_name: str, job_info: dict = None) -> dict:
    """构造报告 meta（agent 可重写）"""
    job = job_info or {}
    return {
        "title": f"{job_name} · 简历筛选报告",
        "subtitle": "LLM 主导评分 + school_tier 学历分档校准",
        "job": {
            "name": job_name,
            "company": job.get("company", ""),
            "location": job.get("location", ""),
            "salary": job.get("salary", ""),
            "experience_required": job.get("experience_required", ""),
            "degree_required": job.get("degree_required", ""),
        },
        "type_judgment": job.get("type_judgment", {
            "type": "技术岗",
            "reason": "JD 明确要求结构设计 + 仿真 + 工艺核心技术能力"
        }),
        "core_requirements": job.get("core_requirements", []),
    }


# ============================================================
# CLI 入口
# ============================================================

def main():
    ap = argparse.ArgumentParser(description="简历评分统一方案（LLM 4 维度 + school_tier 校准）")
    ap.add_argument("--input", default=None, help="LLM 评分 JSON（4 维度）。不传则按 orchestrator 的 run_id 自动定位 runs/<run_id>/process/_llm_scores.json")
    ap.add_argument("--output", default=None, help="标准化后的 screening_results.json。不传则按 orchestrator 自动定位到 run_dir")
    ap.add_argument("--job-name", default="工程师", help="岗位名（jobs.json metadata）")
    ap.add_argument("--encrypt-job-id", default=None,
                    help="BOSS encryptJobId（推荐；新设计目录名依此定位；亦可走 env BOSS_HR_ENCRYPT_JOB_ID）")
    ap.add_argument("--job-info", default=None, help="JD 完整信息 JSON 字符串（可选）")
    ap.add_argument("--run-id", required=True, help="【必填】run_id 是数据边界。新任务先跑 boss_jd.py 创建 run；不传直接报错。")
    ap.add_argument("--rescore", action="store_true",
                    help="重评：不跳过历史已评分候选人（换 JD / 修评分口径时用）")
    args = ap.parse_args()

    # 默认走 orchestrator，确保落到跟前面 Step 同一 run 目录
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))
    from output_manager import OUTPUT_ROOT, JobOutputManager, resolve_encrypt_job_id
    from run_orchestrator import RunOrchestrator
    from job_resume_store import JobResumeStore
    from score_identity import bind_score_identity
    encrypt_job_id = resolve_encrypt_job_id(args.encrypt_job_id)
    if not encrypt_job_id:
        raise ValueError("缺少 encrypt_job_id。\n  传 --encrypt-job-id，或设置 env BOSS_HR_ENCRYPT_JOB_ID")
    orch = RunOrchestrator(args.job_name, encrypt_job_id=encrypt_job_id)
    # 2026-07-30 重构：run_id 是数据边界，必须显式传（--run-id required=True）
    run_id = orch.bind_existing_run(args.run_id)
    out = JobOutputManager(args.job_name, encrypt_job_id=encrypt_job_id, run_id=run_id)
    store = JobResumeStore(args.job_name, encrypt_job_id=encrypt_job_id)

    # input/output 没传 → 用 orchestrator 推断的 run_dir
    if not args.input:
        args.input = out.get_process_path('_llm_scores.json')
        print(f'[orchestrator] --input 默认: {args.input}')
    if not args.output:
        args.output = out.screening_results_path
        print(f'[orchestrator] --output 默认: {args.output}')

    # 2026-07-30 数据边界：当前 run 缺 _llm_scores.json → 直接报错，绝不跨 run 找
    if not os.path.exists(args.input):
        print(json.dumps({
            "status": "blocked",
            "exit_code": 26,
            "run_id": run_id,
            "message": (f"当前 run={run_id} 缺少简历评分输入 {args.input}。"
                         "评分脚本只读当前 run 的 process/，不会跨 run 或扫桌面找旧文件。"
                         "请先跑 Step 2 (recommend_list + recommend_download) 拉取简历，"
                         "再让 LLM 生成 _llm_scores.json。"),
        }, ensure_ascii=False))
        raise SystemExit(26)

    data = json.load(open(args.input, encoding="utf-8"))
    if not isinstance(data, list):
        print("错误：输入 JSON 必须是候选人数组")
        return

    # 姓名 → geek_id 反查表（_llm_scores.json 只有姓名时兜底用）
    # 重要（2026-07-30 数据边界）：只从当前 run 的 process/ 读简历，绝不读
    # state/resumes_master.json（那是跨 run 累计文件）。
    name_to_gids = {}
    resumes_by_gid = {}
    run_resume_paths = []
    for p in (out.new_resumes_path,
              out.get_process_path('batch_1_resumes.json'),
              out.get_process_path('batch_2_resumes.json'),
              out.get_process_path('batch_3_resumes.json')):
        run_resume_paths.append(p)
    try:
        import glob as _glob
        for p in _glob.glob(out.get_process_path('*_resumes.json')):
            if p not in run_resume_paths:
                run_resume_paths.append(p)
    except Exception:
        pass
    for path in run_resume_paths:
        if not os.path.exists(path):
            continue
        try:
            arr = json.load(open(path, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(arr, list):
            continue
        for r in arr:
            if not isinstance(r, dict):
                continue
            _n = (r.get("name") or "").strip()
            _gid = (r.get("_meta", {}) or {}).get("encrypt_geek_id", "") or ""
            if _n and _gid:
                gids = name_to_gids.setdefault(_n, [])
                if _gid not in gids:
                    gids.append(_gid)
                resumes_by_gid[_gid] = r

    # 先绑定身份，再去重。--rescore 仅控制重评，不绕过身份校验。
    invalid = []
    for c in data:
        gid = (c.get("geek_id") or "").strip()
        if not gid:
            gids = name_to_gids.get((c.get("name") or "").strip(), [])
            if len(gids) != 1:
                invalid.append({"name": c.get("name"),
                                "errors": ["姓名无法唯一对应当前任务简历，请填写 geek_id"]})
                continue
            gid = gids[0]
        resume = resumes_by_gid.get(gid)
        errors = (bind_score_identity(c, resume, encrypt_job_id) if resume else
                  [f"geek_id={gid!r} 不属于当前任务的原始简历"])
        if errors:
            invalid.append({"name": c.get("name"), "errors": errors})
    if invalid:
        print(json.dumps({"status": "blocked", "exit_code": 26,
                          "message": "评分身份不一致，请修正评分输入后重试",
                          "invalid": invalid}, ensure_ascii=False))
        raise SystemExit(26)

    # 已校验的稳定 ID 直接用于去重及状态回写，不再通过姓名重新分配。
    if not args.rescore:
        _kept, _skipped = [], []
        for c in data:
            if store.is_scored(store.encrypt_job_id, c["geek_id"]):
                _skipped.append(c.get("name", ""))
            else:
                _kept.append(c)
        if _skipped:
            print(f"⏭ 跳过 {len(_skipped)} 位历史已评分候选人：{'、'.join(_skipped[:8])}"
                  + ("..." if len(_skipped) > 8 else ""))
            print("   （如需重评请加 --rescore）")
        data = _kept
        if not data:
            print("本轮无新候选人可评分，退出。")
            return

    # 对每份评分收尾（强制用 school_tier 校准 edu + 重算 total + 判定 tier）
    for c in data:
        validate_score(c)

    # 按 total 倒序
    data.sort(key=lambda x: -x["total"])

    # 统计
    summary = {
        "total": len(data),
        "recommend": sum(1 for c in data if c["tier"] == "推荐"),
        "pending": sum(1 for c in data if c["tier"] == "待定"),
        "reject": sum(1 for c in data if c["tier"] == "不推荐"),
    }

    # 转 candidates 格式
    candidates = [candidate_to_report(c, i + 1) for i, c in enumerate(data)]
    actions = build_actions(candidates)

    # 解析 job_info（可选）
    job_info = json.loads(args.job_info) if args.job_info else {}

    # 组装 screening_results.json
    meta = build_meta(args.job_name, job_info)
    generated_at = time.strftime("%Y-%m-%d %H:%M:%S")
    if args.run_id:
        meta["run_id"] = args.run_id
        meta["generated_at"] = generated_at

    output = {
        "job_name": args.job_name,
        "run_id": args.run_id,
        "generated_at": generated_at,
        "meta": meta,
        "summary": summary,
        "dimension_labels": DIM_LABELS,
        "candidates": candidates,
        "actions": actions,
    }

    Path(args.output).write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"✓ 处理 {len(data)} 份评分")
    print(f"  推荐 {summary['recommend']} / 待定 {summary['pending']} / 不推荐 {summary['reject']}")
    print(f"  输出: {args.output}")

    # 回写评分状态（下次 run 自动跳过这批人）。
    _marked = 0
    for c in data:
        store.mark_scored(store.encrypt_job_id, c["geek_id"],
                          name=c.get("name", ""), total=c.get("total"),
                          tier=c.get("tier", ""), run_id=run_id)
        _marked += 1
    if _marked:
        print(f"  已记录 {_marked} 人评分状态 → state/scored_state.json")

    orch.mark_done('score', run_id=run_id)  # 标记 score 步骤完成


if __name__ == "__main__":
    # win32 控制台中文编码保障（避免 GBK 乱码）；放 __main__ 内确保被 import 时不触发
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    main()
