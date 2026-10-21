# -*- coding: utf-8 -*-
"""LLM 评分合并器（2026-07-31 新增）

设计动机：
  - prepare_scoring_inputs.py 把每份简历拆成 scoring/inputs/candidate_<geek_id>.json
  - LLM agent 逐份评分，落盘到 scoring/outputs/candidate_<geek_id>.json（每评一个立即写一份）
  - 中途崩了下次只需补未评的部分；落盘的进度是确定的、可重入的
  - 本脚本做一次**确定性合并**，把 outputs/ 拼成 _llm_scores.json（score_resumes.py 接的标准）

本脚本只读原始简历的身份字段，不做评分；负责身份校验、合并和 manifest 状态回写。
   评分标准、edu 校准、加权、tier 判定全部交给 score_resumes.py。

幂等性：
  - 重复跑会**覆盖** _llm_scores.json（不是 append），不会重复拼接
  - manifest.status 会被更新为 scored / missing

CLI：
  python collect_llm_scores.py \
    --job-name "<岗位名>" --encrypt-job-id "<id>" --run-id "<run_id>"

前置：scoring/outputs/candidate_*.json（至少要有需要合并的几份）
"""
import argparse
import io
import json
import os
import sys
import time

# ⚠️ 2026-08-03 重构：win32 的 sys.stdout reconfigure **不在模块顶层**做。
# 与 score_resumes.py / prepare_scoring_inputs.py 一致——只在 __main__ 入口 reconfigure。


# ============================================================
# 校验：单个评分 object 的最小 schema
# ============================================================
REQUIRED_SCORE_FIELDS = ("name", "dims")
REQUIRED_DIM_FIELDS = ("exp", "skill", "proj", "major")


def _validate_score(score: dict) -> list:
    """返回校验错误信息列表。空列表表示通过。

    校验内容：
      - 必须是 dict
      - 必含 name / dims
      - dims 里必含 exp/skill/proj/major 四个 0-100 数字
    """
    errs = []
    if not isinstance(score, dict):
        return [f"score 不是 dict 类型：{type(score).__name__}"]
    if not score.get("name"):
        errs.append("缺 name 字段")
    dims = score.get("dims")
    if not isinstance(dims, dict):
        errs.append("缺 dims 字段或不是 dict")
        return errs
    for k in REQUIRED_DIM_FIELDS:
        v = dims.get(k)
        if not isinstance(v, (int, float)):
            errs.append(f"dims.{k} 不是数字：{v!r}")
            continue
        if not (0 <= v <= 100):
            errs.append(f"dims.{k}={v} 超出 [0, 100]")
    return errs


# ============================================================
# CLI 入口
# ============================================================

def main():
    ap = argparse.ArgumentParser(
        description="LLM 评分合并器：把 scoring/outputs/candidate_*.json 拼成 _llm_scores.json。"
                    "幂等可重跑；不做评分、不读简历。"
    )
    ap.add_argument("--job-name", required=True, help="岗位名")
    ap.add_argument("--encrypt-job-id", default=None,
                    help="BOSS encryptJobId（推荐；亦可走 env BOSS_HR_ENCRYPT_JOB_ID）")
    ap.add_argument("--run-id", required=True, help="【必填】run_id 是数据边界")
    ap.add_argument("--scoring-dir", default=None,
                    help="scoring 根目录（不传则用 runs/<run_id>/process/scoring/）")
    ap.add_argument("--output", default=None,
                    help="合并输出文件（不传则用 runs/<run_id>/process/_llm_scores.json）")
    args = ap.parse_args()

    # 解析 encrypt_job_id
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))
    from output_manager import JobOutputManager, resolve_encrypt_job_id
    from score_identity import bind_score_identity
    encrypt_job_id = resolve_encrypt_job_id(args.encrypt_job_id, allow_fallback=False)
    out = JobOutputManager(args.job_name, encrypt_job_id=encrypt_job_id, run_id=args.run_id)

    # 定位 scoring 目录
    scoring_dir = args.scoring_dir or out.get_process_path("scoring")
    manifest_path = os.path.join(scoring_dir, "manifest.json")
    outputs_dir = os.path.join(scoring_dir, "outputs")

    if not os.path.isdir(scoring_dir):
        print(json.dumps({
            "status": "blocked",
            "exit_code": 26,
            "message": f"scoring 目录不存在：{scoring_dir}。请先跑 prepare_scoring_inputs.py。",
        }, ensure_ascii=False))
        raise SystemExit(26)

    if not os.path.exists(manifest_path):
        print(json.dumps({
            "status": "blocked",
            "exit_code": 26,
            "message": f"manifest.json 不存在：{manifest_path}。请先跑 prepare_scoring_inputs.py。",
        }, ensure_ascii=False))
        raise SystemExit(26)

    if not os.path.isdir(outputs_dir):
        print(json.dumps({
            "status": "blocked",
            "exit_code": 26,
            "message": f"outputs 目录不存在：{outputs_dir}。LLM 还没开始评分？",
        }, ensure_ascii=False))
        raise SystemExit(26)

    # 读 manifest
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    candidates = manifest.get("candidates", [])
    raw_resumes = json.load(open(out.new_resumes_path, encoding="utf-8"))
    resumes_by_gid = {
        r["_meta"]["encrypt_geek_id"]: r
        for r in raw_resumes
        if isinstance(r, dict) and (r.get("_meta") or {}).get("encrypt_geek_id")
    }

    # 收 outputs/ 下的所有 candidate_*.json（按 manifest 顺序，确保确定性）
    merged = []
    status_updated = []  # 用于回写 manifest
    missing = []
    invalid = []

    for entry in candidates:
        geek_id = entry["geek_id"]
        output_rel = entry["output_path"]  # e.g. "outputs/candidate_xxx.json"
        output_abs = os.path.join(scoring_dir, output_rel)
        # 兼容 entry 给的是绝对路径或相对路径（防御性处理）
        if not os.path.isabs(output_abs):
            output_abs = os.path.normpath(output_abs)

        if not os.path.exists(output_abs):
            missing.append({"geek_id": geek_id, "name": entry["name"], "path": output_abs})
            status_updated.append({**entry, "status": "missing"})
            continue

        try:
            score = json.load(open(output_abs, encoding="utf-8"))
        except json.JSONDecodeError as e:
            invalid.append({"geek_id": geek_id, "name": entry["name"], "error": f"JSON 解析失败：{e}"})
            status_updated.append({**entry, "status": "invalid"})
            continue

        # 校验
        errs = _validate_score(score)
        resume = resumes_by_gid.get(geek_id)
        if resume is None:
            errs.append(f"manifest 中的 geek_id={geek_id!r} 不属于当前任务的原始简历")
        elif entry["name"] != resume["name"].strip():
            errs.append("manifest 姓名与原始简历不一致，请重新准备评分输入")
        elif not errs:
            errs.extend(bind_score_identity(score, resume, encrypt_job_id))
        if errs:
            invalid.append({"geek_id": geek_id, "name": entry["name"], "errors": errs})
            status_updated.append({**entry, "status": "invalid"})
            continue

        merged.append(score)
        status_updated.append({**entry, "status": "scored"})

    # 输出 _llm_scores.json（覆盖；幂等）
    output_path = args.output or out.get_process_path("_llm_scores.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    # 回写 manifest（更新 status）
    manifest["candidates"] = status_updated
    manifest["collected_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    manifest["last_collect"] = {
        "scored_count": len(merged),
        "missing_count": len(missing),
        "invalid_count": len(invalid),
        "merged_file": output_path,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    print(json.dumps({
        "status": "success" if merged else "warning",
        "manifest": manifest_path,
        "outputs_dir": outputs_dir,
        "merged_file": output_path,
        "merged_count": len(merged),
        "missing_count": len(missing),
        "invalid_count": len(invalid),
        "missing": missing,
        "invalid": invalid,
    }, ensure_ascii=False))


if __name__ == "__main__":
    # win32 控制台中文编码保障（避免 GBK 乱码）；放 __main__ 内确保被 import 时不触发
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    main()
