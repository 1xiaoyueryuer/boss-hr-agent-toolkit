"""报告应保留原始文字，包括 C++、&、尖括号；外部文字不成为 HTML。"""
import copy
from html import escape
from html.parser import HTMLParser
import json
from pathlib import Path

from tests.cli.test_report import _decode, _run_cli, workspace


def test_external_text_is_displayed_literally(workspace):
    root, eid, job, rid, _, _ = workspace
    path = root / eid / "runs" / rid / "process" / "screening_results.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    literal = 'C++ / A&B / <试验平台> / "引号" <script>/* 无操作 */</script>'
    meta = data["meta"]
    meta.update(title=literal, subtitle=literal)
    meta["job"] = {k: literal for k in ("name", "company", "location", "salary", "experience_required", "degree_required")}
    meta["type_judgment"] = {"type": literal, "reason": literal}
    meta["core_requirements"] = [literal]
    data.update(footer=literal, generated_at=literal)
    data["dimension_labels"][0] = literal
    for c in data["candidates"]:
        for k in ("name", "school", "work_years", "current_role"):
            c[k] = literal
        c["dimensions"][0]["reason"] = literal
        c["highlights"] = [literal]
        c["concerns"] = [literal]
    hard = copy.deepcopy(data["candidates"][0])
    hard.update(hard_pass=False, hard_reason=literal)
    data["candidates"].append(hard)
    data["actions"] = {
        "recommend": [{"name": literal, "score": 80, "background": literal, "action": literal}],
        "pending": [{"name": literal, "score": 65, "strengths": literal, "action": literal}],
        "reject": [{"name": literal, "score": 45, "concerns": literal}],
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    proc = _run_cli("report", "--job-name", job, "--encrypt-job-id", eid, "--run-id", rid)
    assert proc.returncode == 0, _decode(proc.stderr)
    output = json.loads(_decode(proc.stdout))["data"]["report_file"]
    html = Path(output).read_text(encoding="utf-8")

    class Parsed(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tags, self.texts = [], []

        def handle_starttag(self, tag, attrs):
            self.tags.append(tag)

        def handle_data(self, value):
            self.texts.append(value)

    parsed = Parsed()
    parsed.feed(html)
    assert "script" not in parsed.tags
    assert "试验平台" not in parsed.tags
    assert escape(literal) in html
    assert literal in "".join(parsed.texts)
    # 所有展示位置都应原样显示同一段文字；固定页面结构仍可正常渲染。
    assert html.count(escape(literal)) >= 30
    assert '<table>' in html and 'class="action-card"' in html
