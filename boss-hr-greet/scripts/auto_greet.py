#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
boss-hr-greet：高分候选人自动打招呼脚本（patchright DOM 路线 · element.click）

工作流：
  1) 读 screening_results.json（最新 run）+ recommend_geek_ids.json + candidate_pool.json，
     按 score 阈值挑出高分名单，并反查每个目标的 encrypt_geek_id
  2) 连上 BOSS 推荐牛人页（已登录态），定位 iframe
  3) 渐进式滚动扫描整个 list：每屏 sleep + 收集所有候选人卡片
     （encrypt_geek_id 是唯一身份，name 仅作 fallback）
  4) 对每个高分目标：
       - 按 encrypt_geek_id 在实时 DOM 中找对应 li
       - 若未找到 → 渐进滚动 + 等待 + 重扫（有上限）；
         完整扫描仍未找到 → 必要时刷新页面一次；
         完整扫描 + 刷新仍没有 → not_found（记录滚动次数 + 扫描卡片数 + 原因）
       - 找到的卡片若 encrypt_geek_id 与目标不一致 → 不得点击，记 not_found reason='geekId mismatch'
       - 找到且 ID 一致 → 在卡片内 locator("button.btn-greet:has-text('打招呼')") 精确定位按钮
       - 用 patchright locator 拟人 click
       - 等 2 秒，按钮 text 应变"继续沟通"（验证成功）
       - 扫 iframe 看有没有"已向牛人发送招呼"对话框 → 点"知道了"
  5) 落 runs/<run_id>/process/greet_log.json + run_log.txt
     顶层 status: complete（greeted=total）/ partial_success（greeted>=1 且 not_found>=1）/ no_candidates

为什么用 locator 而不是裸坐标：
  - BOSS list 每个候选人渲染 5 列副本（横向布局），同一 (name, doc_y) 有多个按钮
  - 用 li:has-text(name) 限定到唯一候选人卡片 → 按钮 locator 唯一
  - 真实坐标由 patchright 自己算，不用维护 doc_y/doc_x

为什么不用 force click：
  - force click 会绕过遮挡检测，可能命中错误的按钮副本
  - patchright locator 默认 click 会等元素可见+稳定，更稳
"""
import sys, os, json, time, random, argparse, atexit
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'shared'))
import fix_encoding  # noqa: E402
from output_manager import JobOutputManager
from job_resume_store import JobResumeStore
from run_orchestrator import RunOrchestrator

from patchright.sync_api import sync_playwright
from human_interaction import human_move

DEFAULT_JOB = "线控底盘制动、转向工程师"
GREET_TEXT = "打招呼"
GREETED_TEXT = "继续沟通"
DIALOG_CLOSE_TEXT = "知道了"
DIALOG_TITLE = "已向牛人发送招呼"


# ============== 工具函数 ==============

def log(out, msg):
    """打印 + 写 run_log"""
    ts = time.strftime('%H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    with open(out.run_log_path, 'a', encoding='utf-8') as f:
        f.write(line + '\n')


def note_skip_if_unsaved(out, saved: bool):
    """异常退出钩子：未写出 greet_log.json 时仅记日志，不删任何文件。

    修复（2026-08-04）：之前的实现会调 output.prune_if_empty()，只要
    run_dir 里没有 .html 报告就 rmtree 整个目录 — 误删 run.json /
    job_detail.json / screening_results.json / scoring/ 等业务数据。

    本函数是 auto_greet 的"atexit 钩子"的唯一允许操作：
      - 写一行 run_log 提示本次没产出 greet_log.json
      - 永不删除 run_dir 或其中任何文件
      - 永不依赖"目录里恰好有 HTML"作为保留条件
      - 幂等：同一次进程内重复调用最多写一次（用 sentry 文件避免 atexit 多注册）

    Args:
        out: JobOutputManager 实例（仅用其 run_dir / run_log_path）。
        saved: True 表示本次成功写出 greet_log.json（跳过提示）。

    Returns:
        bool: True 表示本次写入了提示；False 表示之前已写或已 saved。
    """
    if saved:
        return False
    # 幂等：用一个 sentry 文件防止 atexit 重复触发时多次写日志
    sentry = os.path.join(out.run_dir, ".greet_skip_noted")
    try:
        if os.path.isfile(sentry):
            return False
        with open(out.run_log_path, 'a', encoding='utf-8') as f:
            f.write(
                f"[{time.strftime('%H:%M:%S')}] ⚠️  本次 greet 未产生 greet_log.json；"
                f"run_dir 完整保留: {out.run_dir}\n"
            )
        # 写 sentry 标记（不写在 run_log 里以免被业务清理误删）
        with open(sentry, 'w', encoding='utf-8') as f:
            f.write("noted\n")
        return True
    except Exception:
        return False


def maybe_finish(orch, run_id: str, greeted_count: int, *,
                 dry_run: bool = False, no_finish: bool = False) -> bool:
    """招呼完成后决定是否 finish run。返回 True 表示已 finish。

    修复（2026-08-04）：之前 orch.finish() 缺 run_id 实参（签名要求必填），
    抛 TypeError 被 except Exception 吞掉，run.json.finished 恒为 false。

    规则：
      - greeted_count == 0  → 不 finish（保留回头补招呼能力）
      - dry_run              → 不 finish（DRY-RUN 没真发招呼）
      - no_finish            → 不 finish（CLI 显式要求保留）
      - 其余                  → finish(run_id=run_id)（失败不静默）
    """
    if greeted_count <= 0:
        return False
    if dry_run:
        return False
    if no_finish:
        return False
    # 不再用 try/except 吞错 — 让异常向上抛，智能体能立刻看到
    orch.finish(run_id=run_id)
    return True


def _geek_id_lookup_paths(job_dir: str, run_id: str) -> list[str]:
    """列出可能含 encrypt_geek_id 反查表的文件，按优先级返回。"""
    paths = []
    run_dir = os.path.join(job_dir, 'runs', run_id)
    paths.append(os.path.join(run_dir, 'process', 'recommend_geek_ids.json'))
    paths.append(os.path.join(run_dir, 'process', 'new_resumes.json'))
    return [p for p in paths if os.path.exists(p)]


def _build_geek_id_index(job_dir: str, run_id: str) -> dict[str, str]:
    """name(去空白) → encrypt_geek_id 反查表。
    优先 recommend_geek_ids.json（BOSS 实时返回，字段最权威）；
    兜底 new_resumes.json（_meta.encrypt_geek_id）。
    同名多个 geekId 时只保留第一次出现（DOM 上若有同名卡片，靠
    encrypt_geek_id 二次校验，避免误发）。
    """
    index: dict[str, str] = {}
    for path in _geek_id_lookup_paths(job_dir, run_id):
        try:
            data = json.load(open(path, encoding='utf-8'))
        except Exception:
            continue
        items = data if isinstance(data, list) else []
        for item in items:
            gid = (item.get('encryptGeekId')
                   or (item.get('_meta') or {}).get('encrypt_geek_id')
                   or '')
            nm = (item.get('mateName')
                  or item.get('name')
                  or (item.get('_meta') or {}).get('name')
                  or '').strip()
            if gid and nm and nm not in index:
                index[nm] = gid
        if index:
            # 第一个有数据的文件就够了（recommend_geek_ids 已含全部候选人）
            break
    return index


def load_high_score_candidates(job_dir, run_id, score_threshold, only_names=None):
    """从指定 run 的 process/screening_results.json 读高分候选。

    2026-07-30 重构：
      - run_id 必填（数据边界）
      - 不再扫 runs/*/ 找"最新" —— 那是智能体偷懒入口
      - 找不到 screening_results.json → 高分列表为空，由调用方决定

    2026-08-04 重构（fix not_found bug）：
      - 增加 encrypt_geek_id 字段：从 recommend_geek_ids.json /
        new_resumes.json 按 name 反查（BOSS 真实身份标识）
      - DOM 扫描时 encrypt_geek_id 是唯一身份，name 仅作 fallback

    --only-names 模式：如果名单里的人在 screening_results 里找不到，会回退到
    state/candidate_pool.json 兜底（仅用于打招呼定位，不要求评分）。
    """
    if not run_id:
        raise ValueError("load_high_score_candidates 必须显式传 run_id，禁止扫 runs/*/")
    run_dir = os.path.join(job_dir, 'runs', run_id)
    screen_path = os.path.join(run_dir, 'process', 'screening_results.json')

    high = []
    missing_v2 = []  # v2: --only-names 中不存在于 v2 screening results 的名字
    if os.path.exists(screen_path):
        screen = json.load(open(screen_path, encoding='utf-8'))
        is_v2 = screen.get("schema_version") == "2.0"
        cands = screen.get('candidates') or []
        for c in cands:
            if is_v2:
                # v2 协议：招呼资格 = decision in (A,B) 且 contact_recommended=true。
                # --threshold 完全不参与资格判断。
                if not c.get("contact_recommended", False):
                    continue
                decision = c.get("decision", "C")
                if decision not in ("A", "B"):
                    continue
                score = c.get("final_score", 0)
                high.append({
                    'name': c.get('name', ''),
                    'score': score,
                    'tier': decision,
                    'school': c.get('education_tier', ''),
                    'work_years': '',
                    'current_role': c.get('best_track', '') or '',
                    'encrypt_geek_id': '',
                })
            else:
                # v1 旧协议：仅看 score >= threshold（向后兼容）。
                score = c.get('total', 0)
                if score < score_threshold:
                    continue
                high.append({
                    'name': c.get('name', ''),
                    'score': score,
                    'tier': c.get('tier', ''),
                    'school': c.get('school', ''),
                    'work_years': c.get('work_years', ''),
                    'current_role': c.get('current_role', ''),
                    'encrypt_geek_id': '',
                })

    # ★ v2 --only-names：与本 run v2 screening_results 的合法集合（A/B + contact_recommended=true）
    # 取交集。名单中 C 排除；名单中不在 v2 screening_results 的人 warning 跳过；
    # 禁止从 candidate_pool.json / resumes_master.json 等补招呼资格（仅用于反查
    # encryptGeekId，不用于"补 v2 资格"）。
    # ★ v1 --only-names：保持旧 pool 兜底，向后兼容。
    _is_v2_screen = False
    if os.path.exists(screen_path):
        try:
            with open(screen_path, "r", encoding="utf-8") as _f:
                _is_v2_screen = json.load(_f).get("schema_version") == "2.0"
        except Exception:
            _is_v2_screen = False
    if only_names:
        only_set = {n.strip() for n in only_names}
        before = len(high)
        if _is_v2_screen:
            # v2：仅按本 run screening 的合法集合过滤
            high = [h for h in high if h['name'].strip() in only_set]
            existing_names = {h['name'].strip() for h in high}
            for n in only_set - existing_names:
                print(f'⚠ --only-names 跳过「{n}」：不在本 run v2 screening_results.json 的合法集合（A/B + contact_recommended=true）中')
        else:
            # v1：旧 pool 兜底
            high = [h for h in high if h['name'].strip() in only_set]
            existing_names = {h['name'].strip() for h in high}
            missing = [n for n in only_set if n not in existing_names]
            if missing:
                pool_path = os.path.join(job_dir, 'state', 'candidate_pool.json')
                if os.path.exists(pool_path):
                    pool = json.load(open(pool_path, encoding='utf-8')).get('items', {})
                    name_to_item = {}
                    for k, item in pool.items():
                        n = item.get('name', '').strip()
                        if n:
                            name_to_item[n] = item
                    for n in missing:
                        item = name_to_item.get(n)
                        if item:
                            high.append({
                                'name': n,
                                'score': 0,
                                'tier': 'from-pool',
                                'school': item.get('school_name', '') or item.get('school', ''),
                                'work_years': item.get('work_years', ''),
                                'current_role': item.get('current_role', '') or item.get('expectPosition', ''),
                                'encrypt_geek_id': item.get('encrypt_geek_id', ''),
                                'from_pool_only': True,
                            })
        print(f'按 --only-names 过滤：{before} → {len(high)} 人')

    # v2 排序：A → B → 同类 final_score 倒序（即便 --only-names 也按此排序，
    # 让 max_count 截断稳定）。
    _rank = {"A": 0, "B": 1}
    high.sort(key=lambda h: (
        _rank.get(h.get("tier", ""), 99),
        -float(h.get("score", 0)),
        h.get("name", ""),
    ))

    # ★ 关键：name → encrypt_geek_id 反查（BOSS 唯一身份）
    # 优先 recommend_geek_ids.json（权威）；兜底 candidate_pool.json / state/resumes_master.json
    # 仅用于反查；v2 --only-names 已不再从 pool 补资格（避免绕过 v2 资格判断）。
    geek_index = _build_geek_id_index(job_dir, run_id)
    # 补 candidate_pool / resumes_master（仅 geek_id 反查）
    for fallback in [
        os.path.join(job_dir, 'state', 'candidate_pool.json'),
        os.path.join(job_dir, 'state', 'resumes_master.json'),
    ]:
        if os.path.exists(fallback):
            try:
                data = json.load(open(fallback, encoding='utf-8'))
                items = (data.get('items') if isinstance(data, dict) else data) or {}
                if isinstance(items, dict):
                    for k, item in items.items():
                        gid = item.get('encrypt_geek_id', '')
                        nm = (item.get('name') or '').strip()
                        if gid and nm and nm not in geek_index:
                            geek_index[nm] = gid
                elif isinstance(items, list):
                    for item in items:
                        gid = item.get('encrypt_geek_id', '')
                        nm = (item.get('name') or '').strip()
                        if gid and nm and nm not in geek_index:
                            geek_index[nm] = gid
            except Exception:
                pass

    for h in high:
        if not h.get('encrypt_geek_id'):
            h['encrypt_geek_id'] = geek_index.get(h['name'].strip(), '')

    return high


def load_positions(job_dir):
    """读 state/geek_positions.json（候选人 → doc_y/doc_x 映射）"""
    pos_path = os.path.join(job_dir, 'state', 'geek_positions.json')
    if not os.path.exists(pos_path):
        return {}
    data = json.load(open(pos_path, encoding='utf-8'))
    return data.get('positions', {})


def save_positions(job_dir, positions):
    """落 state/geek_positions.json"""
    pos_path = os.path.join(job_dir, 'state', 'geek_positions.json')
    payload = {
        'job': '线控底盘制动、转向工程师',
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'count': len(positions),
        'positions': positions,
    }
    with open(pos_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return pos_path


# ============== iframe 操作（locator 版）==============

def scroll_iframe_to(frame, target_y):
    """iframe 内瞬时滚动"""
    frame.evaluate(f"(y) => window.scrollTo({{top: y, behavior: 'instant'}})", target_y)
    time.sleep(random.uniform(0.3, 0.6))


def count_greet_buttons(frame):
    """扫 iframe 内 btn-greet 按钮数（不去重，BOSS 横向渲染多列）"""
    return frame.locator('button.btn-greet').count()


def _extract_card_identity(card_js: dict) -> dict:
    """从卡片 JS object 抽 encrypt_geek_id / name / doc_y / doc_x。
    多个 DOM 来源（属性 / href / data-*），按优先级。
    """
    out = {
        'encrypt_geek_id': '',
        'name': '',
        'doc_y': 0,
        'doc_x': 0,
        'btn_text': '',
    }
    if not isinstance(card_js, dict):
        return out

    # encrypt_geek_id 来源（按优先级取第一个非空）
    # 真实 BOSS DOM 用的属性是 data-geek / data-geekid（实测），
    # 其他只是兼容 / 兜底
    gid_candidates = [
        card_js.get('data_geek'),
        card_js.get('data_geekid'),
        card_js.get('data_geek_id'),
        card_js.get('data_encrypt_geek_id'),
        card_js.get('encrypt_geek_id'),
        card_js.get('security_id'),
        # 从 href 参数（如 /web/chat/geek/...&geekId=xxx）解析
        _query_param(card_js.get('href', ''), 'geekId'),
        _query_param(card_js.get('href', ''), 'encryptGeekId'),
        _query_param(card_js.get('href', ''), 'securityId'),
    ]
    for c in gid_candidates:
        if c:
            out['encrypt_geek_id'] = str(c).strip()
            break

    out['name'] = (card_js.get('name') or '').strip()
    out['doc_y'] = int(card_js.get('doc_y') or 0)
    out['doc_x'] = int(card_js.get('doc_x') or 0)
    out['btn_text'] = (card_js.get('btn_text') or '').strip()
    return out


def _query_param(href: str, key: str) -> str:
    """从 URL 查 query 参数（极简版，足够处理 BOSS 实际 href 形态）。"""
    if not href or '?' not in href:
        return ''
    try:
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(href).query)
        v = qs.get(key, [''])[0]
        return v or ''
    except Exception:
        return ''


# DOM 卡片扫描 JS（返回当前 DOM 中所有候选人卡片的身份 + 坐标）
# encryptGeekId 是 BOSS 唯一身份，name 仅作 fallback
_CARD_SCAN_JS = r"""() => {
    const out = [];
    const btns = document.querySelectorAll('button.btn-greet');
    for (const b of btns) {
        const t = (b.textContent || '').trim();
        if (t !== '打招呼') continue;  // 已招呼 / 其它按钮不算
        const card = b.closest('li.card-item');
        if (!card) continue;

        // 1) encrypt_geek_id：多个 DOM 来源（真实 BOSS DOM 用 data-geek + data-geekid）
        let gid = '';
        // (a) li 元素自身属性
        gid = card.getAttribute('data-geek')
           || card.getAttribute('data-geekid')
           || card.getAttribute('data-geek-id')
           || card.getAttribute('data-encrypt-geek-id')
           || card.getAttribute('data-uid')
           || '';
        // (b) 卡片内任意 [data-geek / data-geekid / data-geek-id / data-uid] 元素
        if (!gid) {
            const tagged = card.querySelector('[data-geek], [data-geekid], [data-geek-id], [data-encrypt-geek-id], [data-uid]');
            if (tagged) gid = tagged.getAttribute('data-geek')
                        || tagged.getAttribute('data-geekid')
                        || tagged.getAttribute('data-geek-id')
                        || tagged.getAttribute('data-encrypt-geek-id')
                        || tagged.getAttribute('data-uid')
                        || '';
        }
        // (c) 卡片内第一个 href 含 geekId 的 <a>
        if (!gid) {
            const anchors = card.querySelectorAll('a[href]');
            for (const a of anchors) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/[?&](?:geekId|encryptGeekId|securityId|geek_id)=([^&]+)/);
                if (m) { gid = decodeURIComponent(m[1]); break; }
            }
        }

        // 2) name
        let name = '';
        const nameEl = card.querySelector('[class*="name"]')
                     || card.querySelector('h3, h4');
        if (nameEl) {
            name = (nameEl.textContent || '').trim()
                .replace(/\s*(刚刚活跃|今日活跃|3日内活跃|本周活跃|2周内活跃|本月活跃)\s*$/, '')
                .trim();
        }

        // 3) 坐标（绝对 doc_y）
        const rect = b.getBoundingClientRect();
        const doc_y = Math.round(rect.top + window.scrollY);
        const doc_x = Math.round(rect.left + window.scrollX);

        out.push({
            encrypt_geek_id: gid,
            name: name,
            doc_y: doc_y,
            doc_x: doc_x,
            btn_text: t,
            btn_count: 1,
        });
    }
    return out;
}"""


def scan_dom_cards(frame) -> list[dict]:
    """扫当前 DOM 中所有候选人卡片（去重 by encrypt_geek_id）。
    不滚动、不刷新。返回 [{encrypt_geek_id, name, doc_y, doc_x, btn_text}, ...]
    """
    raw = frame.evaluate(_CARD_SCAN_JS) or []
    seen: dict[str, dict] = {}
    no_id: list[dict] = []
    for r in raw:
        rec = _extract_card_identity(r)
        if rec['encrypt_geek_id']:
            # 同 ID 多张卡（如横向布局）只保留 doc_y 最小（最靠前）
            key = rec['encrypt_geek_id']
            if key not in seen or rec['doc_y'] < seen[key]['doc_y']:
                seen[key] = rec
        else:
            no_id.append(rec)
    return list(seen.values()) + no_id


def scan_all_cards_progressively(frame, *, max_scroll_steps: int = 30,
                                 step_pause_sec: float = 0.8,
                                 settle_extra_sec: float = 1.2) -> list[dict]:
    """渐进式滚动扫描整个 list：每次滚一屏 + 收集 + 滚下一屏，
    直到 scrollHeight 不再增长 + 最后一屏无新卡。返回去重后的卡片列表。

    BOSS 推荐牛人页用虚拟列表 / 懒加载：一次性 scrollHeight 滚到底
    可能只加载当时可见的 li；渐进式滚动能稳定触发逐屏加载。
    """
    cards_by_id: dict[str, dict] = {}
    cards_no_id: list[dict] = []
    seen_signatures: set[str] = set()

    # 先回到顶部
    frame.evaluate(r'() => window.scrollTo({top: 0, behavior: "instant"})')
    time.sleep(0.5)

    last_scroll_y = -1
    no_growth_steps = 0
    for step in range(max_scroll_steps):
        cur_y = frame.evaluate('() => window.scrollY')
        cur_h = frame.evaluate('() => document.documentElement.scrollHeight')
        viewport_h = frame.evaluate('() => window.innerHeight')

        # 扫当前可见
        batch = scan_dom_cards(frame)
        for c in batch:
            sig = f"{c['encrypt_geek_id']}|{c['name']}|{c['doc_y']}"
            if sig in seen_signatures:
                continue
            seen_signatures.add(sig)
            if c['encrypt_geek_id']:
                key = c['encrypt_geek_id']
                if key not in cards_by_id or c['doc_y'] < cards_by_id[key]['doc_y']:
                    cards_by_id[key] = c
            else:
                cards_no_id.append(c)

        # 滚下一屏
        new_y = min(cur_y + max(int(viewport_h * 0.85), 600), cur_h)
        if new_y >= cur_h or cur_y == new_y:
            no_growth_steps += 1
            if no_growth_steps >= 2:
                break
        else:
            no_growth_steps = 0
            frame.evaluate('(y) => window.scrollTo({top: y, behavior: "instant"})', new_y)
            time.sleep(step_pause_sec)

        last_scroll_y = cur_y
        if last_scroll_y == new_y and step > 5:
            # 滚到底了
            break

    # 收尾：滚回顶再等一下，确保最顶部卡片稳定
    frame.evaluate(r'() => window.scrollTo({top: 0, behavior: "instant"})')
    time.sleep(settle_extra_sec)
    # 再次扫顶部（捕获收尾时刚加载的）
    top_batch = scan_dom_cards(frame)
    for c in top_batch:
        if c['encrypt_geek_id']:
            key = c['encrypt_geek_id']
            if key not in cards_by_id or c['doc_y'] < cards_by_id[key]['doc_y']:
                cards_by_id[key] = c
        elif not any(x['name'] == c['name'] and x['doc_y'] == c['doc_y']
                     for x in cards_no_id):
            cards_no_id.append(c)

    return list(cards_by_id.values()) + cards_no_id


def scan_and_record_positions(frame, scroll_first=True):
    """保留旧接口签名（兼容 --skip-scan 模式）。
    实际上扫描整个 list 并按 encrypt_geek_id 去重；name 仅 fallback。
    返回 {encrypt_geek_id: {doc_y, doc_x, name, btn_text}}。
    若 encrypt_geek_id 为空则用 name 作 key（fallback 路径）。
    """
    if scroll_first:
        # 兼容旧行为：仍尝试滚一次；扫描本身已用渐进式，所以这一步只 sleep 一下
        frame.evaluate(r'() => window.scrollTo({top: document.documentElement.scrollHeight, behavior: "instant"})')
        time.sleep(1.5)
        frame.evaluate(r'() => window.scrollTo({top: 0, behavior: "instant"})')
        time.sleep(0.8)
    cards = scan_all_cards_progressively(frame)
    positions = {}
    for c in cards:
        key = c['encrypt_geek_id'] or c['name']
        if not key:
            continue
        if key not in positions or c['doc_y'] < positions[key]['doc_y']:
            positions[key] = {
                'doc_y': c['doc_y'],
                'doc_x': c['doc_x'],
                'name': c['name'],
                'encrypt_geek_id': c['encrypt_geek_id'],
                'btn_text': c['btn_text'],
            }
    return positions


def scroll_iframe_smooth(frame, target_y):
    """iframe 内平滑滚动（避免瞬时跳触发反爬）"""
    frame.evaluate(
        """(y) => window.scrollTo({top: y, behavior: 'smooth'})""",
        target_y,
    )
    # 平滑滚动需较长时间等动画完成
    time.sleep(random.uniform(0.8, 1.2))


def greet_one_by_id(page, frame, iframe_box, name, encrypt_geek_id, pos,
                       dry_run=False, iframe=None):
    """v1.1.3 final: 按 encrypt_geek_id + 实时 DOM 扫描定位候选人。
    关键设计：
      - encrypt_geek_id 是 BOSS 唯一身份；name 仅作 fallback
      - 实时扫描 DOM（不依赖旧的 doc_y 缓存，因虚拟列表可能已重建）
      - 找到的卡片 encrypt_geek_id 与目标不一致 → 拒绝点击（同名陷阱）
      - 未找到 → 渐进滚动重扫（最多 max_redo_scroll 次）；
        已到底（reached_bottom=True）后仍无 → not_found_after_full_scan
      - **不刷新页面**：尊重用户当前 BOSS 页面状态（避免清空筛选条件、
        滚动位置、已招呼候选人列表）
      - dry_run=True → 找到后立即返回 dry_run 状态，不 click、不写 greeted、
        不 finish run
    """
    target_gid = (encrypt_geek_id or '').strip()
    target_name = (name or '').strip()

    # 1) 实时 DOM 扫描（用 encrypt_geek_id 找）
    found_card, match_by = _find_card_by_id(frame, target_gid, target_name)

    scroll_attempts = 0
    reached_bottom = False
    if not found_card:
        # 渐进滚动重扫（最多 6 次，每次 sleep 后再扫）
        for _ in range(6):
            scroll_attempts += 1
            try:
                cur_y = frame.evaluate('() => window.scrollY')
                cur_h = frame.evaluate('() => document.documentElement.scrollHeight')
                vh = frame.evaluate('() => window.innerHeight')
                new_y = min(cur_y + max(int(vh * 0.85), 600), cur_h)
                frame.evaluate('(y) => window.scrollTo({top: y, behavior: "instant"})', new_y)
                time.sleep(1.2)
            except Exception:
                break
            found_card, match_by = _find_card_by_id(frame, target_gid, target_name)
            if found_card:
                break
            if new_y >= cur_h:
                reached_bottom = True
                break  # 已到底

    if not found_card:
        # 完整扫描后仍未找到 → not_found_after_full_scan
        # 历史兼容性：CLI 仍按 not_found 计入 summary
        return {
            'name': name,
            'encrypt_geek_id': encrypt_geek_id,
            'found': False,
            'verified': False,
            'status': 'not_found_after_full_scan',
            'match_by': 'none',
            'scroll_attempts': scroll_attempts,
            'cards_scanned': _last_scan_count(),
            'reached_bottom': reached_bottom,
            'reason': (f'完整扫描后仍未找到（reached_bottom={reached_bottom}）；'
                       f'虚拟列表已到底或目标不在当前页'),
        }

    # 2) encryptGeekId 一致性二次校验（防同名陷阱）
    if target_gid and found_card.get('encrypt_geek_id') \
            and found_card['encrypt_geek_id'] != target_gid:
        return {
            'name': name,
            'encrypt_geek_id': encrypt_geek_id,
            'found_card_geek_id': found_card['encrypt_geek_id'],
            'found': False,
            'verified': False,
            'status': 'not_found_after_full_scan',
            'match_by': match_by,
            'scroll_attempts': scroll_attempts,
            'cards_scanned': _last_scan_count(),
            'reached_bottom': reached_bottom,
            'reason': (f"geekId mismatch: target={target_gid[:14]}... "
                       f"vs card={found_card['encrypt_geek_id'][:14]}..."),
        }

    # 3) 滚到候选人位置（视口顶部留 100px 余量）
    doc_y = found_card['doc_y']
    doc_x = found_card.get('doc_x', 0)
    try:
        viewport_h = frame.evaluate("() => window.innerHeight")
        target_y = max(0, doc_y - 100)
        scroll_iframe_smooth(frame, target_y)
        time.sleep(0.3)
    except Exception:
        pass

    # 4) 实时找按钮（encrypt_geek_id 锁定 li → 找 btn）
    btn_info = _find_btn_by_card_id(frame, target_gid, target_name, target_y=doc_y)
    if not btn_info.get('found'):
        return {
            'name': name,
            'encrypt_geek_id': encrypt_geek_id,
            'found': True,  # 卡片找到但 btn 失效
            'verified': False,
            'status': 'not_found_after_full_scan',
            'match_by': match_by,
            'scroll_attempts': scroll_attempts,
            'cards_scanned': _last_scan_count(),
            'reached_bottom': reached_bottom,
            'reason': btn_info.get('reason', '卡片找到但招呼按钮不可用'),
        }

    # 5) dry-run 早返：找到卡片与按钮，记录定位结果，**绝不 click**
    if dry_run:
        return {
            'name': name,
            'encrypt_geek_id': encrypt_geek_id,
            'target_encrypt_geek_id': target_gid,
            'found': True,
            'match_by': match_by,
            'scroll_attempts': scroll_attempts,
            'cards_scanned': _last_scan_count(),
            'reached_bottom': reached_bottom,
            'target_scroll_y': doc_y - 100,
            'target_doc_y': doc_y,
            'btn_text': btn_info.get('btn_text', ''),
            'reason': (f'dry_run: 找到 (encrypt_geek_id={target_gid[:14]}... '
                       f'doc_y={doc_y} btn_text=打招呼)；未 click'),
            'dry_run': True,
            'verified': False,   # dry-run 不验证（也不 click）
            'status': 'dry_run',
        }

    # 6) 预检阻挡层（沿用旧实现）
    try:
        page_blockers = _scan_blockers(page)
        frame_blockers = _scan_blockers(frame) if frame else []
        blockers = page_blockers + frame_blockers
        if blockers:
            print(f'  [WARN] 发现 {len(blockers)} 个阻挡层', flush=True)
            closed = _try_close_blockers(frame)
            if not closed:
                closed = _try_close_blockers(page)
            if closed:
                time.sleep(0.5)
    except Exception:
        pass

    # 7) 拟人 hover + evaluate click
    btn_idx = btn_info.get('btn_idx', -1)
    li_idx = btn_info.get('li_idx', -1)
    if btn_idx < 0 or li_idx < 0:
        return {
            'name': name, 'encrypt_geek_id': encrypt_geek_id,
            'found': True, 'verified': False, 'status': 'not_found_after_full_scan',
            'match_by': match_by, 'scroll_attempts': scroll_attempts,
            'cards_scanned': _last_scan_count(),
            'reached_bottom': reached_bottom,
            'reason': 'btn/li 索引无效',
        }

    try:
        _cur_box = iframe.bounding_box() if iframe else iframe_box
        _px = _cur_box['x'] + btn_info['x'] + btn_info['w'] / 2
        _py = _cur_box['y'] + btn_info['y'] + btn_info['h'] / 2
        human_move(page, (_px, _py))
        time.sleep(random.uniform(0.3, 0.5))
    except Exception:
        pass

    btn_text_before = frame.evaluate(
        "(idx) => { const all = document.querySelectorAll('button.btn-greet'); "
        "return all[idx] ? (all[idx].textContent||'').trim() : null; }", btn_idx)
    clicked = frame.evaluate(
        "(idx) => { const all = document.querySelectorAll('button.btn-greet'); "
        "if (all[idx] && (all[idx].textContent||'').trim()==='打招呼') { all[idx].click(); return true; } "
        "return false; }", btn_idx)
    if not clicked:
        return {
            'name': name, 'encrypt_geek_id': encrypt_geek_id,
            'found': True, 'verified': False, 'status': 'not_found_after_full_scan',
            'match_by': match_by, 'scroll_attempts': scroll_attempts,
            'cards_scanned': _last_scan_count(),
            'reached_bottom': reached_bottom,
            'reason': f'btn_idx={btn_idx} 不存在或 text 不是"打招呼"（之前：{btn_text_before!r}）',
        }

    time.sleep(4.0)

    # 7) 验证
    verified = False
    try:
        scan_result = frame.evaluate(
            r"(args) => { const name = args.name; "
            r"const btns = document.querySelectorAll('button[class*=\"btn-continue\"], button.btn-greet'); "
            r"for (const b of btns) { const t=(b.textContent||'').trim(); "
            r"if (!t.includes('继续') && !t.includes('沟通') && t!=='打招呼') continue; "
            r"if (t === '打招呼') continue; "
            r"let card = b.closest('li'); if (!card) card = b.closest('[class*=\"card\"]'); "
            r"if (!card) continue; "
            r"const ct=(card.innerText||'').replace(/\s+/g,''); "
            r"if (ct.includes(name)) return {found:true, btn_text:t, btn_class:b.className}; } "
            r"return {found:false}; }",
            {'name': name.replace(' ', '').strip()})
        if scan_result and scan_result.get('found'):
            verified = True
    except Exception:
        pass

    # 8) 关 dialog
    dialog_closed = False
    try:
        for f in page.frames:
            try:
                found = f.evaluate(
                    r"() => { const btns = document.querySelectorAll('button'); "
                    r"for (let i=0;i<btns.length;i++) { const t=(btns[i].textContent||'').trim(); "
                    r"if ((t==='知道了'||t==='我知道了') && btns[i].offsetWidth>0 && btns[i].offsetHeight>0) "
                    r"{ btns[i].click(); return t; } } return null; }")
                if found:
                    dialog_closed = True
                    break
            except Exception:
                continue
    except Exception:
        pass

    return {
        'name': name,
        'encrypt_geek_id': encrypt_geek_id,
        'found': True,
        'clicked': True,
        'verified': verified,
        'dialog_closed': dialog_closed,
        'status': 'greeted' if verified else 'clicked_unverified',
        'match_by': match_by,
        'scroll_attempts': scroll_attempts,
        'cards_scanned': _last_scan_count(),
    }


# ============== 实时 DOM 定位辅助 ==============

_LAST_SCAN_COUNT = [0]


def _last_scan_count() -> int:
    return _LAST_SCAN_COUNT[0]


_FIND_CARD_JS = r"""(args) => {
    const gid = (args.gid || '').trim();
    const name = (args.name || '').trim();
    const btns = document.querySelectorAll('button.btn-greet');
    let first_id_match = null;
    let first_name_match = null;
    let total = 0;
    for (const b of btns) {
        const t = (b.textContent || '').trim();
        if (t !== '打招呼') continue;
        total++;
        const card = b.closest('li.card-item');
        if (!card) continue;

        // 抽 encrypt_geek_id（真实 BOSS DOM：data-geek + data-geekid）
        let card_gid = '';
        card_gid = card.getAttribute('data-geek')
                 || card.getAttribute('data-geekid')
                 || card.getAttribute('data-geek-id')
                 || card.getAttribute('data-encrypt-geek-id')
                 || card.getAttribute('data-uid')
                 || '';
        if (!card_gid) {
            const tagged = card.querySelector('[data-geek], [data-geekid], [data-geek-id], [data-encrypt-geek-id], [data-uid]');
            if (tagged) card_gid = tagged.getAttribute('data-geek')
                              || tagged.getAttribute('data-geekid')
                              || tagged.getAttribute('data-geek-id')
                              || tagged.getAttribute('data-encrypt-geek-id')
                              || tagged.getAttribute('data-uid')
                              || '';
        }
        if (!card_gid) {
            for (const a of card.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/[?&](?:geekId|encryptGeekId|securityId|geek_id)=([^&]+)/);
                if (m) { card_gid = decodeURIComponent(m[1]); break; }
            }
        }

        // 抽 name
        let card_name = '';
        const nameEl = card.querySelector('[class*="name"]') || card.querySelector('h3, h4');
        if (nameEl) {
            card_name = (nameEl.textContent || '').trim()
                .replace(/\s*(刚刚活跃|今日活跃|3日内活跃|本周活跃|2周内活跃|本月活跃)\s*$/, '')
                .trim();
        }

        const rect = b.getBoundingClientRect();
        const doc_y = Math.round(rect.top + window.scrollY);
        const doc_x = Math.round(rect.left + window.scrollX);

        if (gid && card_gid === gid) {
            return {found: true, match_by: 'encrypt_geek_id',
                    card: {encrypt_geek_id: card_gid, name: card_name,
                           doc_y: doc_y, doc_x: doc_x, btn_text: t},
                    total};
        }
        if (!first_id_match && card_gid === '') {
            first_id_match = {encrypt_geek_id: '', name: card_name,
                              doc_y: doc_y, doc_x: doc_x, btn_text: t};
        }
        if (gid === '' && name && card_name === name && !first_name_match) {
            first_name_match = {encrypt_geek_id: card_gid, name: card_name,
                                doc_y: doc_y, doc_x: doc_x, btn_text: t};
        }
    }
    if (first_name_match) {
        return {found: true, match_by: 'name',
                card: first_name_match, total};
    }
    if (first_id_match) {
        return {found: true, match_by: 'no_gid_in_dom',
                card: first_id_match, total};
    }
    return {found: false, total};
}"""


def _find_card_by_id(frame, encrypt_geek_id: str, name: str) -> tuple[Optional[dict], str]:
    """实时扫当前 DOM，按 encrypt_geek_id 找候选卡片。
    返回 (card_dict_or_None, match_by)。
    match_by: 'encrypt_geek_id' / 'name' / 'no_gid_in_dom' / 'none'
    """
    try:
        res = frame.evaluate(_FIND_CARD_JS,
                              {'gid': encrypt_geek_id or '',
                               'name': name or ''}) or {}
    except Exception:
        res = {}
    _LAST_SCAN_COUNT[0] = int(res.get('total', 0))
    if res.get('found'):
        return res.get('card'), res.get('match_by', 'unknown')
    return None, 'none'


_FIND_BTN_JS = r"""(args) => {
    const gid = (args.gid || '').trim();
    const name = (args.name || '').trim();
    const target_dy = args.target_dy;
    const dy_tol = 260;
    const lis = document.querySelectorAll('li.card-item');
    let best_li = null;
    let best_li_idx = -1;
    let best_dy_diff = Infinity;

    for (let i = 0; i < lis.length; i++) {
        const li = lis[i];
        // encrypt_geek_id 锁定（真实 BOSS DOM：data-geek + data-geekid）
        let card_gid = li.getAttribute('data-geek')
                    || li.getAttribute('data-geekid')
                    || li.getAttribute('data-geek-id')
                    || li.getAttribute('data-encrypt-geek-id')
                    || li.getAttribute('data-uid')
                    || '';
        if (!card_gid) {
            const tagged = li.querySelector('[data-geek], [data-geekid], [data-geek-id], [data-encrypt-geek-id], [data-uid]');
            if (tagged) card_gid = tagged.getAttribute('data-geek')
                              || tagged.getAttribute('data-geekid')
                              || tagged.getAttribute('data-geek-id')
                              || tagged.getAttribute('data-encrypt-geek-id')
                              || tagged.getAttribute('data-uid')
                              || '';
        }
        if (!card_gid) {
            for (const a of li.querySelectorAll('a[href]')) {
                const href = a.getAttribute('href') || '';
                const m = href.match(/[?&](?:geekId|encryptGeekId|securityId|geek_id)=([^&]+)/);
                if (m) { card_gid = decodeURIComponent(m[1]); break; }
            }
        }

        // name（兜底）
        let card_name = '';
        const nameEl = li.querySelector('[class*="name"]') || li.querySelector('h3, h4');
        if (nameEl) {
            card_name = (nameEl.textContent || '').trim()
                .replace(/\s*(刚刚活跃|今日活跃|3日内活跃|本周活跃|2周内活跃|本月活跃)\s*$/, '')
                .trim();
        }

        const matched = (gid && card_gid === gid) || (gid === '' && name && card_name === name);
        if (!matched) continue;

        const btn = li.querySelector('button.btn-greet');
        if (!btn) continue;
        const btnText = (btn.textContent || '').trim();
        if (btnText !== '打招呼') continue;

        const r = li.getBoundingClientRect();
        const li_dy = r.top + window.scrollY;
        const dy_diff = Math.abs(li_dy - target_dy);
        if (dy_diff < best_dy_diff) {
            best_dy_diff = dy_diff;
            best_li = li;
            best_li_idx = i;
        }
    }
    if (!best_li) {
        return {found: false, reason: 'li.card-item 上无 encrypt_geek_id/name 匹配按钮'};
    }
    if (best_dy_diff > dy_tol) {
        return {found: false, reason: `找到但 dy_diff=${Math.round(best_dy_diff)} > ${dy_tol}（list 已重建，跳过避免点错人）`};
    }
    const btn = best_li.querySelector('button.btn-greet');
    const r = btn.getBoundingClientRect();
    const allBtns = document.querySelectorAll('button.btn-greet');
    let btn_idx = -1;
    for (let i = 0; i < allBtns.length; i++) {
        if (allBtns[i] === btn) { btn_idx = i; break; }
    }
    return {
        found: true,
        x: r.left, y: r.top, w: r.width, h: r.height,
        li_idx: best_li_idx, btn_idx: btn_idx,
        btn_text: (btn.textContent || '').trim(),
        dy_diff: best_dy_diff,
    };
}"""


def _find_btn_by_card_id(frame, encrypt_geek_id: str, name: str, *, target_y: int) -> dict:
    """在 li.card-item 上找与目标匹配（encrypt_geek_id 优先；fallback name）
    的招呼按钮。target_y 仅作 dy 容差参考。
    """
    try:
        return frame.evaluate(_FIND_BTN_JS,
                              {'gid': encrypt_geek_id or '',
                               'name': name or '',
                               'target_dy': int(target_y or 0)}) or {'found': False}
    except Exception as e:
        return {'found': False, 'reason': f'evaluate 异常: {type(e).__name__}'}


# 保留旧接口名（仅签名兼容；外部不应再调用）
def greet_one_by_position(page, frame, iframe_box, name, pos,
                          dry_run=False, iframe=None):
    """兼容旧调用：内部转发到 greet_one_by_id（encrypt_geek_id 从 pos 取）。"""
    encrypt_geek_id = (pos or {}).get('encrypt_geek_id', '')
    return greet_one_by_id(page, frame, iframe_box, name, encrypt_geek_id,
                           pos or {}, dry_run=dry_run, iframe=iframe)


def _calc_summary(greet_log: list, *, dry_run: bool = False) -> dict:
    """算 summary + 顶层 status。

    非 dry-run 路径状态语义：
      - 'no_candidates':   greeted=0 且 not_found=0 且 total=0
      - 'complete':        greeted=total>0 且 not_found=0
      - 'partial_success': greeted>=1 且 not_found>=1
      - 'all_not_found':   greeted=0 且 not_found>0

    dry-run 路径状态语义（v1.1.3 final）：
      - 'no_candidates':   目标列表为空
      - 'dry_run_complete': 有目标，全部只读定位完成（无论 found/not_found_after_full_scan）
      - 'dry_run_review':   有目标，部分或全部 not_found_after_full_scan
                            （dry-run 不 click，所以必有 not_found>=1；
                            只要 not_found>0 就标 review 让用户回头人工核对）

    dry-run 路径下 greeted/clicked_unverified 必为 0（不 click）。

    not_found 计数同时兼容旧 'not_found' 与新 'not_found_after_full_scan'。
    """
    greeted = sum(1 for r in greet_log if r.get('status') == 'greeted')
    unverified = sum(1 for r in greet_log if r.get('status') == 'clicked_unverified')
    not_found = sum(1 for r in greet_log
                    if r.get('status') in ('not_found', 'not_found_after_full_scan'))
    dry_run_count = sum(1 for r in greet_log if r.get('status') == 'dry_run')
    scanned = sum(1 for r in greet_log if r.get('status') == 'scanned')
    total = len(greet_log)

    if dry_run:
        # dry-run：只读，不写 greeted
        if total == 0:
            top = 'no_candidates'
        elif not_found > 0:
            top = 'dry_run_review'
        else:
            top = 'dry_run_complete'
        return {
            'status': top,
            'greeted': 0,
            'clicked_unverified': 0,
            'not_found': not_found,
            'dry_run': dry_run_count,
            'scanned': scanned,
            'total': total,
        }

    if total == 0:
        top = 'no_candidates'
    elif greeted == 0 and not_found > 0:
        top = 'all_not_found'
    elif not_found > 0 and greeted >= 1:
        top = 'partial_success'
    elif greeted > 0:
        top = 'complete'
    else:
        top = 'no_candidates'
    return {
        'status': top,
        'greeted': greeted,
        'clicked_unverified': unverified,
        'not_found': not_found,
        'dry_run': dry_run_count,
        'scanned': scanned,
        'total': total,
    }


def auto_greet(job_name=DEFAULT_JOB, score_threshold=70, max_count=10,
               run_id=None, only_names=None, dry_run=False, scroll_max=60,
               scan_only=False, skip_scan=False, encrypt_job_id=None):
    """打招呼主流程。

    关键设计（按用户的"不刷新 list 稳定"前提）：
      1) 不滚回顶、不刷新页面，复用 BOSS 当前 DOM
      2) 全量扫描：滚一遍 list 把每个候选人的 doc_y 记到 state/geek_positions.json
      3) 倒序招呼：从最底往上招乎（BOSS 动态换卡只影响底部，已招呼的人在底部）
      4) 每次招呼按 doc_y 直接 scrollTo + click（不走 locator 反复跳）

    模式：
      scan_only=True        只扫描记位置，不打招呼
      skip_scan=True        跳过扫描，用 state/geek_positions.json 已有位置直接打招呼
      默认                  先扫描，再用新位置打招呼
    """
    # 路径解析：直接交给 JobOutputManager 处理（统一 OUTPUT_ROOT 含义）。
    # 2026-07-28 修复：之前手拼路径时错把 BOSS_HR_OUTPUT_DIR 当 job_dir，
    #   导致 greet_log 落到 boss-hr-output/runs/<run>/process/。
    from output_manager import resolve_encrypt_job_id
    encrypt_job_id = resolve_encrypt_job_id(encrypt_job_id)
    if not encrypt_job_id:
        raise ValueError("缺少 encrypt_job_id。\n  传 --encrypt-job-id，或设置 env BOSS_HR_ENCRYPT_JOB_ID")
    output = JobOutputManager(job_name, encrypt_job_id=encrypt_job_id, run_id=run_id, lazy=True)

    # ★ 2026-07-30 重构：run_id 是数据边界，--run-id 必填。
    #   不再"扫 runs/*/ 找最新" —— 那是智能体偷懒入口。
    from run_orchestrator import RunOrchestrator
    orch = RunOrchestrator(job_name, encrypt_job_id=encrypt_job_id)
    run_id = orch.bind_existing_run(run_id)
    output.run_id = run_id

    # 异常护栏：脚本若异常退出且没写 greet_log.json，仅记日志；绝不删 run_dir。
    # 修复（2026-08-04）：之前的 atexit 会调 output.prune_if_empty()，
    # 该方法只要 run_dir 里没有 .html 报告就 rmtree 整个目录 — 会误删
    # run.json / job_detail.json / screening_results.json / scoring/ 等业务数据。
    # greet 是 Step 5，所有业务目录已被上游脚本（boss_jd / score / report）建好，
    # greet 没资格决定 run 是否"空"。本次 greet 唯一可能新增的是 greet_log.json，
    # 缺它就只是没工作成果，绝不是"run 是空的"。
    _SAVED = False

    def _note_skip():
        nonlocal _SAVED
        note_skip_if_unsaved(output, _SAVED)

    atexit.register(_note_skip)

    output.ensure_run_dir()
    run_id = output.run_id

    log(output, f'=== boss-hr-greet 启动 run_id={run_id} ===')
    log(output, f'岗位：{job_name} | 阈值：{score_threshold} | 上限：{max_count} | dry_run={dry_run}')
    log(output, f'模式：scan_only={scan_only} skip_scan={skip_scan}')

    high = load_high_score_candidates(output.job_dir, run_id, score_threshold, only_names)
    if not high and not only_names:
        # 默认模式（按 score 筛）无结果才退出
        log(output, '没有高分候选人，结束')
        return
    if not high and only_names:
        # --only-names 模式：screening 里没找到、pool 也没补上
        # → 用 stub 项继续往下走（扫描阶段如果 list 上真有人，可以补位置）
        log(output, '--only-names 给的名字在 screening/pool 都查不到，构造 stub 继续扫描')
        high = [{'name': n.strip(), 'score': 0, 'tier': 'manual',
                 'school': '', 'work_years': '', 'current_role': '',
                 'from_pool_only': True, 'from_stub': True} for n in only_names]

    # --only-names 模式下保留用户给定顺序，否则按 v2 规则排序：
    #   A 优先 → B 其次 → 同类 final_score 倒序。
    # v1 旧路径仍按 score 倒序。
    # 注：load_high_score_candidates 已按 v2 规则排序；此处仅对 v1 路径做
    # 旧式 score 倒序（load_high_score_candidates 在 v1 模式不排序）。
    if not only_names and not _is_v2_screen:
        high.sort(key=lambda x: -x['score'])
    if len(high) > max_count:
        log(output, f'高分 {len(high)} > max_count {max_count}，取前 {max_count}')
        high = high[:max_count]

    log(output, f'目标 {len(high)} 人（基于 run_id={run_id}）')
    for i, h in enumerate(high, 1):
        log(output, f'  #{i}: {h["name"]:8s} score={h["score"]:.1f} tier={h["tier"]:4s}')

    greet_log = []

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp('http://localhost:9222')
        ctx = browser.contexts[0]
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        if 'recommend' not in page.url:
            log(output, f'当前页面不是 recommend 页（{page.url[:80]}），goto 切换')
            page.goto('https://www.zhipin.com/web/chat/recommend',
                       wait_until='domcontentloaded', timeout=30000)
            time.sleep(5)
        else:
            log(output, f'复用已有 recommend 页（不刷新）: {page.url[:80]}')

        iframe = page.wait_for_selector('iframe', timeout=15000)
        frame = iframe.content_frame()
        iframe_box = iframe.bounding_box()
        log(output, f'iframe 定位 ({iframe_box["width"]:.0f}x{iframe_box["height"]:.0f})')

        # ★ Step 1: 一次性扫描整个 list DOM（先滚到底触发懒加载 → 再滚回顶取绝对坐标）
        positions = {}
        if not skip_scan:
            log(output, '开始扫描 list（一次性 evaluate 扫全）...')
            positions = scan_and_record_positions(frame, scroll_first=True)
            log(output, f'  扫描到 {len(positions)} 个候选人（去重后）')
            # 落盘
            save_positions(output.job_dir, positions)
            log(output, f'  位置已落盘：state/geek_positions.json（{len(positions)} 人）')
        else:
            positions = load_positions(output.job_dir)
            log(output, f'复用已有位置：{len(positions)} 人')

        if scan_only:
            log(output, '--scan-only 模式，跳过打招呼')
            for h in high:
                pos = positions.get(h['name'])
                greet_log.append({
                    **h,
                    'found': pos is not None,
                    'doc_y': pos['doc_y'] if pos else None,
                    'doc_x': pos.get('doc_x') if pos else None,
                    'status': 'scanned' if pos else 'not_found',
                    'reason': '' if pos else '未在当前 list 出现',
                })
        else:
            # ★ Step 2 (v1.1.3 hard): 按 encrypt_geek_id + 实时 DOM 扫描逐位招呼
            #   - 不再依赖一次性扫描结果（虚拟列表懒加载易遗漏）
            #   - 每位独立实时扫描 + 渐进滚动重试
            #   - **不刷新页面**（v1.1.3 final）：尊重用户当前 BOSS 页面状态
            #   - 每位招呼前重新扫一遍实时 DOM（BOSS 动态换卡，剩余候选人位置随时变）
            #   - 完整扫描（progressively scroll to bottom + 每屏 sleep）后
            #     仍找不到 → not_found_after_full_scan
            log(output, f'目标 {len(high)} 人开始招呼（按 encrypt_geek_id 实时定位）')

            for i, h in enumerate(high, 1):
                name = h['name']
                encrypt_geek_id = h.get('encrypt_geek_id', '') or ''
                log(output, f'\n--- 招呼 [{i}/{len(high)}]: {name} gid={encrypt_geek_id[:14]}... ---')

                # 每位招呼前**实时**扫一遍当前 DOM（BOSS 动态换卡；
                # 已招呼的人被 BOSS 从底部移除，剩余 doc_y 会变化）
                pre_scan = scan_dom_cards(frame)
                seen_ids = sorted({c.get('encrypt_geek_id') for c in pre_scan
                                   if c.get('encrypt_geek_id')})

                # ★ 实时扫描 + 重试（含渐进滚动）
                # greet_one_by_id 内部：实时找 → 找不到则渐进滚动重扫
                # → 完整扫描终止 → not_found_after_full_scan
                result = greet_one_by_id(
                    page, frame, iframe_box, name, encrypt_geek_id,
                    pos={},
                    dry_run=dry_run,
                    iframe=iframe,
                )

                # 注入本次扫描元数据
                result['target_encrypt_geek_id'] = encrypt_geek_id
                result['unique_ids_seen'] = len(seen_ids)
                result['reached_bottom'] = bool(result.get('reached_bottom', False))

                result.update(h)  # 合并 score/tier/school
                greet_log.append(result)

                if dry_run:
                    log(output, f'    [DRY-RUN] 找到 target_scroll_y={result.get("target_scroll_y", 0):.0f} '
                                f'cards_scanned={result.get("cards_scanned", 0)} '
                                f'unique_ids={len(seen_ids)} '
                                f'scroll_attempts={result.get("scroll_attempts", 0)}')
                elif result.get('status') == 'greeted':
                    log(output, f'    ✓ {name} 已打招呼（按钮变"继续沟通"）')
                elif result.get('status') == 'clicked_unverified':
                    log(output, f'    ⚠ {name} 点击了但验证失败')
                else:
                    log(output, f'    ✗ {name} 未找到 ({result.get("reason","")[:60]})')

                # 节流 3-6 秒（仅在成功招呼后；dry-run 与 not_found 不浪费 BOSS 节奏预算）
                if result.get('status') in ('greeted', 'clicked_unverified') and not dry_run:
                    wait = random.uniform(3, 6)
                    log(output, f'    ⏸ 节流 {wait:.1f}s')
                    time.sleep(wait)

                # 实时落盘（含顶层 status；dry_run 路径用 dry_run=True 区分语义）
                _summary_now = _calc_summary(greet_log, dry_run=dry_run)
                with open(output.get_process_path('greet_log.json'), 'w', encoding='utf-8') as f:
                    json.dump({
                        'job': job_name,
                        'run_id': run_id,
                        'score_threshold': score_threshold,
                        'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                        'mode': 'scan_and_greet_reverse',
                        'positions_count': len(positions),
                        'dry_run': dry_run,
                        'status': _summary_now['status'],
                        'summary': _summary_now,
                        'results': greet_log,
                    }, f, ensure_ascii=False, indent=2)

    # 最终落盘（含顶层 status）
    summary = _calc_summary(greet_log, dry_run=dry_run)
    log(output, f'\n=== 完成：status={summary["status"]} greeted={summary["greeted"]} '
                f'unverified={summary["clicked_unverified"]} not_found={summary["not_found"]} ===')

    with open(output.get_process_path('greet_log.json'), 'w', encoding='utf-8') as f:
        json.dump({
            'job': job_name,
            'run_id': run_id,
            'score_threshold': score_threshold,
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'mode': 'scan_and_greet_reverse',
            'positions_count': len(positions),
            'status': summary['status'],
            'summary': summary,
            'results': greet_log,
        }, f, ensure_ascii=False, indent=2)
    log(output, f'日志 → {output.get_process_path("greet_log.json")}')

    # 标记 greet 步骤完成（让后续补招呼仍能跟走同一 run_id）
    # 不主动 finish()：greeting 是 run 的"延展"，报告已生成后再补招也合理
    _SAVED = True  # 招呼日志已落盘，run 保留
    # v1.1.3: dry-run 模式下不写 run.json（dry_run 零副作用）
    if not dry_run:
        try:
            orch.mark_done('greet', run_id=run_id)
        except Exception as e:
            log(output, f'⚠️  mark_done 失败（不影响招呼结果）: {e}')

    # 是否自动 finish()
    # 2026-07-29 改进：默认招呼成功后自动 finish()，下次跑自动开新 run。
    # 显式 --no-finish 可保留「回头补招呼同一 run」能力。
    greeted_count = summary.get('greeted', 0)
    auto_finished = False
    if args.no_finish:
        log(output, '')
        log(output, '━' * 60)
        log(output, 'A 流程 5 步全部完成。本次 run 仍标记为「未 finish」，')
        log(output, '意味着下次跑 greet 会沿用此 run（用于回头补招呼）。')
        log(output, '')
        log(output, '要开新 run？执行：')
        log(output, f'    python -X utf8 -c "import sys;sys.path.insert(0,\'shared\');from run_orchestrator import RunOrchestrator;RunOrchestrator(\'{job_name}\').finish()"')
        log(output, '━' * 60)
    elif greeted_count > 0 and not args.dry_run:
        # 真招呼且至少招呼成功 1 人。
        # v1.1.3 fix: partial_success（greeted>=1 且 not_found>=1）时**不**
        # 自动 finish() — 让 run 保留待用户决定补招或接受部分完成。
        # complete（not_found=0）才走原 finish 路径。
        if summary.get('not_found', 0) > 0:
            log(output, '')
            log(output, '━' * 60)
            log(output, f'⚠️  partial_success：招呼 {greeted_count} 人成功，'
                        f'但 {summary.get("not_found")} 人未找到。')
            log(output, f'    run_id={run_id} 不自动 finish() — 保留待你决定。')
            log(output, '    选项：')
            log(output, '      1) 重跑 greet（同 eid/run_id，--only-names 补名单）')
            log(output, '      2) 显式 finish（接受部分完成）：')
            log(output, f'         python -c "import sys;sys.path.insert(0,\'shared\');'
                        f'from run_orchestrator import RunOrchestrator;'
                        f'RunOrchestrator(\'{job_name}\').finish()"')
            log(output, '━' * 60)
        else:
            # complete：原逻辑，自动 finish()
            try:
                auto_finished = maybe_finish(
                    orch, run_id,
                    greeted_count=greeted_count,
                    dry_run=args.dry_run,
                    no_finish=args.no_finish,
                )
            except TypeError as e:
                log(output, f'⚠️  finish() 签名错误: {e}')
                raise
            if auto_finished:
                log(output, '')
                log(output, '━' * 60)
                log(output, f'✅ A 流程 5 步全部完成，已自动 finish() run_id={run_id}。')
                log(output, f'招呼成功 {greeted_count} 人，下次跑会自动开新 run。')
                log(output, '━' * 60)
    elif args.dry_run:
        # dry-run 完成分支（v1.1.3 final）：永远不 mark_done / 不 finish / 不写 greeted
        log(output, '')
        log(output, '━' * 60)
        log(output, f'⏸  DRY-RUN 完成：status={summary["status"]}，不发送任何招呼。')
        if summary.get('status') == 'dry_run_complete':
            log(output, '    所有目标候选人都在 DOM 中定位到（按 encrypt_geek_id）。')
        elif summary.get('status') == 'dry_run_review':
            log(output, f'    {summary.get("not_found")} 位候选人完整扫描后仍未找到；'
                        '需检查 BOSS 当前推荐列表是否包含目标。')
        log(output, '    run.json 未修改、run 未 finish。')
        log(output, '━' * 60)
    else:
        log(output, '')
        log(output, '━' * 60)
        log(output, f'⏸  本轮招呼成功 0 人，未 finish()。')
        log(output, f'    如确认本轮结束，手动: RunOrchestrator(\'{job_name}\').finish()')
        log(output, '━' * 60)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='高分候选人回头打招呼（位置表 + 倒序版 / 一键式）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
三种调用姿势：
  1) 默认（一键：自动扫描 + 招呼）
     python auto_greet.py --only-names 张三,李四

  2) 仅扫描（不下发，用于调试/刷新位置表）
     python auto_greet.py --only-names 张三 --scan-only

  3) 用已有位置表直接招呼（list 没变，省一次扫描）
     python auto_greet.py --only-names 张三 --skip-scan
""",
    )
    parser.add_argument('--job-name', default=DEFAULT_JOB)
    parser.add_argument('--encrypt-job-id', default=None,
                        help='BOSS encryptJobId（推荐；新设计目录名依此定位；亦可走 env BOSS_HR_ENCRYPT_JOB_ID）')
    parser.add_argument('--threshold', type=float, default=70.0,
                        help='score 阈值（>= 阈值的候选人会被招呼；与 --only-names 互斥，--only-names 优先）')
    parser.add_argument('--max', type=int, default=10)
    parser.add_argument('--run-id', required=True,
                        help='【必填】run_id 是数据边界。新任务先跑 boss_jd.py 创建 run；不传直接报错。')
    parser.add_argument('--only-names', default=None,
                        help='逗号分隔，直接指定要打招呼的名字（精准点名模式）')
    parser.add_argument('--dry-run', action='store_true',
                        help='干跑：只定位不 click')
    parser.add_argument('--scroll-max', type=int, default=60,
                        help='扫描时最多滚多少屏（防止 list 不收敛）')
    parser.add_argument('--scan-only', action='store_true',
                        help='只扫描记位置到 geek_positions.json，不打招呼')
    parser.add_argument('--skip-scan', action='store_true',
                        help='跳过扫描，直接用已有 geek_positions.json 招呼（list 状态未变时可省 2s）')
    parser.add_argument('--no-finish', action='store_true',
                        help='招呼跑完后不自动 finish()，保留「回头补招呼同一 run」能力（默认招呼成功后自动 finish）')
    args = parser.parse_args()

    # 互斥校验：--scan-only 与 --skip-scan 不能同时给
    if args.scan_only and args.skip_scan:
        parser.error('--scan-only 和 --skip-scan 互斥，只能二选一')

    only_names = args.only_names.split(',') if args.only_names else None
    if only_names:
        # 精准点名模式：忽略 --threshold / --max
        score_threshold = 0
        max_count = len(only_names)
    else:
        score_threshold = args.threshold
        max_count = args.max

    auto_greet(
        job_name=args.job_name,
        score_threshold=score_threshold,
        max_count=max_count,
        run_id=args.run_id,
        only_names=only_names,
        dry_run=args.dry_run,
        scroll_max=args.scroll_max,
        scan_only=args.scan_only,
        skip_scan=args.skip_scan,
        encrypt_job_id=args.encrypt_job_id,
    )