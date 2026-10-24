"""把已采集的岗位、简历和旧评分字段接入报告，不生成新的模型评价。"""
import re


def normalize_job_info(raw: dict) -> dict:
    job = dict(raw)
    lines = [line.strip() for line in raw.get("bodyText", "").splitlines() if line.strip()]
    values = [v.strip() for v in raw.get("formValues", []) if isinstance(v, str) and v.strip()]

    def after(label):
        for i, line in enumerate(lines[:-1]):
            if re.sub(r"\s+", "", line) == label:
                return lines[i + 1]
        return ""

    description = job.get("description") or max(
        (v for v in values if "\n" in v), key=len, default="")
    addresses = [v for v in values if "\n" not in v and
                 re.search(r"(?:区|县|路|街|镇|号)", v) and v != raw.get("jobName")]
    location = addresses[-1] if addresses else ""
    category = next((v for v in values if "\n" not in v and v not in
                     (raw.get("jobName"), location, description)), "")
    salary = ""
    body = "\n".join(lines)
    if "薪资范围" in body:
        salary_section = body.split("薪资范围", 1)[1].split("奖金绩效", 1)[0]
        amounts = re.findall(r"\d+(?:\.\d+)?\s*[kK]", salary_section)
        months = re.search(r"\d+\s*个月", salary_section)
        if len(amounts) >= 2:
            salary = "–".join(amounts[:2])
            if months:
                salary += " · " + months.group()
    extracted = {
        "company": after("公司"), "location": location, "salary": salary,
        "experience_required": after("经验"), "degree_required": after("学历"),
        "description": description,
    }
    for key, value in extracted.items():
        if not job.get(key):
            job[key] = value
    requirements_heading = re.search(
        r"(?:^|\n)\s*(?:[一二三四五六七八九十\d]+[、.．]\s*)?"
        r"(?:任职要求|任职资格|岗位要求|职位要求|任职条件)[：:]?\s*(?:\n|$)", description)
    if not job.get("core_requirements") and requirements_heading:
        requirements = description[requirements_heading.end():].splitlines()
        job["core_requirements"] = [line.strip() for line in requirements
                                    if line.strip() and line.strip() not in ("基本要求", "技能要求")]
    if not job.get("type_judgment"):
        job["type_judgment"] = {
            "type": category,
            "reason": "",
        }
    return job


def select_education(resume: dict, as_of: str) -> dict | None:
    """按历史任务日期选择最高已完成学历；无法唯一选校时留待复核。"""
    cutoff = re.sub(r"\D", "", as_of)[:8]
    levels = {"博士": 6, "硕士": 5, "本科": 4, "大专": 3, "高中": 2, "中专": 2}
    completed = []
    for edu in resume.get("education", []):
        if not edu.get("school"):
            continue
        end = str(edu.get("end") or "")
        digits = re.sub(r"\D", "", end)
        if len(digits) >= 6:
            end_date = digits[:8] if len(digits) >= 8 else digits + "31"
            if end_date <= cutoff:
                completed.append(edu)
        elif not end and edu.get("degree") == resume.get("degree"):
            # 简历已声明该学历，但旧格式没有日期；仍要求最高层级的学校唯一。
            completed.append(edu)
    if not completed:
        return None
    highest = max(levels.get(e.get("degree"), 0) for e in completed)
    choices = [e for e in completed if levels.get(e.get("degree"), 0) == highest]
    if len({e["school"] for e in choices}) != 1:
        return None
    return choices[0]


def enrich_score(score: dict, resume: dict, as_of: str) -> dict:
    """补取简历事实，适配已存在的旧理由格式；保留当前格式的模型内容。"""
    result = dict(score)
    result["dims"] = dict(score["dims"])
    if not result.get("school_name") and not result.get("school"):
        education = select_education(resume, as_of)
        if education:
            result["school_name"] = education["school"]
            result["school"] = " · ".join(education[k] for k in ("school", "major", "degree") if education.get(k))
        elif resume.get("education"):
            result["school"] = "；".join(
                " · ".join(e[k] for k in ("school", "major", "degree") if e.get(k)) +
                (f"（毕业：{e['end']}）" if e.get("end") else "")
                for e in resume["education"] if e.get("school")) + "（计分学校待复核）"
            result["school_selection_note"] = (
                f"需复核（截至{as_of}无法唯一确认最高已完成学历的学校，暂按60计）")
    if not result.get("work_years"):
        result["work_years"] = resume.get("work_years", "")
    if not result.get("match_type"):
        jobs = [w for w in resume.get("work_experience", []) if w.get("position")]
        if jobs:
            latest = max(jobs, key=lambda w: w.get("start") or "")
            result["match_type"] = "最近职位：" + latest["position"]
    for dim in ("exp", "skill", "proj", "major"):
        field = f"dims_{dim}_reason"
        legacy = score.get(dim)
        if not result.get(field) and isinstance(legacy, dict):
            result[field] = "；".join(legacy.get("reasons", []))
    return result
