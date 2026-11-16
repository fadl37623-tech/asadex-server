"""
SciGo Improver (المرحلة د): يقترح تحسيناً ولا ينشره.

الدورة لكل prompt:
  1) يقيس النسخة الحالية بالاختبارات الثابتة (أو يعيد استخدام قياس حديث).
  2) يجمع الأدلة: الحالات الفاشلة (المرئية فقط) + شكاوى المستخدمين المصنّفة لهذه النسخة.
  3) يطلب من Gemini كتابة نسخة معدّلة بأقل تغييرات ممكنة، ويتحقق منها آلياً.
  4) يحفظها كمسودة (لا تُفعَّل)، ويقيسها بنفس الاختبارات.
  5) يقارن، ويعرض عليك: الأدلة، فرق النص، الدرجات، وتوصية.
  6) أنت وحدك توافق (فتُفعَّل) أو ترفض، وتستطيع التراجع بضغطة.

حماية من "التدريب على الاختبار": نصف الحالات مخفية عن المحسّن (holdout)
ولا تُستخدم إلا في المقارنة النهائية.

الصفحة: /admin/proposals?key=ADMIN_KEY
"""

import difflib
import html
import json
import os
import re
import secrets
import threading
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, redirect, request

from eval_cases import CASES

bp = Blueprint("improver", __name__)
_d = {}

MIN_GAIN = 0.03                 # أقل تحسن مطلوب للتوصية
MAX_HOLDOUT_DROP = 0.02         # أقصى تراجع مسموح على الحالات المخفية
MAX_CASE_REGRESSION = 0.30      # تراجع حالة واحدة أكثر من هذا = رفض التوصية
MIN_COMPARABLE_CASES = 8
MAX_GROWTH = 1.25
MIN_SHRINK = 0.6
STALE_MINUTES = 40


def init(app, get_conn, engine, evaluator, analyzer):
    _d.update(get_conn=get_conn, engine=engine, evaluator=evaluator, analyzer=analyzer)
    _create_tables()
    app.register_blueprint(bp)


def _create_tables():
    conn = None
    try:
        conn = _d["get_conn"]()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS proposals (
                id SERIAL PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                base_version INTEGER NOT NULL,
                new_version INTEGER,
                status TEXT NOT NULL DEFAULT 'evaluating'
                    CHECK (status IN ('evaluating','ready','approved','rejected','rolled_back','error')),
                evidence JSONB,
                changes JSONB,
                comparison JSONB,
                base_run_id TEXT,
                new_run_id TEXT,
                base_score REAL,
                new_score REAL,
                recommended BOOLEAN,
                note TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                decided_at TIMESTAMP
            )
        """)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("PROPOSALS TABLE ERROR:", repr(e))
    finally:
        if conn:
            conn.close()


def _admin_ok():
    expected = os.environ.get("ADMIN_KEY", "")
    supplied = request.headers.get("X-Admin-Key") or request.values.get("key", "")
    return bool(expected) and secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


# ============================================================
# منطق خالص (قابل للاختبار)
# ============================================================

_KEY_RE = re.compile(r'"([A-Za-z_]+)"\s*:')
_CARD_RE = re.compile(r":::([a-z]+)")
_PLACEHOLDER_RE = re.compile(r"\{\{[a-z_0-9]+\}\}")


HOLDOUT_KEYS = {c["key"] for c in CASES if c.get("holdout")}


def is_holdout(case_key):
    """الحالات المخفية عن المحسّن (محددة صراحةً في eval_cases.py)."""
    return case_key in HOLDOUT_KEYS


def validate_new_prompt(base, new):
    """فحوص آلية على النص المقترح. ترجع قائمة مشكلات (فارغة = سليم)."""
    if not new or not new.strip():
        return ["النص المقترح فارغ"]
    problems = []
    if new.strip() == base.strip():
        problems.append("النص لم يتغير")
    if len(new) > len(base) * MAX_GROWTH + 200:
        problems.append("النص أطول من المسموح (أكثر من 25%)")
    if len(new) < len(base) * MIN_SHRINK:
        problems.append("النص أقصر بكثير من الأصل (قد يكون حذف قواعد)")
    for label, rx in (("متغيرات {{..}}", _PLACEHOLDER_RE),
                      ("حقول JSON", _KEY_RE),
                      ("علامات البطاقات :::", _CARD_RE)):
        missing = set(rx.findall(base)) - set(rx.findall(new))
        if missing:
            problems.append(f"حُذف من النص: {label} ({', '.join(sorted(missing))[:80]})")
    return problems


def parse_improver_output(text):
    """يرجع (changes: list[dict], new_prompt: str). يرمي ValueError إذا الصيغة خاطئة."""
    m = re.search(r"===CHANGES_JSON===\s*(.*?)\s*===NEW_PROMPT===\s*(.*?)\s*(?:===END===|\Z)",
                  text or "", re.S)
    if not m:
        raise ValueError("improver output format invalid")
    changes_raw = re.sub(r"^`(?:json)?\s*|\s*```$", "", m.group(1).strip())
    try:
        changes = json.loads(changes_raw)
    except Exception:
        raise ValueError("improver changes JSON invalid")
    if not isinstance(changes, list):
        raise ValueError("improver changes must be a list")
    clean = [{"what": str(c.get("what", ""))[:300], "why": str(c.get("why", ""))[:400]}
             for c in changes if isinstance(c, dict)][:10]
    new_prompt = re.sub(r"^(?:\w+)?\s*|\s*```$", "", m.group(2).strip()).strip()
    return clean, new_prompt


def compare(base_results, new_results):
    """يقارن نتائج نسختين على الحالات المشتركة غير المخطئة."""
    b = {r["case_key"]: r for r in base_results if not r["errored"] and r["score"] is not None}
    n = {r["case_key"]: r for r in new_results if not r["errored"] and r["score"] is not None}
    common = sorted(set(b) & set(n))

    def mean(keys, d):
        return sum(d[k]["score"] for k in keys) / len(keys) if keys else None

    hold = [k for k in common if is_holdout(k)]
    vis = [k for k in common if not is_holdout(k)]
    cases = [{"case_key": k, "base": round(b[k]["score"], 3), "new": round(n[k]["score"], 3),
              "delta": round(n[k]["score"] - b[k]["score"], 3), "holdout": is_holdout(k)}
             for k in common]
    regress = [c["case_key"] for c in cases if c["delta"] < -MAX_CASE_REGRESSION]

    base_all, new_all = mean(common, b), mean(common, n)
    base_h, new_h = mean(hold, b), mean(hold, n)
    reasons = []
    if len(common) < MIN_COMPARABLE_CASES:
        reasons.append(f"الحالات القابلة للمقارنة {len(common)} فقط (أقل من {MIN_COMPARABLE_CASES})")
    if base_all is not None and new_all is not None and (new_all - base_all) < MIN_GAIN:
        reasons.append("التحسن الكلي أقل من 3%")
    if base_h is not None and new_h is not None and new_h < base_h - MAX_HOLDOUT_DROP:
        reasons.append("تراجع على الحالات المخفية")
    if regress:
        reasons.append("تراجع كبير في: " + ", ".join(regress))

    return {
        "n_common": len(common),
        "base": base_all, "new": new_all,
        "visible": {"base": mean(vis, b), "new": mean(vis, n), "n": len(vis)},
        "holdout": {"base": base_h, "new": new_h, "n": len(hold)},
        "cases": cases,
        "regressions": regress,
        "reasons": reasons,
        "recommended": not reasons,
    }


# ============================================================
# جمع الأدلة
# ============================================================

def _failing_cases(base_results, inputs):
    out = []
    for r in base_results:
        if r["errored"] or r["passed"] or is_holdout(r["case_key"]):
            continue                      # المخفية لا تُعرض للمحسّن أبداً
        out.append({
            "case": r["case_key"],
            "input": (inputs.get(r["case_key"]) or "")[:300],
            "failed_checks": [f"{c['check']}: {c['detail']}" for c in (r["checks"] or []) if not c["passed"]][:4],
            "judge_issues": ((r.get("judge") or {}).get("issues") or [])[:4],
            "output_excerpt": (r.get("output") or "")[:500],
        })
    return out[:8]


def _user_feedback(prompt_name, version):
    """شكاوى مصنّفة ومربوطة بهذه النسخة تحديداً (لا نستعمل تقييمات بلا نسخة)."""
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT a.category, COUNT(*), (ARRAY_AGG(a.summary ORDER BY a.feedback_id DESC))[1]
            FROM feedback_analysis a JOIN answer_feedback f ON f.id = a.feedback_id
            WHERE f.prompt_name = %s AND f.prompt_version = %s AND a.cause = 'prompt'
            GROUP BY a.category ORDER BY COUNT(*) DESC LIMIT 8
        """, (prompt_name, version))
        issues = [{"category": cat, "count": n, "example": (s or "")[:200]} for cat, n, s in c.fetchall()]
        c.execute("""
            SELECT comment FROM answer_feedback
            WHERE prompt_name = %s AND prompt_version = %s AND rating = -1
              AND comment IS NOT NULL AND BTRIM(comment) <> ''
            ORDER BY created_at DESC LIMIT 5
        """, (prompt_name, version))
        comments = [(r[0] or "")[:300] for r in c.fetchall()]
        return issues, comments
    finally:
        conn.close()


# ============================================================
# المهمة الخلفية
# ============================================================

def _set(pid, **fields):
    if not fields:
        return
    json_cols = {"evidence", "changes", "comparison"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in json_cols:
            sets.append(f"{k} = %s::jsonb")
            vals.append(json.dumps(v, ensure_ascii=False))
        else:
            sets.append(f"{k} = %s")
            vals.append(v)
    vals.append(pid)
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute(f"UPDATE proposals SET {', '.join(sets)} WHERE id = %s", vals)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _job(pid, name):
    engine, evaluator, analyzer = _d["engine"], _d["evaluator"], _d["analyzer"]
    try:
        active = engine.get_active(name)
        if not active:
            _set(pid, status="error", note="الـ prompt غير موجود")
            return
        base_version, base_text = active["version"], active["text"]

        try:                                           # تصنيف أي شكاوى جديدة (اختياري)
            analyzer.analyze_pending()
        except Exception as e:
            print("IMPROVER: analyze skipped:", repr(e))

        base_run = evaluator.latest_done_run(name, base_version) or evaluator.run_sync(name, base_version)
        _set(pid, base_run_id=base_run)
        _, base_results = evaluator.get_run_results(base_run)

        inputs = evaluator.load_case_inputs(name)
        failing = _failing_cases(base_results, inputs)
        issues, comments = _user_feedback(name, base_version)
        evidence = {"failing_cases": failing, "user_issues": issues, "user_comments": comments}
        _set(pid, evidence=evidence)

        if not failing and not issues:
            _set(pid, status="error",
                 note="لا توجد أدلة كافية للتحسين: لا حالات فاشلة ولا شكاوى مصنّفة لهذه النسخة. "
                      "لن أعدّل شيئاً بلا دليل.")
            return

        payload = {"prompt_name": name, "current_prompt": base_text, "failing_cases": failing,
                   "user_issues": issues, "user_comments": comments}
        text, _, _ = engine.run_internal("improver_rewrite", json.dumps(payload, ensure_ascii=False))
        changes, new_prompt = parse_improver_output(text)

        problems = validate_new_prompt(base_text, new_prompt)
        if problems:
            _set(pid, status="error", changes=changes,
                 note="رُفض النص المقترح آلياً: " + "؛ ".join(problems))
            return

        row = engine.get_version_row(name, base_version)
        new_version = engine.create_draft(name, new_prompt, row["config"],
                                          notes=f"proposal #{pid}", source="engine")
        _set(pid, new_version=new_version, changes=changes)
        new_run = evaluator.run_sync(name, new_version)
        _set(pid, new_run_id=new_run)
        _, new_results = evaluator.get_run_results(new_run)

        cmp = compare(base_results, new_results)
        _set(pid, comparison=cmp, base_score=cmp["base"], new_score=cmp["new"],
             recommended=cmp["recommended"], status="ready")
    except Exception as e:
        print("IMPROVER JOB ERROR:", repr(e))
        try:
            _set(pid, status="error", note=("فشل: " + str(e))[:300])
        except Exception as e2:
            print("IMPROVER SET ERROR:", repr(e2))


def start_proposal(name):
    evaluator = _d["evaluator"]
    if not evaluator.load_case_inputs(name):
        raise ValueError("لا توجد اختبارات ثابتة لهذا الـ prompt.")
    active = _d["engine"].get_active(name)
    if not active:
        raise ValueError("الـ prompt غير موجود.")
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("""UPDATE proposals SET status='error', note='interrupted'
                     WHERE status='evaluating' AND created_at < NOW() - (%s || ' minutes')::interval""",
                  (str(STALE_MINUTES),))
        c.execute("SELECT 1 FROM proposals WHERE prompt_name=%s AND status='evaluating'", (name,))
        if c.fetchone():
            conn.commit()
            raise ValueError("يوجد اقتراح قيد التنفيذ لهذا الـ prompt.")
        c.execute("INSERT INTO proposals (prompt_name, base_version) VALUES (%s, %s) RETURNING id",
                  (name, active["version"]))
        pid = c.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    threading.Thread(target=_job, args=(pid, name), daemon=True).start()
    return pid


# ============================================================
# العرض
# ============================================================

def _e(x):
    return html.escape(str(x if x is not None else ""))


def _pct(x):
    return f"{round(x * 100)}%" if x is not None else "-"


def _delta(x):
    if x is None:
        return "-"
    v = round(x * 100)
    cls = "good" if v > 0 else ("bad" if v < 0 else "muted")
    return f"<span class='{cls}'>{'+' if v > 0 else ''}{v}%</span>"


CSS = """<style>
body{font-family:system-ui,-apple-system,Segoe UI,Tahoma,sans-serif;margin:0;padding:14px;background:#f5f6f8;color:#1c1c1e;line-height:1.55}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px;color:#1565c0}
.card{background:#fff;border-radius:14px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.muted{color:#666;font-size:13px}.ok{background:#e8f5e9;border:1px solid #81c784}.warn{background:#fff4e5;border:1px solid #ffb74d}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;text-align:right;border-bottom:1px solid #eee;vertical-align:top}
th{color:#555;font-weight:600}.bad{color:#c62828;font-weight:600}.good{color:#2e7d32;font-weight:600}
a.btn,button.btn{display:inline-block;padding:9px 16px;background:#1565c0;color:#fff;border:0;border-radius:18px;text-decoration:none;font-size:14px;cursor:pointer}
button.red{background:#c62828}button.grey{background:#757575}button.green{background:#2e7d32}
a{color:#1565c0}.wrap{overflow-x:auto}
pre.diff{direction:ltr;text-align:left;white-space:pre-wrap;font-size:12px;background:#fafafa;padding:8px;border-radius:8px;margin:0}
.add{background:#e6ffed}.del{background:#ffeef0}
</style>"""

HEAD = ("<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Asadex Proposals</title>" + CSS + "</head><body>")

STATUS_AR = {"evaluating": "قيد التنفيذ... (حدّث الصفحة)", "ready": "جاهز للمراجعة", "approved": "مُفعَّل",
             "rejected": "مرفوض", "rolled_back": "تم التراجع", "error": "لم يكتمل"}
def render_diff(base, new):
    lines = []
    for line in difflib.unified_diff(base.splitlines(), new.splitlines(), "الحالي", "المقترح", lineterm="", n=1):
        if line.startswith("+++") or line.startswith("---"):
            continue
        cls = "add" if line.startswith("+") else ("del" if line.startswith("-") else "")
        lines.append(f"<span class='{cls}'>{_e(line)}</span>")
    return "<pre class='diff'>" + "\n".join(lines) + "</pre>" if lines else "<div class='muted'>لا فروق.</div>"


def render_list(prompts, rows, message, key):
    k = quote(key)
    out = [HEAD, "<h1>اقتراحات التحسين</h1>",
           "<div class='muted'>النظام يقترح ويختبر. أنت وحدك توافق أو ترفض.</div>"]
    if message:
        out.append(f"<div class='card warn'>{_e(message)}</div>")
    out.append("<h2>اطلب اقتراحاً</h2><div class='card wrap'><table><tr><th>Prompt</th><th>النسخة</th><th></th></tr>")
    for p in prompts:
        out.append(f"<tr><td>{_e(p['name'])}</td><td>v{_e(p['version'])}</td>"
                   f"<td><a class='btn' href='/admin/proposals/new?key={k}&name={quote(p['name'])}'>اقترح تحسيناً</a></td></tr>")
    out.append("</table></div><h2>الاقتراحات</h2>")
    if rows:
        out.append("<div class='card wrap'><table><tr><th>#</th><th>Prompt</th><th>النسخ</th><th>الحالة</th>"
                   "<th>الدرجة</th><th>توصية</th><th></th></tr>")
        for r in rows:
            vers = f"v{r['base_version']}" + (f" ← v{r['new_version']}" if r["new_version"] else "")
            score = f"{_pct(r['base_score'])} ← {_pct(r['new_score'])}" if r["new_score"] is not None else "-"
            rec = "✅" if r["recommended"] else ("⚠️" if r["recommended"] is False else "-")
            out.append(f"<tr><td>{r['id']}</td><td>{_e(r['prompt_name'])}</td><td>{vers}</td>"
                       f"<td>{_e(STATUS_AR.get(r['status'], r['status']))}</td><td>{score}</td><td>{rec}</td>"
                       f"<td><a href='?key={k}&id={r['id']}'>فتح</a></td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card muted'>لا توجد اقتراحات بعد.</div>")
    out.append("</body></html>")
    return "".join(out)


def _form(pid, action, label, cls, key, confirm=None):
    onsubmit = f" onsubmit=\"return confirm('{_e(confirm)}')\"" if confirm else ""
    return (f"<form method='post' action='/admin/proposals/decide' style='display:inline-block;margin:4px'{onsubmit}>"
            f"<input type='hidden' name='key' value='{_e(key)}'><input type='hidden' name='id' value='{pid}'>"
            f"<input type='hidden' name='action' value='{action}'><button class='btn {cls}'>{label}</button></form>")


def render_detail(p, base_text, new_text, key):
    k = quote(key)
    out = [HEAD, f"<h1>#{p['id']} {_e(p['prompt_name'])}: v{p['base_version']}"
                 f"{' ← v' + str(p['new_version']) if p['new_version'] else ''}</h1>",
           f"<div class='muted'>الحالة: {_e(STATUS_AR.get(p['status'], p['status']))}</div>",
           f"<p><a href='?key={k}'>← رجوع</a></p>"]
    if p.get("note"):
        out.append(f"<div class='card warn'>{_e(p['note'])}</div>")

    cmp = p.get("comparison")
    if cmp:
        if cmp["recommended"]:
            out.append("<div class='card ok'>✅ <b>موصى به:</b> تحسّنت الدرجة دون تراجع على الحالات المخفية.</div>")
        else:
            out.append("<div class='card warn'>⚠️ <b>غير موصى به:</b><br>" +
                       "<br>".join("• " + _e(r) for r in cmp["reasons"]) + "</div>")
        out.append("<h2>الدرجات</h2><div class='card'><table><tr><th></th><th>الحالي</th><th>المقترح</th><th>الفرق</th></tr>")
        rows = [("الكل", cmp["base"], cmp["new"]),
                (f"الحالات المرئية للمحسّن ({cmp['visible']['n']})", cmp["visible"]["base"], cmp["visible"]["new"]),
                (f"الحالات المخفية ({cmp['holdout']['n']})", cmp["holdout"]["base"], cmp["holdout"]["new"])]
        for label, b, n in rows:
            out.append(f"<tr><td>{label}</td><td>{_pct(b)}</td><td>{_pct(n)}</td>"
                       f"<td>{_delta(None if b is None or n is None else n - b)}</td></tr>")
        out.append("</table><div class='muted'>العيّنة صغيرة والحَكَم نفسه نموذج ذكاء اصطناعي: "
                   "اعتبر الفروق الصغيرة ضجيجاً.</div></div>")

    if p.get("changes"):
        out.append("<h2>التعديلات ولماذا</h2>")
        for c in p["changes"]:
            out.append(f"<div class='card'><b>{_e(c.get('what'))}</b><div class='muted'>{_e(c.get('why'))}</div></div>")

    ev = p.get("evidence") or {}
    if ev.get("user_issues") or ev.get("failing_cases"):
        out.append("<h2>الأدلة التي بُني عليها</h2><div class='card'>")
        for i in ev.get("user_issues", []):
            out.append(f"<div>👥 {_e(i['category'])} ({i['count']}): <span class='muted'>{_e(i['example'])}</span></div>")
        for f in ev.get("failing_cases", []):
            out.append(f"<div>🧪 {_e(f['case'])}: <span class='muted'>{_e('; '.join(f['failed_checks'] + f['judge_issues']))}</span></div>")
        out.append("</div>")

    if cmp and cmp.get("cases"):
        out.append("<h2>كل حالة</h2><div class='card wrap'><table><tr><th>الحالة</th><th>الحالي</th><th>المقترح</th><th>الفرق</th></tr>")
        for c in cmp["cases"]:
            hid = " 🔒" if c["holdout"] else ""
            out.append(f"<tr><td>{_e(c['case_key'])}{hid}</td><td>{_pct(c['base'])}</td><td>{_pct(c['new'])}</td><td>{_delta(c['delta'])}</td></tr>")
        out.append("</table><div class='muted'>🔒 = مخفية عن المحسّن.</div></div>")

    if base_text is not None and new_text is not None:
        out.append("<h2>فرق النص</h2><div class='card wrap'>" + render_diff(base_text, new_text) + "</div>")

    out.append("<h2>القرار</h2><div class='card'>")
    if p["status"] == "ready":
        label = "وافق وفعّل" if p["recommended"] else "أوافق رغم عدم التوصية"
        out.append(_form(p["id"], "approve", label, "green", key,
                         "سيتغير سلوك التطبيق عند كل المستخدمين فوراً. متأكد؟"))
        out.append(_form(p["id"], "reject", "ارفض", "red", key))
    elif p["status"] == "approved":
        out.append("<div class='muted'>مُفعَّل الآن.</div>")
        out.append(_form(p["id"], "rollback", "تراجع إلى النسخة السابقة", "grey", key, "تراجع إلى النسخة السابقة؟"))
    else:
        out.append("<div class='muted'>لا إجراء متاح.</div>")
    out.append("</div></body></html>")
    return "".join(out)


# ============================================================
# المسارات
# ============================================================

def _row_to_dict(r):
    keys = ["id", "prompt_name", "base_version", "new_version", "status", "evidence", "changes",
            "comparison", "base_run_id", "new_run_id", "base_score", "new_score", "recommended", "note"]
    return dict(zip(keys, r))


_COLS = ("id, prompt_name, base_version, new_version, status, evidence, changes, comparison, "
         "base_run_id, new_run_id, base_score, new_score, recommended, note")


def _load(pid):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute(f"SELECT {_COLS} FROM proposals WHERE id=%s", (pid,))
        row = c.fetchone()
        return _row_to_dict(row) if row else None
    finally:
        conn.close()


@bp.route("/admin/proposals", methods=["GET"])
def admin_proposals():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        pid = request.args.get("id")
        if pid:
            p = _load(int(pid))
            if not p:
                return jsonify({"ok": False, "error": "Not found"}), 404
            engine = _d["engine"]
            base_row = engine.get_version_row(p["prompt_name"], p["base_version"])
            new_row = engine.get_version_row(p["prompt_name"], p["new_version"]) if p["new_version"] else None
            if request.args.get("format") == "json":
                return jsonify({"ok": True, "proposal": p})
            return Response(render_detail(p, base_row["text"] if base_row else None,
                                          new_row["text"] if new_row else None, key),
                            mimetype="text/html; charset=utf-8")

        conn = _d["get_conn"]()
        try:
            c = conn.cursor()
            c.execute("""UPDATE proposals SET status='error', note='interrupted'
                         WHERE status='evaluating' AND created_at < NOW() - (%s || ' minutes')::interval""",
                      (str(STALE_MINUTES),))
            conn.commit()
            c.execute(f"SELECT {_COLS} FROM proposals ORDER BY id DESC LIMIT 20")
            rows = [_row_to_dict(r) for r in c.fetchall()]
            c.execute("SELECT DISTINCT prompt_name FROM eval_cases ORDER BY 1")
            names = [r[0] for r in c.fetchall()]
        finally:
            conn.close()
        prompts = []
        for n in names:
            a = _d["engine"].get_active(n)
            prompts.append({"name": n, "version": a["version"] if a else None})
        return Response(render_list(prompts, rows, request.args.get("msg"), key),
                        mimetype="text/html; charset=utf-8")
    except Exception as e:
        print("PROPOSALS PAGE ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed."}), 500


@bp.route("/admin/proposals/new", methods=["GET"])
def admin_proposals_new():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        pid = start_proposal(request.args.get("name", ""))
    except ValueError as e:
        return redirect(f"/admin/proposals?key={quote(key)}&msg={quote(str(e))}")
    except Exception as e:
        print("PROPOSAL START ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed."}), 500
    return redirect(f"/admin/proposals?key={quote(key)}&id={pid}")


@bp.route("/admin/proposals/decide", methods=["POST"])
def admin_proposals_decide():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        pid = int(request.form.get("id", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid id"}), 400
    action = request.form.get("action", "")
    p = _load(pid)
    if not p:
        return jsonify({"ok": False, "error": "Not found"}), 404
    engine = _d["engine"]
    name = p["prompt_name"]

    def back(msg=None):
        extra = f"&msg={quote(msg)}" if msg else ""
        return redirect(f"/admin/proposals?key={quote(key)}&id={pid}{extra}")

    try:
        if action == "approve":
            if p["status"] != "ready" or not p["new_version"]:
                return back("الاقتراح غير جاهز.")
            current = engine.get_active(name)
            if not current or current["version"] != p["base_version"]:
                return back("النسخة الفعّالة تغيّرت بعد هذا الاقتراح، فهو قديم. اطلب اقتراحاً جديداً.")
            if not engine.activate_version(name, p["new_version"]):
                return back("تعذّر التفعيل.")
            _set(pid, status="approved")
            _mark_decided(pid)
        elif action == "reject":
            if p["status"] != "ready":
                return back("لا يمكن رفض هذا الاقتراح الآن.")
            if p["new_version"]:
                engine.archive_draft(name, p["new_version"])
            _set(pid, status="rejected")
            _mark_decided(pid)
        elif action == "rollback":
            if p["status"] != "approved":
                return back("لا يوجد ما يُتراجع عنه.")
            if not engine.activate_version(name, p["base_version"]):
                return back("تعذّر التراجع.")
            _set(pid, status="rolled_back")
            _mark_decided(pid)
        else:
            return jsonify({"ok": False, "error": "Invalid action"}), 400
    except Exception as e:
        print("DECIDE ERROR:", repr(e))
        return back("حدث خطأ.")
    return back()


def _mark_decided(pid):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("UPDATE proposals SET decided_at = NOW() WHERE id = %s", (pid,))
        conn.commit()
    finally:
        conn.close()
