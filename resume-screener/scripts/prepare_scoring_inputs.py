# -*- coding: utf-8 -*-
"""简历净化层（2026-07-31 重构 v2）

设计动机：
  - 推荐牛人批量下载后，process/new_resumes.json 动辄几 MB（30 人 × 完整简历）
  - 之前 LLM 评分是一次塞整个 JSON 让模型自己读，候选人多时既慢又容易看漏
  - 本脚本做一次**确定性净化**，把原始简历拆成「每人一文件」：
      scoring/
      ├── manifest.json           # 候选人清单（geek_id / name / input_path / status）
      ├── inputs/                 # 净化输入（LLM 读这里）
      │   └── candidate_<geek_id>.json
      ├── outputs/                # LLM 评分落盘位置（每评一个立即写一份到这里）
      │   └── candidate_<geek_id>.json
      └── _skipped.json           # 被跳过的简历

⚠️ 本脚本**只改变输入形式**，不改变评分标准。所有评分锚点（boss-hr-auto/SKILL.md）
  保持不变。LLM 仍然按 exp/skill/proj/major 四维评 0-100 分；edu 由 school_tier 查表。

新工作流（替代原来的「LLM 一次性合并到 _llm_scores.json」）：
  1. 跑本脚本 → scoring/inputs/candidate_<geek_id>.json（净化输入）
                scoring/outputs/ 自动创建空目录（LLM 落盘点）
                scoring/manifest.json（清单：geek_id / name / input_path / status）
  2. LLM agent 逐份读 scoring/inputs/candidate_<geek_id>.json，
     评 4 维度，**立即落盘**到 scoring/outputs/candidate_<geek_id>.json
     （中途崩了下次重跑只评未完成的）
  3. 跑 collect_llm_scores.py → 把 outputs/ 合并到 _llm_scores.json
  4. 跑 score_resumes.py（不变，仍只接 _llm_scores.json）

CLI：
  python prepare_scoring_inputs.py \
    --job-name "<岗位名>" --encrypt-job-id "<id>" --run-id "<run_id>"
"""
import argparse
import io
import json
import os
import re
import sys
import time

# ⚠️ 2026-08-03 重构：win32 的 sys.stdout reconfigure **不在模块顶层**做。
# 旧实现在 import 时就跑 reconfigure（只改 encoding 不替换对象，pytest 9 兼容），
# 但仍属 import 副作用；与 score_resumes.py 一致——只在 __main__ 入口 reconfigure。


# ============================================================
# 净化规则（白名单 —— 只保留这些字段，其它一律丢弃）
# ============================================================

# 顶层保留字段（白名单）
KEEP_TOP_LEVEL = {
    "name",                # 候选人姓名（评分输入必备）
    "degree",              # 最高学历（脚本做硬门槛过滤时用；净化层原样保留）
    "work_years",          # 工作年限（脚本做硬门槛过滤时用；净化层原样保留）
    "work_experience",     # 工作经历数组（含 company/position/duration/responsibility）
    "project_experience",  # 项目经历数组
    "education",           # 教育经历数组（school_tier 查表依据 + 专业匹配依据）
    "certifications",      # 证书数组
    "skills",              # 技能字符串（JD 关键词命中证据；空串也保留，方便 LLM 看到"无技能"）
}

# work_experience[] 子字段白名单
KEEP_WORK_EXP = {"company", "position", "start", "end", "duration", "responsibility"}

# project_experience[] 子字段白名单
KEEP_PROJECT_EXP = {"name", "role", "start", "end", "duration", "description", "achievement"}

# education[] 子字段白名单
KEEP_EDUCATION = {"school", "major", "degree", "start", "end"}

# _meta 里要抽到顶层 geek_id/job_id 的字段
META_GEEK_ID = "encrypt_geek_id"
META_JOB_ID = "encrypt_job_id"


# ============================================================
# 净化函数
# ============================================================

def _sanitize_work_experience(work_exp: list) -> list:
    """过滤 work_experience[]，只保留白名单字段。

    删掉：performance（历年空）、keywords（BOSS 内部空数组）、department（与评分无关）。
    保留：company / position / start / end / duration / responsibility（评分主体证据）。
    """
    out = []
    for item in work_exp or []:
        if not isinstance(item, dict):
            continue
        kept = {k: item.get(k, "") for k in KEEP_WORK_EXP}
        out.append(kept)
    return out


def _sanitize_project_experience(proj_exp: list) -> list:
    """过滤 project_experience[]。

    删掉：全空字段（避免 LLM 看到一堆 ""）。
    """
    out = []
    for item in proj_exp or []:
        if not isinstance(item, dict):
            continue
        kept = {}
        for k in KEEP_PROJECT_EXP:
            v = item.get(k, "")
            if v:  # 只保留非空
                kept[k] = v
        # 即使全空也保留 name（项目名是核心证据），其它全空则丢弃这条
        if kept.get("name") or kept.get("description") or kept.get("achievement"):
            out.append(kept)
    return out


def _sanitize_education(edu: list) -> list:
    """过滤 education[]，保留 school/major/degree/start/end。
    注意：净化层原样保留顺序；智能体按 boss-hr-auto/SKILL.md 选择评分校名。
    """
    out = []
    for item in edu or []:
        if not isinstance(item, dict):
            continue
        kept = {k: item.get(k, "") for k in KEEP_EDUCATION}
        out.append(kept)
    return out


def sanitize_resume(raw: dict, source_index: int) -> dict:
    """净化一份简历，返回精简 dict（不含 _meta 包装层）。

    顶层结构：
      {
        "__meta__": {"source": "new_resumes.json", "source_index": N, "generated_at": "..."},
        "name": str,
        "geek_id": str,        # 从 _meta.encrypt_geek_id 抽出
        "job_id": str,         # 从 _meta.encrypt_job_id 抽出
        "degree": str,
        "work_years": str,
        "work_experience": [...],
        "project_experience": [...],
        "education": [...],
        "certifications": [...],
        "skills": str
      }
    """
    meta = raw.get("_meta", {}) or {}

    sanitized = {
        "__meta__": {
            "source": "new_resumes.json",
            "source_index": source_index,
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        },
        "name": (raw.get("name") or "").strip(),
        "geek_id": meta.get(META_GEEK_ID, "") or "",
        "job_id": meta.get(META_JOB_ID, "") or "",
        "degree": raw.get("degree", "") or "",
        "work_years": raw.get("work_years", "") or "",
        "work_experience": _sanitize_work_experience(raw.get("work_experience")),
        "project_experience": _sanitize_project_experience(raw.get("project_experience")),
        "education": _sanitize_education(raw.get("education")),
        "certifications": [c for c in (raw.get("certifications") or []) if c],
        "skills": raw.get("skills", "") or "",
    }
    return sanitized


def _safe_geek_id(geek_id: str, fallback_index: int) -> str:
    """生成 ASCII 安全的 geek_id 文件名片段。

    geek_id 形如 "3c3c7a6ce2baa4f60Hx52t27E1o~"，BOSS 返回的字符基本是 base64-like。
    安全策略：
      - 非 ASCII 安全字符（路径分隔符 / 控制字符 / 不可打印）→ 替换为 _
      - 为空 → 用 index 兜底
      - 截断到 80 字符
    """
    safe = re.sub(r'[^A-Za-z0-9._~+-]', '_', geek_id).strip()
    if not safe:
        safe = f"unknown_{fallback_index:03d}"
    if len(safe) > 80:
        safe = safe[:80]
    return safe


def _candidate_filename(geek_id: str, fallback_index: int) -> str:
    """生成候选人文件名：candidate_<geek_id>.json"""
    return f"candidate_{_safe_geek_id(geek_id, fallback_index)}.json"


# ============================================================
# CLI 入口
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="简历净化层：把 new_resumes.json 拆成「每人一文件」的精简评分输入。"
                    "输出 scoring/{inputs,outputs,manifest.json}。"
                    "只改变输入形式，不改变评分标准。"
    )
    ap.add_argument("--job-name", required=True, help="岗位名（jobs.json metadata）")
    ap.add_argument("--encrypt-job-id", default=None,
                    help="BOSS encryptJobId（推荐；新设计目录名依此定位；亦可走 env BOSS_HR_ENCRYPT_JOB_ID）")
    ap.add_argument("--run-id", required=True, help="【必填】run_id 是数据边界")
    ap.add_argument("--input", default=None,
                    help="原始 new_resumes.json 路径（不传则用 orchestrator 默认 runs/<run_id>/process/new_resumes.json）")
    ap.add_argument("--output-root", default=None,
                    help="scoring 根目录（不传则用 runs/<run_id>/process/scoring/）")
    args = ap.parse_args()

    # 解析 encrypt_job_id
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))
    from output_manager import JobOutputManager, resolve_encrypt_job_id
    encrypt_job_id = resolve_encrypt_job_id(args.encrypt_job_id, allow_fallback=False)
    out = JobOutputManager(args.job_name, encrypt_job_id=encrypt_job_id, run_id=args.run_id)

    # 定位输入
    input_path = args.input or out.new_resumes_path
    if not os.path.exists(input_path):
        print(json.dumps({
            "status": "blocked",
            "exit_code": 26,
            "message": f"原始简历文件不存在：{input_path}。请先跑 Step 2 (recommend_download)。",
        }, ensure_ascii=False))
        raise SystemExit(26)

    # 定位输出根目录，并强制要求没有遗留的旧 scoring_inputs/
    output_root = args.output_root or out.get_process_path("scoring")
    inputs_dir = os.path.join(output_root, "inputs")
    outputs_dir = os.path.join(output_root, "outputs")
    os.makedirs(output_root, exist_ok=True)
    os.makedirs(inputs_dir, exist_ok=True)
    os.makedirs(outputs_dir, exist_ok=True)

    # 读原始简历
    raw_list = json.load(open(input_path, encoding="utf-8"))
    if not isinstance(raw_list, list):
        print(json.dumps({
            "status": "error",
            "message": f"原始简历必须是 JSON 数组，实际={type(raw_list).__name__}",
        }, ensure_ascii=False))
        raise SystemExit(1)

    # 净化
    manifest_entries = []
    skipped = []
    seen_geek_ids = set()  # 防止同一 geek_id 出现两次（去重）
    for idx, raw in enumerate(raw_list):
        if not isinstance(raw, dict):
            skipped.append({"source_index": idx, "reason": "非 dict 类型"})
            continue
        if raw.get("ok") is False:
            skipped.append({
                "source_index": idx,
                "name": raw.get("name", ""),
                "reason": "ok=false（下载失败/不完整）",
            })
            continue
        if not (raw.get("name") or "").strip():
            skipped.append({"source_index": idx, "reason": "缺 name"})
            continue

        sanitized = sanitize_resume(raw, idx)
        geek_id = sanitized["geek_id"]

        if not geek_id:
            skipped.append({
                "source_index": idx,
                "name": sanitized["name"],
                "reason": "缺 geek_id（_meta.encrypt_geek_id 缺失）",
            })
            continue

        if geek_id in seen_geek_ids:
            skipped.append({
                "source_index": idx,
                "name": sanitized["name"],
                "geek_id": geek_id,
                "reason": f"geek_id 与之前候选人重复",
            })
            continue
        seen_geek_ids.add(geek_id)

        filename = _candidate_filename(geek_id, idx)
        full_path = os.path.join(inputs_dir, filename)
        with open(full_path, "w", encoding="utf-8") as f:
            json.dump(sanitized, f, ensure_ascii=False, indent=2)

        manifest_entries.append({
            "geek_id": geek_id,
            "name": sanitized["name"],
            "source_index": idx,
            "input_path": os.path.relpath(full_path, output_root).replace("\\", "/"),
            "output_path": f"outputs/{filename}",
            "status": "pending",  # LLM 评完改 scored
            "size_bytes": os.path.getsize(full_path),
        })

    # 写 manifest —— 关键入口文件，智能体按这个清单逐份评分
    manifest_path = os.path.join(output_root, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_file": input_path,
            "input_dir": "inputs/",
            "output_dir": "outputs/",
            "total_candidates": len(raw_list),
            "pending_count": len(manifest_entries),
            "skipped_count": len(skipped),
            "candidates": manifest_entries,
            "skipped": skipped,
        }, f, ensure_ascii=False, indent=2)

    # 写 skipped
    skipped_path = os.path.join(output_root, "_skipped.json")
    if skipped:
        with open(skipped_path, "w", encoding="utf-8") as f:
            json.dump(skipped, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "status": "success",
        "input": input_path,
        "output_root": output_root,
        "inputs_dir": inputs_dir,
        "outputs_dir": outputs_dir,
        "manifest": manifest_path,
        "total": len(raw_list),
        "sanitized": len(manifest_entries),
        "skipped": len(skipped),
        "first_5": [
            {"geek_id": e["geek_id"], "name": e["name"], "input": e["input_path"]}
            for e in manifest_entries[:5]
        ],
    }, ensure_ascii=False))


if __name__ == "__main__":
    # win32 控制台中文编码保障（避免 GBK 乱码）；放 __main__ 内确保被 import 时不触发
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
