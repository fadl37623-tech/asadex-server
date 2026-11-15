"""
SciGo Evaluator (المرحلة ج): الاختبارات الثابتة.

يشغّل حالات eval_cases.py على نسخة محددة من prompt (حتى لو لم تكن فعّالة)،
ويقيّم كل ناتج بفحوص آلية + حَكَم ذكاء اصطناعي، ثم يحفظ الدرجة.
لا يغيّر أي prompt ولا يلمس التطبيق.

الصفحة: /admin/eval?key=ADMIN_KEY
"""

import concurrent.futures
import html
import json
import os
import re
import secrets
import threading
import uuid
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, redirect, request

from eval_cases import CASES

bp = Blueprint("evaluator", __name__)
_deps = {}

WORKERS = 3
JUDGE_WEIGHT = 2          # وزن الحَكَم مقابل كل فحص آلي
PASS_SCORE = 0.8

_ARABIC = re.compile(r"[\u0600-\u06FF]")
_LATIN = re.compile(r"[A-Za-z]")
_FINAL_RE = re.compile(r"^\s*(?:\*\*)?\s*(?:final answer|الإجابة النهائية|الاجابة النهائية)", re.I | re.M)
_PRACTICE = (
    "solve", "problem", "practice", "exercise", "quiz", "test", "worksheet",
    "drill", "questions", "حل", "تمرين", "تمارين", "مسائل", "مسألة", "تدرب",
    "اختبر", "أسئلة", "اسئلة",
)


def init(app, get_conn, run_internal, run_version, get_active):
    _deps.update(get_conn=get_conn, run_internal=run_internal,
                 run_version=run_version, get_active=get_active)
    _create_tables()
    _seed_cases()
    app.register_blueprint(bp)


def _create_tables():
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS eval_cases (
                case_key TEXT PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                user_input TEXT NOT NULL,
                variables JSONB NOT NULL DEFAULT '{}'::jsonb,
                checks JSONB NOT NULL DEFAULT '{}'::jsonb,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS eval_runs (
                run_id TEXT PRIMARY KEY,
                prompt_name TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'running',
                n_cases INTEGER NOT NULL DEFAULT 0,
                n_errors INTEGER NOT NULL DEFAULT 0,
                score REAL,
                pass_rate REAL,
                note TEXT,
                started_at TIMESTAMP NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMP
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS eval_results (
                id BIGSERIAL PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_key TEXT NOT NULL,
                score REAL,
                passed BOOLEAN,
                errored BOOLEAN NOT NULL DEFAULT FALSE,
                checks JSONB,
                judge JSONB,
                output TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS eval_results_run ON eval_results(run_id)")
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("EVAL TABLES ERROR:", repr(e))
    finally:
        if conn:
            conn.close()


def _seed_cases():
    """الحالات مملوكة للكود: تُحدَّث من eval_cases.py عند كل تشغيل."""
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        for case in CASES:
            c.execute(
                """
                INSERT INTO eval_cases (case_key, prompt_name, user_input, variables, checks)
                VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
                ON CONFLICT (case_key) DO UPDATE SET
                    prompt_name = EXCLUDED.prompt_name,
                    user_input = EXCLUDED.user_input,
                    variables = EXCLUDED.variables,
                    checks = EXCLUDED.checks,
                    updated_at = NOW()
                """,
                (case["key"], case["prompt"], case["input"],
                 json.dumps(case.get("variables") or {}, ensure_ascii=False),
                 json.dumps(case["checks"], ensure_ascii=False)),
            )
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("EVAL SEED ERROR:", repr(e))
    finally:
        if conn:
            conn.close()


def _admin_ok():
    expected = os.environ.get("ADMIN_KEY", "")
    supplied = request.headers.get("X-Admin-Key") or request.args.get("key", "")
    return bool(expected) and secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


# ============================================================
# الفحوص الآلية: كل دالة ترجع (نجح؟, تفصيل)
# ============================================================

def _json(output):
    text = re.sub(r"^`(?:json)?\s*|\s*```$", "", (output or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON")
    return json.loads(text[start:end + 1])


def _lang_ok(text, want):
    ar = len(_ARABIC.findall(text))
    total = ar + len(_LATIN.findall(text))
    if total == 0:
        return False, "no letters"
    share = ar / total
    ok = share >= 0.3 if want == "ar" else share <= 0.05
    return ok, f"arabic share {share:.0%}"


def check_language(output, want):
    return _lang_ok(output, want)


def check_forbidden(output, items):
    found = [s for s in items if s in output]
    return (not found), ("found: " + " ".join(found) if found else "ok")


def check_final_answer(output, want):
    present = bool(_FINAL_RE.search(output))
    return present == bool(want), ("has Final answer line" if present else "no Final answer line")


def check_no_final_answer(output, want):
    present = bool(_FINAL_RE.search(output))
    return (not present) == bool(want), ("has Final answer line" if present else "no Final answer line")


def check_max_cards(output, limit):
    n = len(re.findall(r"^:::[a-z]+", output, flags=re.M))
    return n <= limit, f"{n} cards"


def check_arabic_formula_lines(output, want):
    """قاعدة الـ prompt: لا معادلة داخل جملة عربية (المعادلة في سطر مستقل)."""
    bad = 0
    for line in output.split("\n"):
        if _ARABIC.search(line) and "=" in line and not _FINAL_RE.search(line):
            bad += 1
    return (bad == 0) == bool(want), f"{bad} mixed lines"


def _plan_text(data):
    parts = [str(data.get("title") or "")]
    for d in data.get("days") or []:
        if isinstance(d, dict):
            parts.append(str(d.get("topic") or ""))
            parts.extend(str(t) for t in (d.get("tasks") or []))
    return " ".join(parts)


def check_plan(output, arg):
    try:
        data = _json(output)
    except Exception:
        return False, "not valid JSON"
    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, list):
        return False, "no days list"
    problems = []
    if "days" in arg:
        lo, hi = arg["days"]
        if not lo <= len(days) <= hi:
            problems.append(f"{len(days)} days (wanted {lo}-{hi})")
    for i, d in enumerate(days, 1):
        if not isinstance(d, dict) or not str(d.get("topic") or "").strip():
            problems.append(f"day {i} has no topic")
            continue
        tasks = d.get("tasks")
        if not isinstance(tasks, list):
            problems.append(f"day {i} has no tasks")
            continue
        if len(tasks) < arg.get("min_tasks", 1):
            problems.append(f"day {i} has {len(tasks)} tasks")
        if arg.get("practice_each_day"):
            joined = " ".join(str(t) for t in tasks).lower()
            if not any(k in joined for k in _PRACTICE):
                problems.append(f"day {i} has no practice task")
    if arg.get("language"):
        ok, detail = _lang_ok(_plan_text(data), arg["language"])
        if not ok:
            problems.append("language: " + detail)
    return (not problems), ("; ".join(problems[:4]) if problems else "ok")


def check_quiz(output, arg):
    try:
        data = _json(output)
    except Exception:
        return False, "not valid JSON"
    qs = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(qs, list) or not qs:
        return False, "no questions"
    problems = []
    if "count" in arg and len(qs) != arg["count"]:
        problems.append(f"{len(qs)} questions (wanted {arg['count']})")
    texts = []
    for i, q in enumerate(qs, 1):
        opts = q.get("options") if isinstance(q, dict) else None
        if not (isinstance(opts, list) and len(opts) == 4 and all(isinstance(o, str) and o.strip() for o in opts)):
            problems.append(f"q{i} options invalid")
            continue
        if q.get("answer") not in (0, 1, 2, 3):
            problems.append(f"q{i} answer index invalid")
        if len({o.strip().lower() for o in opts}) < 4:
            problems.append(f"q{i} duplicate options")
        texts.append(str(q.get("q") or "") + " " + " ".join(opts))
    if arg.get("language"):
        ok, detail = _lang_ok(" ".join(texts), arg["language"])
        if not ok:
            problems.append("language: " + detail)
    return (not problems), ("; ".join(problems[:4]) if problems else "ok")


def check_json_intent(output, want):
    try:
        data = _json(output)
    except Exception:
        return False, "not valid JSON"
    got = data.get("intent") if isinstance(data, dict) else None
    return got == want, f"intent={got}"


def check_unsupported_key(output, want):
    try:
        data = _json(output)
    except Exception:
        return False, "not valid JSON"
    has = isinstance(data, dict) and bool(data.get("unsupported"))
    return has == bool(want), ("has unsupported key" if has else "no unsupported key")


CHECKS = {
    "language": check_language,
    "forbidden": check_forbidden,
    "final_answer": check_final_answer,
    "no_final_answer": check_no_final_answer,
    "max_cards": check_max_cards,
    "arabic_formula_lines": check_arabic_formula_lines,
    "plan": check_plan,
    "quiz": check_quiz,
    "json_intent": check_json_intent,
    "unsupported_key": check_unsupported_key,
}


def run_checks(output, checks):
    results = []
    for name, arg in checks.items():
        fn = CHECKS.get(name)
        if not fn:
            continue
        try:
            ok, detail = fn(output, arg)
        except Exception as e:
            ok, detail = False, "check error: " + str(e)[:80]
        results.append({"check": name, "passed": bool(ok), "detail": detail})
    return results


# ============================================================
# الحَكَم
# ============================================================

def run_judge(task, output, spec):
    payload = {
        "task": task,
        "output": output[:6000],
        "rubric": spec.get("rubric", ""),
        "expected": spec.get("expected"),
    }
    text, _, _ = _deps["run_internal"]("eval_judge", json.dumps(payload, ensure_ascii=False))
    data = _json(text)
    score = data.get("score")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise ValueError("judge returned no score")
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    return {
        "score": max(0.0, min(1.0, float(score))),
        "issues": [str(i)[:200] for i in issues[:5]],
    }


def score_case(check_results, judge):
    """درجة الحالة = (الفحوص الناجحة + وزن×درجة الحكم) / (عدد الفحوص + الوزن)."""
    n = len(check_results)
    passed = sum(1 for c in check_results if c["passed"])
    if judge is not None:
        return (passed + JUDGE_WEIGHT * judge["score"]) / (n + JUDGE_WEIGHT)
    return passed / n if n else 0.0
def run_case(prompt_name, version, case):
    """case: dict فيه case_key, user_input, variables, checks."""
    base = {"case_key": case["case_key"], "errored": False, "score": None,
            "passed": None, "checks": [], "judge": None, "output": ""}
    try:
        output = _deps["run_version"](
            prompt_name, version, case["user_input"], case.get("variables") or {}
        )
    except Exception as e:
        base.update(errored=True, output="ERROR: " + str(e)[:300])
        return base

    base["output"] = output[:4000]
    checks = dict(case["checks"])
    judge_spec = checks.pop("judge", None)
    results = run_checks(output, checks)

    judge = None
    if judge_spec:
        try:
            judge = run_judge(case["user_input"], output, judge_spec)
        except Exception as e:
            base.update(errored=True, checks=results,
                        judge={"error": str(e)[:200]})
            return base

    score = score_case(results, judge)
    base.update(score=round(score, 3), passed=score >= PASS_SCORE,
                checks=results, judge=judge)
    return base


# ============================================================
# تشغيل تقييم كامل (في خيط خلفي)
# ============================================================

def _load_cases(prompt_name):
    conn = _deps["get_conn"]()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT case_key, user_input, variables, checks FROM eval_cases "
            "WHERE prompt_name=%s ORDER BY case_key", (prompt_name,))
        return [{"case_key": k, "user_input": u, "variables": v or {}, "checks": ch or {}}
                for k, u, v, ch in c.fetchall()]
    finally:
        conn.close()


def _finish_run(run_id, status, results=None, note=None):
    scored = [r for r in (results or []) if not r["errored"]]
    score = sum(r["score"] for r in scored) / len(scored) if scored else None
    pass_rate = sum(1 for r in scored if r["passed"]) / len(scored) if scored else None
    conn = _deps["get_conn"]()
    try:
        c = conn.cursor()
        for r in results or []:
            c.execute(
                """
                INSERT INTO eval_results (run_id, case_key, score, passed, errored, checks, judge, output)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
                """,
                (run_id, r["case_key"], r["score"], r["passed"], r["errored"],
                 json.dumps(r["checks"], ensure_ascii=False),
                 json.dumps(r["judge"], ensure_ascii=False) if r["judge"] is not None else None,
                 r["output"]),
            )
        c.execute(
            """
            UPDATE eval_runs SET status=%s, n_cases=%s, n_errors=%s, score=%s,
                   pass_rate=%s, note=%s, finished_at=NOW() WHERE run_id=%s
            """,
            (status, len(results or []), sum(1 for r in (results or []) if r["errored"]),
             score, pass_rate, note, run_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _worker(run_id, prompt_name, version):
    try:
        cases = _load_cases(prompt_name)
        if not cases:
            _finish_run(run_id, "error", [], "no cases for this prompt")
            return
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(lambda cs: run_case(prompt_name, version, cs), cases))
        _finish_run(run_id, "done", results)
    except Exception as e:
        print("EVAL WORKER ERROR:", repr(e))
        try:
            _finish_run(run_id, "error", [], str(e)[:200])
        except Exception as e2:
            print("EVAL FINISH ERROR:", repr(e2))


def start_run(prompt_name, version):
    """يبدأ تقييماً في الخلفية ويرجع run_id."""
    if not _load_cases(prompt_name):
        raise ValueError("No evaluation cases for this prompt.")
    run_id = uuid.uuid4().hex[:12]
    conn = _deps["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO eval_runs (run_id, prompt_name, version) VALUES (%s, %s, %s)",
                  (run_id, prompt_name, version))
        conn.commit()
    finally:
        conn.close()
    threading.Thread(target=_worker, args=(run_id, prompt_name, version), daemon=True).start()
    return run_id


# ============================================================
# العرض
# ============================================================

def _e(x):
    return html.escape(str(x if x is not None else ""))


CSS = """<style>
body{font-family:system-ui,-apple-system,Segoe UI,Tahoma,sans-serif;margin:0;padding:14px;background:#f5f6f8;color:#1c1c1e;line-height:1.55}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px;color:#1565c0}
.card{background:#fff;border-radius:14px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.muted{color:#666;font-size:13px}.ok{background:#e8f5e9;border:1px solid #81c784}.warn{background:#fff4e5;border:1px solid #ffb74d}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;text-align:right;border-bottom:1px solid #eee;vertical-align:top}
th{color:#555;font-weight:600}.bad{color:#c62828;font-weight:600}.good{color:#2e7d32;font-weight:600}
a.btn{display:inline-block;padding:7px 14px;background:#1565c0;color:#fff;border-radius:18px;text-decoration:none;font-size:13px}
a{color:#1565c0}.wrap{overflow-x:auto}pre{white-space:pre-wrap;direction:ltr;text-align:left;font-size:12px;background:#f0f0f0;padding:8px;border-radius:8px}
</style>"""


def _pct(x):
    return f"{round(x * 100)}%" if x is not None else "-"


def render_list(prompts, runs, message, key):
    k = quote(key)
    out = [f"<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>Asadex Eval</title>{CSS}</head><body>",
           "<h1>الاختبارات الثابتة</h1>",
           "<div class='muted'>نفس الأسئلة تُشغَّل على أي نسخة prompt لمقارنة الجودة قبل التعديل وبعده.</div>"]
    if message:
        out.append(f"<div class='card ok'>{_e(message)}</div>")

    out.append("<h2>الـ prompts القابلة للاختبار</h2><div class='card wrap'><table>"
               "<tr><th>Prompt</th><th>النسخة الفعّالة</th><th>حالات</th><th>آخر درجة لها</th><th></th></tr>")
    for p in prompts:
        last = f"{_pct(p['last_score'])} ({_e(p['last_status'])})" if p["last_status"] else "لم تُختبر"
        link = (f"<a class='btn' href='/admin/eval/start?key={k}&name={quote(p['name'])}"
                f"&version={p['version']}'>شغّل الاختبار</a>") if p["version"] else "-"
        out.append(f"<tr><td>{_e(p['name'])}</td><td>v{_e(p['version'])}</td><td>{p['cases']}</td>"
                   f"<td>{last}</td><td>{link}</td></tr>")
    out.append("</table></div>")

    out.append("<h2>آخر التشغيلات</h2>")
    if runs:
        out.append("<div class='card wrap'><table><tr><th>Prompt</th><th>نسخة</th><th>الحالة</th>"
                   "<th>الدرجة</th><th>نجاح</th><th>أخطاء</th><th></th></tr>")
        for r in runs:
            status = _e(r["status"])
            if r["status"] == "running":
                status = "جارٍ... (حدّث الصفحة)"
            cls = "good" if (r["score"] or 0) >= PASS_SCORE else "bad"
            out.append(f"<tr><td>{_e(r['prompt'])}</td><td>v{r['version']}</td><td>{status}</td>"
                       f"<td class='{cls}'>{_pct(r['score'])}</td><td>{_pct(r['pass_rate'])}</td>"
                       f"<td>{r['n_errors']}</td>"
                       f"<td><a href='?key={k}&run={_e(r['run_id'])}'>تفاصيل</a></td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card muted'>لم يُشغَّل أي اختبار بعد.</div>")
    out.append("</body></html>")
    return "".join(out)
def render_run(run, results, key):
    k = quote(key)
    out = [f"<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
           f"<title>Eval run</title>{CSS}</head><body>",
           f"<h1>{_e(run['prompt'])} v{run['version']}</h1>",
           f"<div class='muted'>الحالة: {_e(run['status'])} | الدرجة: {_pct(run['score'])} | "
           f"نجاح: {_pct(run['pass_rate'])} | حالات: {run['n_cases']} | أخطاء توليد: {run['n_errors']}</div>",
           f"<p><a href='?key={k}'>← رجوع</a></p>"]
    if run.get("note"):
        out.append(f"<div class='card warn'>{_e(run['note'])}</div>")
    if run["status"] == "running":
        out.append("<div class='card'>الاختبار ما زال يعمل، حدّث الصفحة بعد دقيقة.</div>")
    for r in results:
        if r["errored"]:
            head = "<span class='bad'>خطأ في التوليد (لا يُحتسب)</span>"
        else:
            cls = "good" if r["passed"] else "bad"
            head = f"<span class='{cls}'>{_pct(r['score'])}</span>"
        fails = [c for c in (r["checks"] or []) if not c["passed"]]
        lines = [f"<b>{_e(r['case_key'])}</b> {head}"]
        for c in fails:
            lines.append(f"<div class='muted'>✗ {_e(c['check'])}: {_e(c['detail'])}</div>")
        jd = r.get("judge") or {}
        if jd.get("issues"):
            for i in jd["issues"]:
                lines.append(f"<div class='muted'>⚖ {_e(i)}</div>")
        if jd.get("error"):
            lines.append(f"<div class='muted'>⚖ خطأ في الحكم: {_e(jd['error'])}</div>")
        lines.append(f"<details><summary class='muted'>الناتج</summary><pre>{_e((r['output'] or '')[:1500])}</pre></details>")
        out.append("<div class='card'>" + "".join(lines) + "</div>")
    out.append("</body></html>")
    return "".join(out)


def _prompts_overview():
    conn = _deps["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("SELECT prompt_name, COUNT(*) FROM eval_cases GROUP BY prompt_name ORDER BY prompt_name")
        items = c.fetchall()
        out = []
        for name, n in items:
            active = _deps["get_active"](name)
            version = active["version"] if active else None
            last_score = last_status = None
            if version:
                c.execute(
                    "SELECT status, score FROM eval_runs WHERE prompt_name=%s AND version=%s "
                    "ORDER BY started_at DESC LIMIT 1", (name, version))
                row = c.fetchone()
                if row:
                    last_status, last_score = row
            out.append({"name": name, "version": version, "cases": n,
                        "last_score": last_score, "last_status": last_status})
        return out
    finally:
        conn.close()


def _recent_runs(limit=15):
    conn = _deps["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("""UPDATE eval_runs SET status='error', note='interrupted (server restarted)'
                     WHERE status='running' AND started_at < NOW() - INTERVAL '30 minutes'""")
        conn.commit()
        c.execute("""SELECT run_id, prompt_name, version, status, n_cases, n_errors, score, pass_rate,
                            note, started_at FROM eval_runs ORDER BY started_at DESC LIMIT %s""", (limit,))
        return [{"run_id": r[0], "prompt": r[1], "version": r[2], "status": r[3], "n_cases": r[4],
                 "n_errors": r[5], "score": r[6], "pass_rate": r[7], "note": r[8],
                 "started_at": r[9].isoformat() if r[9] else None} for r in c.fetchall()]
    finally:
        conn.close()


def _run_details(run_id):
    conn = _deps["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("""SELECT run_id, prompt_name, version, status, n_cases, n_errors, score, pass_rate, note
                    FROM eval_runs WHERE run_id=%s""", (run_id,))
        r = c.fetchone()
        if not r:
            return None, []
        run = {"run_id": r[0], "prompt": r[1], "version": r[2], "status": r[3], "n_cases": r[4],
               "n_errors": r[5], "score": r[6], "pass_rate": r[7], "note": r[8]}
        c.execute("""SELECT case_key, score, passed, errored, checks, judge, output
                     FROM eval_results WHERE run_id=%s ORDER BY case_key""", (run_id,))
        results = [{"case_key": k, "score": s, "passed": p, "errored": e,
                    "checks": ch or [], "judge": j, "output": o}
                   for k, s, p, e, ch, j, o in c.fetchall()]
        return run, results
    finally:
        conn.close()


@bp.route("/admin/eval", methods=["GET"])
def admin_eval():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.args.get("key", "")
    try:
        run_id = request.args.get("run")
        if run_id:
            run, results = _run_details(run_id)
            if not run:
                return jsonify({"ok": False, "error": "Run not found"}), 404
            if request.args.get("format") == "json":
                return jsonify({"ok": True, "run": run, "results": results})
            return Response(render_run(run, results, key), mimetype="text/html; charset=utf-8")

        prompts, runs = _prompts_overview(), _recent_runs()
        if request.args.get("format") == "json":
            return jsonify({"ok": True, "prompts": prompts, "runs": runs})
        message = request.args.get("msg")
        return Response(render_list(prompts, runs, message, key), mimetype="text/html; charset=utf-8")
    except Exception as e:
        print("EVAL PAGE ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed."}), 500


@bp.route("/admin/eval/start", methods=["GET"])
def admin_eval_start():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    name = request.args.get("name", "")
    try:
        version = int(request.args.get("version", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid version"}), 400
    try:
        run_id = start_run(name, version)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        print("EVAL START ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed."}), 500
    return redirect(f"/admin/eval?key={quote(request.args.get('key', ''))}&run={run_id}")
