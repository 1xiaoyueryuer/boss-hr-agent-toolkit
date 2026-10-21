"""评分身份以当前任务的原始简历为准；漏写 ID 可补，写错必须重评。"""


def bind_score_identity(score: dict, resume: dict, job_id: str) -> list[str]:
    meta = resume.get("_meta", {}) or {}
    expected = {
        "name": resume["name"].strip(),
        "geek_id": meta["encrypt_geek_id"],
        "job_id": meta.get("encrypt_job_id") or job_id,
    }
    errors = []
    for field, value in expected.items():
        actual = (score.get(field) or "").strip()
        if (actual or field == "name") and actual != value:
            errors.append(f"{field} 与原始简历不一致：应为 {value!r}，实际 {actual!r}")
    if not errors:
        score.update(expected)
    return errors
