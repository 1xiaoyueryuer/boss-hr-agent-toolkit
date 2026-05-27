#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Synchronize online resumes for candidates who applied to BOSS/Zhipin recruiter jobs.

Usage:
    python sync_boss_resumes.py refresh-jobs           # Refresh online job index
    python sync_boss_resumes.py sync-all               # Sync resumes for all online jobs
    python sync_boss_resumes.py sync-job --job-id <id> # Sync one job
    python sync_boss_resumes.py sync-job --job-id <id> --dry-run  # List only, no download

Defaults:
    --root  %USERPROFILE%/WorkBuddy/boss-resumes
    --cdp   http://localhost:9222

Fixed bugs:
    [Bug #1] GBK encoding crash: removed text=True, use PYTHONIOENCODING=utf-8 + raw bytes
    [Bug #2] Pagination dead loop: removed --page (API doesn't actually paginate)
    [Bug #3] Wrong data structure in _write_resume_md: dropped --raw, use clean non-raw format
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_ROOT = Path.home() / "WorkBuddy" / "boss-resumes"
CDP_URL = "http://localhost:9222"
PLATFORM = "zhipin"
ROLE = "recruiter"

RANDOM_DELAY_MIN = 3
RANDOM_DELAY_MAX = 6

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def eprint(*args: Any, **kwargs: Any) -> None:
    """Print to stderr (for --verbose messages)."""
    print(*args, file=sys.stderr, **kwargs)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_filename(s: str, max_len: int = 50) -> str:
    """Sanitize a string for use as a directory/filename."""
    s = re.sub(r'[<>:"/\\|?*]', "_", s)
    s = s.strip(". ")
    if len(s) > max_len:
        s = s[:max_len]
    return s if s else "unnamed"


def boss_cmd(args: list[str], verbose: bool = False) -> tuple[dict[str, Any], str]:
    """
    Run a boss CLI command and return (parsed_json, raw_output).

    Fixes Bug #1 (GBK encoding crash on Chinese Windows):
      - Inject PYTHONIOENCODING=utf-8 so the CLI subprocess outputs UTF-8
      - Use raw bytes (not text=True) to avoid Python's internal GBK reader thread
      - Decode stdout with utf-8 explicitly
    """
    cmd = [
        "boss",
        "--role", ROLE,
        "--platform", PLATFORM,
        "--cdp-url", CDP_URL,
    ] + args
    if verbose:
        eprint("$", " ".join(cmd))
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(cmd, capture_output=True, timeout=30, env=env)
    try:
        raw = result.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        return {}, ""
    if not raw:
        return {}, ""
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError:
        return {}, raw


def load_json(path: Path) -> dict[str, Any]:
    if path.exists():
        with open(path, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    return {}


def save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


# ---------------------------------------------------------------------------
# friend_detail fallback (resolve securityId from numeric friendId)
# ---------------------------------------------------------------------------

def _try_friend_detail(friend_ids: list[int], verbose: bool = False) -> dict[int, dict[str, Any]]:
    """
    Call boss-agent-cli internal friend_detail API to obtain securityId
    by numeric friendId. Returns dict mapping friendId -> info dict.
    """
    try:
        from boss_agent_cli.auth.manager import AuthManager
        from boss_agent_cli.api.recruiter_client import BossRecruiterClient
    except ImportError as exc:
        if verbose:
            eprint(f"[friend_detail] boss_agent_cli import failed ({exc!r}); "
                   f"securityId fallback unavailable.")
        return {}

    data_dir = Path.home() / ".boss-agent"
    auth = AuthManager(data_dir)
    client = BossRecruiterClient(auth)

    try:
        response = client.friend_detail(friend_ids)
    except Exception as exc:
        if verbose:
            eprint(f"[friend_detail] API call failed: {exc}")
        return {}

    zp = (response or {}).get("zpData") or {}
    friend_list = zp.get("friendList") or []
    result: dict[int, dict[str, Any]] = {}
    for entry in friend_list:
        fid = entry.get("uid") or entry.get("friendId")
        if fid:
            result[fid] = entry
    return result


# ---------------------------------------------------------------------------
# Job index
# ---------------------------------------------------------------------------

def _refresh_jobs(root: Path, verbose: bool) -> dict[str, Any]:
    """Fetch online jobs and update job_index.json."""
    resp, raw = boss_cmd(["hr", "jobs", "list"], verbose=verbose)
    # boss hr jobs list returns data as list directly
    result_data = resp.get("data") or []
    if isinstance(result_data, dict):
        raw_jobs = result_data.get("result") or result_data.get("jobs") or []
    else:
        raw_jobs = result_data

    online_jobs = []
    for j in raw_jobs:
        if not isinstance(j, dict):
            continue
        status = j.get("jobOnlineStatus", -1)
        if status != 1:
            continue
        online_jobs.append(j)

    job_index_path = root / "job_index.json"
    old_index = load_json(job_index_path)
    old_jobs_map = {}
    old_jobs_raw = old_index.get("jobs", [])
    if isinstance(old_jobs_raw, dict):
        # Old format: jobs dict keyed by id
        old_jobs_map = old_jobs_raw
    elif isinstance(old_jobs_raw, list):
        for j in old_jobs_raw:
            k = j.get("encryptJobId") or j.get("id") or ""
            old_jobs_map[k] = j

    jobs_out = []
    for j in online_jobs:
        entry = {
            "id": j.get("jobId") or j.get("id", 0),
            "encryptJobId": j.get("encryptJobId", ""),
            "name": j.get("jobName", ""),
            "salary": j.get("salaryDesc", ""),
            "location": j.get("address", ""),
            "status": "online",
            "updated_at": now_iso(),
        }
        # Preserve old job data if available
        old = old_jobs_map.get(entry["encryptJobId"]) or old_jobs_map.get(str(entry["id"]))
        if old:
            entry.setdefault("created_at", old.get("created_at", now_iso()))
            entry.setdefault("total_candidates", old.get("total_candidates", 0))
        jobs_out.append(entry)

    job_index = {
        "root": str(root),
        "updated_at": now_iso(),
        "jobs": jobs_out,
    }
    save_json(job_index_path, job_index)

    if verbose:
        eprint(f"[refresh-jobs] {len(online_jobs)} online jobs saved to {job_index_path}")

    return job_index


# ---------------------------------------------------------------------------
# Candidate resolution and resume download
# ---------------------------------------------------------------------------

def _list_applications(encrypt_job_id: str, verbose: bool) -> list[dict[str, Any]]:
    """
    Fetch all candidates for a job.

    Fixes Bug #2: The CLI --page parameter doesn't actually paginate the BOSS API;
    every page returns the full list. So we call once without --page and that's it.
    """
    args = ["hr", "applications", "--job-id", encrypt_job_id]
    resp, raw = boss_cmd(args, verbose=verbose)
    return (resp.get("data") or {}).get("result") or []


def _resolve_candidate(
    friend_id: int,
    encrypt_friend_id: str,
    encrypt_job_id: str,
    friend_detail_cache: dict[int, dict[str, Any]],
    verbose: bool,
) -> dict[str, Any]:
    """Resolve candidate parameters (securityId, encryptGeekId, name)."""
    # Try friend_detail cache first
    fd = friend_detail_cache.get(friend_id, {})
    security_id = fd.get("securityId", "")
    encrypt_geek_id = fd.get("encryptUid", encrypt_friend_id)
    name = fd.get("name", "")
    resolved_encrypt_job_id = fd.get("encryptJobId", encrypt_job_id)

    if not security_id and verbose:
        eprint(f"  [warn] friendId={friend_id}: securityId not resolved via friend_detail")

    return {
        "friendId": friend_id,
        "encryptFriendId": encrypt_friend_id,
        "name": name,
        "securityId": security_id,
        "encryptGeekId": encrypt_geek_id,
        "encryptJobId": resolved_encrypt_job_id,
    }


def _download_resume(
    candidate: dict[str, Any],
    resume_dir: Path,
    force: bool,
    dry_run: bool,
    verbose: bool,
) -> str:
    """Download one resume. Returns status string."""
    geek_id = candidate["encryptGeekId"]
    job_id = candidate["encryptJobId"]
    sec_id = candidate["securityId"]

    if not sec_id or not geek_id:
        return "pending_security_id"

    if not dry_run:
        # Fixes Bug #3: dropped --raw flag. Non-raw format returns clean structured data
        # with predictable field names (basic, education, work_experience, etc.)
        args = ["hr", "resume", geek_id, "--job-id", job_id, "--security-id", sec_id]
        resp, raw = boss_cmd(args, verbose=verbose)
        if not raw:
            return "failed"

        save_json(resume_dir / "raw_response.json", json.loads(raw) if raw else {})
        save_json(resume_dir / "resume.json", resp)

        # Generate resume.md
        _write_resume_md(resp, resume_dir / "resume.md")

    return "downloaded"


def _write_resume_md(raw_response: dict[str, Any], md_path: Path) -> None:
    """
    Write a human-readable Markdown resume from the CLI response.

    Fixes Bug #3: maps the actual non-raw CLI response structure.
    Non-raw structure:
      data.basic       -> {name, gender:"男"/"女", age, degree, work_years, active_status}
      data.education   -> [{school, major, degree, start, end}]
      data.work_experience -> [{company, position, department, start, end, duration, responsibility, keywords}]
      data.project_experience -> [...]
      data.certifications    -> [str, str, ...]
      data.expectation       -> {position, salary, location, ...}
    """
    data = raw_response.get("data", {}) or {}
    basic = data.get("basic", {}) or {}

    lines = [
        "# 候选人简历\n",
        f"**姓名：** {basic.get('name', '')}",
        f"**性别：** {basic.get('gender', '')}",
        f"**年龄：** {basic.get('age', '')}",
        f"**学历：** {basic.get('degree', '')}",
        f"**工作年限：** {basic.get('work_years', '')}",
        f"**状态：** {basic.get('active_status', '')}",
        "",
    ]

    # Education
    edu_list = data.get("education") or []
    if edu_list:
        lines.append("## 教育经历\n")
        for edu in edu_list:
            school = edu.get("school", "")
            major = edu.get("major", "")
            deg = edu.get("degree", "")
            sd = edu.get("start", "")
            ed = edu.get("end", "")
            parts = " | ".join(filter(None, [school, major, deg]))
            date_range = f" ({sd}-{ed})" if sd or ed else ""
            lines.append(f"- **{school}**{date_range}")
            lines.append(f"  - 专业：{major}" if major else "")
            lines.append(f"  - 学历：{deg}" if deg else "")
        lines.append("")

    # Work experience
    work_list = data.get("work_experience") or []
    if work_list:
        lines.append("## 工作经历\n")
        for we in work_list:
            pos = we.get("position", "")
            comp = we.get("company", "")
            sm = we.get("start", "")
            em = we.get("end", "")
            dep = we.get("department", "")
            dur = we.get("duration", "")
            header = f"### {pos} @ {comp} ({sm} - {em})" if sm else f"### {pos} @ {comp}"
            lines.append(header)
            detail_parts = []
            if dep:
                detail_parts.append(f"部门：{dep}")
            if dur:
                detail_parts.append(f"时长：{dur}")
            if detail_parts:
                lines.append(" | ".join(detail_parts))
            lines.append("")
            resp_text = we.get("responsibility", "")
            if resp_text:
                for r in resp_text.split("\\n"):
                    r = r.strip()
                    if r:
                        # Some items start with "1." style numbering
                        lines.append(f"- {r}" if not r.startswith("- ") else r)
            lines.append("")

    # Project experience
    proj_list = data.get("project_experience") or []
    if proj_list:
        lines.append("## 项目经历\n")
        for proj in proj_list:
            pname = proj.get("project_name", proj.get("name", ""))
            prole = proj.get("role", "")
            pdesc = proj.get("description", "")
            pstart = proj.get("start", "")
            pend = proj.get("end", "")
            header = f"### {pname}"
            if prole:
                header += f" ({prole})"
            lines.append(header)
            if pstart or pend:
                lines.append(f"**时间：** {pstart}-{pend}" if pstart else f"**时间：** {pend}")
            if pdesc:
                for r in pdesc.split("\\n"):
                    r = r.strip()
                    if r:
                        lines.append(f"- {r}")
            lines.append("")

    # Skills (may not be present in non-raw, skip gracefully)
    # Certifications
    certs = data.get("certifications") or []
    if certs:
        lines.append("## 证书\n")
        for c in certs:
            if isinstance(c, str):
                cn = c
            elif isinstance(c, dict):
                cn = c.get("certName", c.get("name", ""))
            else:
                cn = str(c)
            if cn:
                lines.append(f"- {cn}")
        lines.append("")

    # Expected position
    expect = data.get("expectation") or {}
    if expect:
        lines.append("## 求职意向\n")
        lines.append(f"- **期望职位：** {expect.get('position', '')}")
        lines.append(f"- **期望地点：** {expect.get('location', '')}")
        lines.append(f"- **期望薪资：** {expect.get('salary', '')}")

    text = "\n".join(lines)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------------------
# Sync one job
# ---------------------------------------------------------------------------

def _sync_job(job_entry: dict[str, Any], root: Path, force: bool,
              dry_run: bool, verbose: bool, max_candidates: int = 0) -> dict[str, Any]:
    """Sync resumes for one job. Returns totals dict."""
    encrypt_job_id = job_entry.get("encryptJobId", "")
    job_id = job_entry.get("id", 0)
    job_name = job_entry.get("name", "")

    safe_name = safe_filename(job_name)
    job_dir = root / "jobs" / f"{job_id}_{safe_name}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Load or create candidate index
    ci_path = job_dir / "candidate_index.json"
    ci = load_json(ci_path)
    candidates_map = ci.get("candidates") or {}
    if isinstance(candidates_map, list):
        # Convert old format if needed
        cm = {}
        for c in candidates_map:
            cm[str(c.get("friendId", c.get("id", "")))] = c
        candidates_map = cm

    # Fetch applications (single call, no pagination loop - Fix #2)
    apps = _list_applications(encrypt_job_id, verbose)
    if verbose:
        eprint(f"  {len(apps)} applicants found")

    # Collect friend IDs for friend_detail mass resolution
    new_friend_ids = []
    for app in apps:
        fid = app.get("friendId", 0)
        key = str(fid)
        existing = candidates_map.get(key, {})
        status = existing.get("status", "")
        if status == "downloaded" and not force:
            continue
        new_friend_ids.append(fid)

    # Resolve securityId in batch
    fd_cache = _try_friend_detail(new_friend_ids, verbose=verbose) if new_friend_ids else {}

    # Process each candidate
    totals = {
        "candidates_discovered": len(apps),
        "downloaded": 0,
        "skipped_existing": 0,
        "pending_security_id": 0,
        "failed": 0,
    }

    processed = 0
    for app in apps:
        if 0 < max_candidates <= processed:
            if verbose:
                eprint(f"  --max={max_candidates} reached, stopping")
            break
        processed += 1
        fid = app.get("friendId", 0)
        encrypt_fid = app.get("encryptFriendId", "")
        last_update = app.get("updateTime", 0)
        key = str(fid)

        existing = candidates_map.get(key, {})
        status = existing.get("status", "")

        if status == "downloaded" and not force:
            totals["skipped_existing"] += 1
            continue

        candidate = _resolve_candidate(
            fid, encrypt_fid, encrypt_job_id, fd_cache, verbose,
        )

        # Add name from applications if friend_detail didn't provide it
        if not candidate["name"]:
            candidate["name"] = app.get("name", "")

        safe_cand_name = safe_filename(candidate["name"] or str(fid))
        resume_dir = job_dir / "resumes" / f"{safe_cand_name}_{fid}"
        entry = {
            "friendId": fid,
            "encryptFriendId": encrypt_fid,
            "name": candidate["name"],
            "lastUpdate": last_update,
            "updated_at": now_iso(),
        }

        if not candidate["securityId"] or not candidate["encryptGeekId"]:
            entry["status"] = "pending_security_id"
            entry["reason"] = "securityId not found"
            candidates_map[key] = entry
            totals["pending_security_id"] += 1
            continue

        if dry_run:
            entry["status"] = "would_download"
            candidates_map[key] = entry
            totals["downloaded"] += 1
            continue

        # Download
        dl_status = _download_resume(candidate, resume_dir, force, dry_run, verbose)

        entry["status"] = dl_status
        if dl_status == "failed":
            entry["reason"] = "download API returned empty"
            totals["failed"] += 1
        elif dl_status == "downloaded":
            totals["downloaded"] += 1
        elif dl_status == "pending_security_id":
            totals["pending_security_id"] += 1
            entry["reason"] = "securityId empty after resolution"

        candidates_map[key] = entry

        # Random delay after each successful download
        if dl_status == "downloaded":
            delay = random.uniform(RANDOM_DELAY_MIN, RANDOM_DELAY_MAX)
            if verbose:
                eprint(f"  delay {delay:.1f}s")
            time.sleep(delay)

    # Save candidate index
    ci["updated_at"] = now_iso()
    ci["candidates"] = candidates_map
    save_json(ci_path, ci)

    # Save job metadata
    job_meta = {**job_entry, "updated_at": now_iso(), "candidate_count": len(apps)}
    save_json(job_dir / "job.json", job_meta)

    return totals


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------

def cmd_refresh_jobs(args: argparse.Namespace) -> None:
    root = Path(args.root)
    index = _refresh_jobs(root, args.verbose)
    print(json.dumps({
        "ok": True,
        "resume_root": str(root),
        "mode": "refresh-jobs",
        "jobs_discovered": len(index.get("jobs", [])),
    }, ensure_ascii=False, indent=2))


def cmd_sync_all(args: argparse.Namespace) -> None:
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    # Refresh job index first
    index = _refresh_jobs(root, args.verbose)
    jobs = index.get("jobs", [])

    if not jobs:
        print(json.dumps({
            "ok": True,
            "resume_root": str(root),
            "mode": "sync-all",
            "totals": {
                "jobs": 0,
                "candidates_discovered": 0,
                "downloaded": 0,
                "skipped_existing": 0,
                "pending_security_id": 0,
                "failed": 0,
            },
        }, ensure_ascii=False, indent=2))
        return

    grand = {
        "candidates_discovered": 0,
        "downloaded": 0,
        "skipped_existing": 0,
        "pending_security_id": 0,
        "failed": 0,
    }

    for job in jobs:
        if args.verbose:
            eprint(f"\n--- Job: {job.get('name', '')} (id={job.get('id', '')}) ---")
        t = _sync_job(job, root, args.force, args.dry_run, args.verbose, args.max)
        for k in grand:
            grand[k] += t.get(k, 0)

    print(json.dumps({
        "ok": True,
        "resume_root": str(root),
        "mode": "sync-all",
        "dry_run": args.dry_run,
        "totals": grand,
    }, ensure_ascii=False, indent=2))


def cmd_sync_job(args: argparse.Namespace) -> None:
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    job_id = args.job_id

    # Find the job in index (or fetch fresh)
    index = _refresh_jobs(root, args.verbose)
    jobs = index.get("jobs", [])

    match = None
    for j in jobs:
        if j.get("encryptJobId") == job_id or str(j.get("id", "")) == str(job_id):
            match = j
            break

    if not match:
        print(json.dumps({
            "ok": False,
            "error": f"Job not found online: {job_id}",
        }, ensure_ascii=False, indent=2))
        sys.exit(1)

    totals = _sync_job(match, root, args.force, args.dry_run, args.verbose, args.max)

    print(json.dumps({
        "ok": True,
        "resume_root": str(root),
        "mode": "sync-job",
        "job": {
            "id": match.get("id"),
            "encryptJobId": match.get("encryptJobId"),
            "name": match.get("name"),
        },
        "dry_run": args.dry_run,
        "totals": totals,
    }, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync BOSS/Zhipin recruiter application online resumes.",
    )
    parser.add_argument("--root", default=str(DEFAULT_ROOT),
                        help=f"Resume root directory. Default: {DEFAULT_ROOT}")
    parser.add_argument("--cdp-url", default=CDP_URL,
                        help=f"CDP URL. Default: {CDP_URL}")
    parser.add_argument("--verbose", action="store_true",
                        help="Print executed commands to stderr.")

    subparsers = parser.add_subparsers(dest="command", required=True)

    # refresh-jobs
    subparsers.add_parser("refresh-jobs", help="Refresh online job index only.")

    # sync-all
    p_all = subparsers.add_parser("sync-all", help="Incrementally sync resumes for all online jobs.")
    p_all.add_argument("--force", action="store_true",
                       help="Re-download candidates already marked downloaded.")
    p_all.add_argument("--dry-run", action="store_true",
                       help="Resolve candidates without downloading resumes.")
    p_all.add_argument("--max", type=int, default=0, metavar="N",
                       help="Max candidates to process per job (0=unlimited).")

    # sync-job
    p_job = subparsers.add_parser("sync-job", help="Incrementally sync one job.")
    p_job.add_argument("--job-id", required=True,
                       help="Numeric jobId or encryptJobId from job_index.json.")
    p_job.add_argument("--force", action="store_true",
                       help="Re-download candidates already marked downloaded.")
    p_job.add_argument("--dry-run", action="store_true",
                       help="Resolve candidates without downloading resumes.")
    p_job.add_argument("--max", type=int, default=0, metavar="N",
                       help="Max candidates to download per job (0=unlimited).")

    args = parser.parse_args()

    if args.command == "refresh-jobs":
        cmd_refresh_jobs(args)
    elif args.command == "sync-all":
        cmd_sync_all(args)
    elif args.command == "sync-job":
        cmd_sync_job(args)


if __name__ == "__main__":
    main()
