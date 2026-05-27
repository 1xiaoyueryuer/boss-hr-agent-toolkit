# Resume download via CDP friend list → CLI

## When to use this

When `sync_boss_resumes.py` (which calls `boss hr applications`) returns 0 candidates, but the CDP browser chat page shows candidates. This happens when candidates come from **推荐牛人** (BOSS recommendations) or **主动搜索打招呼** rather than formal job applications.

## Full working script

```python
from patchright.sync_api import sync_playwright
import json, time, os, subprocess, random

CDP_URL = 'http://localhost:9222'
BOSS_ENV = {**dict(os.environ), 'PYTHONHOME': '', 'PYTHONIOENCODING': 'utf-8'}
# Ensure PATH includes ~/.local/bin for boss.exe
local_bin = os.path.expanduser('~/.local/bin')
if local_bin not in BOSS_ENV.get('PATH', ''):
    BOSS_ENV['PATH'] = f'{local_bin};{BOSS_ENV["PATH"]}'

# Set up job output directory
job_dir = os.path.expanduser('~/WorkBuddy/boss-resumes/jobs/<jobId>_<safeJobName>')
os.makedirs(job_dir, exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp(CDP_URL)
    ctx = browser.contexts[0]
    pg = ctx.pages[0]  # NEVER call new_page()

    # Intercept the friend list API
    captured = {}
    def on_resp(resp):
        url = resp.url
        try:
            if 'getBossFriendListV2' in url:
                captured['fl'] = resp.json()
        except:
            pass

    pg.on('response', on_resp)
    pg.goto('https://www.zhipin.com/web/chat/index', wait_until='networkidle', timeout=30000)
    time.sleep(5)

    fl_data = captured.get('fl', {}).get('zpData', {})
    friends = fl_data.get('friendList', [])

    # Filter out system users
    system_ids = {989, 797, 899, 1000, 798, 1400400}
    candidates = []
    for f in friends:
        uid = f.get('uid', 0)
        if uid in system_ids:
            continue
        candidates.append({
            'name': f.get('name', '?'),
            'uid': uid,
            'encryptUid': f.get('encryptUid', ''),
            'securityId': f.get('securityId', ''),
            'encryptJobId': f.get('encryptJobId', ''),
            'jobName': f.get('jobName', ''),
        })

    print(f'Found {len(candidates)} candidates')

    # Save candidate list
    with open(os.path.join(job_dir, 'candidates.json'), 'w') as f:
        json.dump(candidates, f, ensure_ascii=False, indent=2)

    # Download each resume via CLI
    for i, c in enumerate(candidates):
        name = c['name']
        if not c['encryptUid'] or not c['securityId']:
            print(f'  SKIP {name}: missing encryptUid or securityId')
            continue

        c_dir = os.path.join(job_dir, 'resumes', f'{name}_{c["uid"]}')
        os.makedirs(c_dir, exist_ok=True)

        result = subprocess.run(
            ['boss.exe', '--role', 'recruiter', '--platform', 'zhipin',
             '--cdp-url', CDP_URL, 'hr', 'resume',
             c['encryptUid'], '--job-id', c['encryptJobId'],
             '--security-id', c['securityId']],
            capture_output=True, timeout=45, env=BOSS_ENV)

        if result.returncode == 0:
            data = json.loads(result.stdout.decode('utf-8'))
            resume = data.get('data', {})
            basic = resume.get('basic', {})
            edu = resume.get('education', [])
            exp = resume.get('work_experience', [])

            # Save raw JSON
            with open(os.path.join(c_dir, 'raw_response.json'), 'w') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

            # Save normalized resume
            resume_json = {
                'basic': basic,
                'education': edu,
                'work_experience': exp,
                'project_experience': resume.get('project_experience', []),
            }
            with open(os.path.join(c_dir, 'resume.json'), 'w') as f:
                json.dump(resume_json, f, ensure_ascii=False, indent=2)

            # Save readable Markdown
            md_lines = [f'# {basic.get("name", name)}']
            levels = {'basic': basic, 'education': edu, 'experience': exp}
            for section, items in [('education', edu), ('workExperience', exp)]:
                if items:
                    md_lines.append(f'\n## {section.replace("experience","Experience")}')
                    for e in items:
                        school = e.get('school') or e.get('company') or ''
                        major = e.get('major') or e.get('title') or ''
                        period = f'{e.get("start", "")} - {e.get("end", "")}'
                        desc = e.get('description', '')[:200]
                        md_lines.append(f'- {school} | {major} | {period}')
                        if desc:
                            md_lines.append(f'  {desc}')

            with open(os.path.join(c_dir, 'resume.md'), 'w') as f:
                f.write('\n'.join(md_lines))

            name_val = basic.get('name', '')
            degree = basic.get('degree', '')
            print(f'  OK {name} (name={name_val}, edu={len(edu)}, exp={len(exp)})')
        else:
            err = result.stderr.decode('utf-8', errors='replace')[:100]
            print(f'  FAILED {name}: {err}')

        # Random delay 3-6 seconds
        if i < len(candidates) - 1:
            time.sleep(random.uniform(3, 6))
```

## Resume JSON fields

The `resume.json` structure:
```json
{
  "basic": {"name": "", "gender": "", "age": "", "degree": "本科/硕士/高中", "avatar": ""},
  "education": [{"school": "", "major": "", "degree": "", "start": "", "end": ""}],
  "work_experience": [{"company": "", "title": "", "start": "", "end": "", "description": ""}],
  "project_experience": []
}
```

## Known pitfalls

- **CLI must be REAL login**: `boss status` can show `logged_in: true` but be a false positive. Before running this, verify with `boss me` (returns real name) and `hr jobs list` (returns actual jobs). If false positive, run `boss login --cdp --timeout 30`.
- **CLI vs browser session**: `boss login --cdp` extracts the browser's session into the CLI. Without this step, the CLI may have a stale token while the browser is fine.
- **Degree field**: BOSS's `degree` field is the highest degree claimed (self-reported). Some candidates may list "高中" even if they have higher education — this might be unverified data.
- **securityId can expire**: These tokens are session-bound. If too much time passes between capturing the friend list and downloading resumes, securityIds may expire.
