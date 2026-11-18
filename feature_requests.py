"""
SciGo Feature Requests (المرحلتان 2 و3): محرك اكتشاف الميزات.

يلتقط إشارتين:
  1) رفض التطبيق لطلب (موضوع/ميزة غير مدعومة) تلقائياً من ردود الذكاء الاصطناعي.
  2) مشكلات التقييمات السلبية التي علاجها ليس prompt (code/ui/data) من analyzer.
     (ما علاجه prompt يذهب إلى improver كما هو.)

لا يقترح شيئاً إلا إذا بلغ عدد المستخدمين المختلفين العتبة (FEATURE_MIN_USERS، الافتراضي 1000).
عند بلوغها يكتب اقتراح ميزة كاملاً (الفائدة، المخرجات، التصميم، التكلفة)
وأنت وحدك توافق أو ترفض. لا ينفّذ شيئاً في التطبيق.

الصفحة: /admin/features?key=ADMIN_KEY
"""

import html
import json
import os
import re
import secrets
import threading
import time
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, redirect, request

bp = Blueprint("feature_requests", __name__)
_d = {}

MIN_USERS = int(os.environ.get("FEATURE_MIN_USERS", "1000"))
CLASSIFY_BATCH = 20
MAX_BATCHES_PER_RUN = 5
MAX_PROPOSALS_PER_RUN = 3
CAPTURE_PROMPTS = {
    "chat_concise", "chat_detailed", "plan_create", "plan_create_lecture",
    "quiz_make", "quiz_make_lecture",
}

# ============================================================
# Prompts (تُضاف إلى prompt_seeds.py، انظر الأسفل)
# ============================================================

CLASSIFY_SYSTEM = """You triage requests that Asadex could not fulfil. Asadex is an educational app that currently supports Mathematics, Physics and Chemistry only.

Input JSON: {"existing_topics": [str], "items": [{"id": int, "text": str}]}
Each item is something a student asked for that the app declined or could not do.

For EACH item return:
- "is_request": true if it is a genuine, understandable request for a subject, topic or feature the app lacks. false for gibberish, spam, abuse, or if it is actually inside Mathematics, Physics or Chemistry.
- "topic_key": lowercase English snake_case, 2-5 words, naming the missing subject or feature (e.g. "biology_support", "history_study_plans", "voice_questions"). If an existing topic means the same thing, REUSE it exactly. Empty string if is_request is false.
- "title": short Arabic title of the missing subject/feature.

Return JSON only, no markdown: {"items":[{"id":1,"is_request":true,"topic_key":"...","title":"..."}]}
One result per input id. Never invent details.
"""

PROPOSAL_SYSTEM = """You are a product analyst for Asadex, an educational app (currently Mathematics, Physics, Chemistry) with: chat Q&A, study plans, quizzes, lecture summaries, reports with sources, diagrams. The backend is Flask + Gemini.

Input JSON: {"topic_key": str, "title": str, "distinct_users": int, "signals": int, "samples": [str]}
Many different users asked for this, or complained about it. Write a feature proposal for the app owner, in Arabic, concrete and honest. Do not exaggerate benefits and do not invent numbers beyond the input.

Return JSON only, no markdown:
{"title": str,
 "why": str,                      // evidence-based reason, 1-2 sentences
 "what_it_does": str,             // what the student gets
 "outputs": str,                  // what the output looks like (format, cards, JSON, etc.)
 "design": str,                   // UI/UX in the app + which backend parts change (prompts, endpoints, tables)
 "cost": {"level": "low"|"medium"|"high", "notes": str},   // build effort + running Gemini cost
 "benefit": {"level": "low"|"medium"|"high", "notes": str},
 "priority": "high"|"medium"|"low",
 "risks": [str]}
"""


PLAN_SYSTEM = """You are a senior backend engineer planning ONE approved feature for Asadex, an educational app (Flask + PostgreSQL on Render, Gemini via prompts stored in the database, a mobile app client).

Input JSON: {"proposal": {...approved feature spec...}, "code_index": {"file.py": {"routes": [...], "functions": [...]}}}
code_index lists the REAL server files, their routes and top-level functions. The mobile app source is NOT visible to you.
Write an implementation plan. Rules:
- Only reference server files that exist in code_index for edits. New files go under action "create".
- Prefer the smallest change: reuse existing prompts, tables and helpers. Prefer a new prompt version over new code when the feature is a behavior change.
- Anything that needs mobile app changes must be listed separately in "app_changes", never hidden in server steps.
- Never invent functions or files. If unsure, say so in risks.
- Write the plan in Arabic, except "implementation_brief" which is English and self-contained so a coding assistant can implement it without other context.

Return JSON only, no markdown:
{"summary": str,
 "files": [{"path": str, "action": "edit"|"create", "changes": str}],
 "steps": [str],
 "new_tables": [str],
 "new_endpoints": [str],
 "app_changes": [str],
 "tests": [str],
 "rollout": str,
 "rollback": str,
 "estimate": str,
 "risks": [str],
 "implementation_brief": str}
"""


# ============================================================
# Setup
# ============================================================

def init(app, get_conn, run_internal):
    _d.update(get_conn=get_conn, run_internal=run_internal)
    _create_tables()
    app.register_blueprint(bp)
    if os.environ.get("FEATURE_AUTO") == "1":
        threading.Thread(target=_auto_loop, daemon=True).start()


def _create_tables():
    conn = None
    try:
        conn = _d["get_conn"]()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS feature_signals (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                ref_id BIGINT,
                user_id INTEGER,
                prompt_name TEXT,
                raw_text TEXT,
                topic_key TEXT,
                title TEXT,
                is_request BOOLEAN,
                classified BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE (source, ref_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS feature_signals_topic ON feature_signals(topic_key)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS feature_proposals (
                id SERIAL PRIMARY KEY,
                topic_key TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready'
                    CHECK (status IN ('ready','approved','rejected','error')),
                users_count INTEGER,
                signals_count INTEGER,
                spec JSONB,
                note TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                decided_at TIMESTAMP
            )
        """)
        c.execute("ALTER TABLE feature_proposals ADD COLUMN IF NOT EXISTS plan JSONB")
        c.execute("ALTER TABLE feature_proposals ADD COLUMN IF NOT EXISTS plan_status TEXT")
        c.execute("ALTER TABLE feature_proposals ADD COLUMN IF NOT EXISTS plan_note TEXT")
        # يزرع prompt كتابة الخطة تلقائياً (لا حاجة لتعديل prompt_seeds.py)
        c.execute("SELECT 1 FROM prompts WHERE name='feature_plan_write' LIMIT 1")
        if not c.fetchone():
            c.execute(
                "INSERT INTO prompts (name, version, text, config, status, source, notes, activated_at) "
                "VALUES ('feature_plan_write', 1, %s, %s::jsonb, 'active', 'seed', 'initial version', NOW())",
                (PLAN_SYSTEM, json.dumps({"thinking_budget": 2048})),
            )
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("FEATURE TABLES ERROR:", repr(e))
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
# الالتقاط (يُستدعى من prompt_engine بعد كل رد ناجح)
# ============================================================

def _last_user_text(contents):
    if isinstance(contents, str):
        return contents
    if isinstance(contents, list):
        for item in reversed(contents):
            if isinstance(item, dict) and item.get("role") == "user":
                for part in item.get("parts") or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        return part["text"]
    return ""


def _json(text):
    text = re.sub(r"^(?:json)?\s*|\s*``$", "", (text or "").strip())
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError("no JSON")
    return json.loads(text[s:e + 1])


def looks_like_refusal(prompt_name, answer):
    """كشف رخيص (بلا استدعاء نموذج). الأخطاء الإيجابية يصفّيها المصنّف لاحقاً."""
    if not answer:
        return False
    if prompt_name.startswith(("plan_", "quiz_")):
        try:
            data = _json(answer)
            return isinstance(data, dict) and bool(data.get("unsupported"))
        except Exception:
            return False
    if prompt_name.startswith("chat_"):
        low = answer.lower()
        if "final answer" in low or len(answer) > 700:
            return False
        en = "mathematics" in low and "physics" in low and "chemistry" in low
        ar = "الرياضيات" in answer and "الفيزياء" in answer and "الكيمياء" in answer
        return en or ar
    return False


def capture(user_id, prompt_name, contents, answer):
    """آمن: لا يرمي أخطاء ولا يبطئ الرد (إدخال واحد فقط عند الرفض)."""
    try:
        if prompt_name not in CAPTURE_PROMPTS or not looks_like_refusal(prompt_name, answer):
            return
        text = _last_user_text(contents).strip()[:300]
        if not text:
            return
        conn = _d["get_conn"]()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO feature_signals (source, user_id, prompt_name, raw_text) "
                "VALUES ('refusal', %s, %s, %s)",
                (user_id, prompt_name, text),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print("FEATURE CAPTURE ERROR:", repr(e))


# ============================================================
# المعالجة: مزامنة التقييمات، التصنيف، الاقتراح
# ============================================================

def sync_ratings():
    """مشكلات التقييمات التي علاجها ليس prompt تصبح إشارات ميزات."""
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO feature_signals
                (source, ref_id, user_id, prompt_name, raw_text, topic_key, title, is_request, classified)
            SELECT 'rating', a.feedback_id, f.user_id, f.prompt_name, a.summary,
                   CONCAT(f.feature, '_', a.category), a.summary, TRUE, TRUE
            FROM feedback_analysis a
            JOIN answer_feedback f ON f.id = a.feedback_id
            WHERE a.cause IN ('code', 'ui', 'data')
            ON CONFLICT (source, ref_id) DO NOTHING
        """)
        n = c.rowcount
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def parse_classification(text, valid_ids):
    data = _json(text)
    out = {}
    for it in data.get("items", []):
        if not isinstance(it, dict):
            continue
        i = it.get("id")
        if isinstance(i, bool) or not isinstance(i, int) or i not in valid_ids:
            continue
        ok = bool(it.get("is_request"))
        key = re.sub(r"[^a-z0-9_]", "", str(it.get("topic_key") or "").lower())[:50]
        if ok and not key:
            ok = False
        out.setdefault(i, (ok, key if ok else None, str(it.get("title") or "")[:120] or None))
    return out
def classify_pending():
    done = 0
    for _ in range(MAX_BATCHES_PER_RUN):
        conn = _d["get_conn"]()
        try:
            c = conn.cursor()
            c.execute("SELECT id, raw_text FROM feature_signals "
                      "WHERE NOT classified AND source='refusal' ORDER BY id LIMIT %s",
                      (CLASSIFY_BATCH,))
            rows = c.fetchall()
            if not rows:
                break
            c.execute("SELECT topic_key FROM feature_signals WHERE is_request AND topic_key IS NOT NULL "
                      "GROUP BY topic_key ORDER BY COUNT(*) DESC LIMIT 40")
            existing = [r[0] for r in c.fetchall()]
            payload = {"existing_topics": existing,
                       "items": [{"id": i, "text": t or ""} for i, t in rows]}
            text, _, _ = _d["run_internal"]("request_classify",
                                            json.dumps(payload, ensure_ascii=False))
            results = parse_classification(text, {r[0] for r in rows})
            for i, (ok, key, title) in results.items():
                c.execute("UPDATE feature_signals SET classified=TRUE, is_request=%s, "
                          "topic_key=%s, title=%s WHERE id=%s", (ok, key, title, i))
            conn.commit()
            done += len(results)
            if not results:
                break
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return done


def topics():
    """كل المواضيع مع عدد المستخدمين المختلفين وحالة آخر اقتراح."""
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT s.topic_key, COUNT(DISTINCT s.user_id), COUNT(*),
                   (ARRAY_AGG(s.title) FILTER (WHERE s.title IS NOT NULL))[1],
                   (ARRAY_AGG(s.source ORDER BY s.id DESC))[1]
            FROM feature_signals s
            WHERE s.classified AND s.is_request AND s.topic_key IS NOT NULL
            GROUP BY s.topic_key ORDER BY 2 DESC, 3 DESC LIMIT 40
        """)
        rows = c.fetchall()
        out = []
        for key, users, n, title, source in rows:
            c.execute("SELECT id, status, users_count FROM feature_proposals "
                      "WHERE topic_key=%s ORDER BY id DESC LIMIT 1", (key,))
            p = c.fetchone()
            out.append({"key": key, "users": users, "signals": n, "title": title or key,
                        "source": source,
                        "proposal": {"id": p[0], "status": p[1], "users": p[2]} if p else None})
        return out
    finally:
        conn.close()


def _should_propose(t):
    if t["users"] < MIN_USERS:
        return False
    p = t["proposal"]
    if p is None or p["status"] == "error":
        return True
    if p["status"] == "rejected":                       # يُعاد فقط إذا تضاعف الطلب
        return t["users"] >= 2 * (p["users"] or MIN_USERS)
    return False


def parse_proposal(text):
    data = _json(text)
    need = ("title", "why", "what_it_does", "outputs", "design")
    for k in need:
        if not isinstance(data.get(k), str) or not data[k].strip():
            raise ValueError("proposal missing " + k)
    for k in ("cost", "benefit"):
        v = data.get(k)
        if not isinstance(v, dict) or v.get("level") not in ("low", "medium", "high"):
            raise ValueError("proposal bad " + k)
        v["notes"] = str(v.get("notes") or "")[:400]
    if data.get("priority") not in ("high", "medium", "low"):
        data["priority"] = "medium"
    data["risks"] = [str(r)[:200] for r in (data.get("risks") or [])][:6]
    return data


def make_proposal(t):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("SELECT raw_text FROM feature_signals WHERE topic_key=%s AND raw_text IS NOT NULL "
                  "ORDER BY id DESC LIMIT 8", (t["key"],))
        samples = [(r[0] or "")[:200] for r in c.fetchall()]
    finally:
        conn.close()
    payload = {"topic_key": t["key"], "title": t["title"], "distinct_users": t["users"],
               "signals": t["signals"], "samples": samples}
    status, spec, note = "ready", None, None
    try:
        text, _, _ = _d["run_internal"]("feature_proposal_write",
                                        json.dumps(payload, ensure_ascii=False))
        spec = parse_proposal(text)
    except Exception as e:
        status, note = "error", ("فشل كتابة الاقتراح: " + str(e))[:300]

    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("INSERT INTO feature_proposals (topic_key, status, users_count, signals_count, spec, note) "
                  "VALUES (%s, %s, %s, %s, %s::jsonb, %s) RETURNING id",
                  (t["key"], status, t["users"], t["signals"],
                   json.dumps(spec, ensure_ascii=False) if spec else None, note))
        pid = c.fetchone()[0]
        conn.commit()
        return pid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def process():
    """دورة كاملة. ترجع ملخصاً نصياً."""
    synced = sync_ratings()
    classified = classify_pending()
    made = 0
    for t in topics():
        if made >= MAX_PROPOSALS_PER_RUN:
            break
        if _should_propose(t):
            make_proposal(t)
            made += 1
    return f"إشارات تقييم جديدة: {synced} | طلبات صُنّفت: {classified} | اقتراحات جديدة: {made}"


def _auto_loop():
    time.sleep(120)
    while True:
        try:
            print("FEATURE AUTO:", process())
        except Exception as e:
            print("FEATURE AUTO ERROR:", repr(e))
        time.sleep(int(os.environ.get("FEATURE_LOOP_MINUTES", "360")) * 60)


# ============================================================
# المرحلة 4: خطة التنفيذ (كتابة فقط، لا تعديل للكود)
# ============================================================

def code_index(max_chars=9000):
    """فهرس حقيقي لملفات السيرفر (ملف ← مساراته ودوالّه) ليبني النموذج خطته عليه."""
    import ast
    base = os.path.dirname(os.path.abspath(__file__))
    index = {}
    for fn in sorted(os.listdir(base)):
        if not fn.endswith(".py"):
            continue
        try:
            with open(os.path.join(base, fn), encoding="utf-8") as f:
                tree = ast.parse(f.read())
        except Exception:
            continue
        routes, funcs = [], []
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                if not node.name.startswith("__"):
                    funcs.append(node.name)
                for dec in node.decorator_list:
                    if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                            and dec.func.attr == "route" and dec.args
                            and isinstance(dec.args[0], ast.Constant)):
                        routes.append(str(dec.args[0].value))
        index[fn] = {"routes": routes[:40], "functions": funcs[:40]}
    text = json.dumps(index, ensure_ascii=False)
    while len(text) > max_chars and index:      # قصّ بالتدريج إن كبر الفهرس
        for v in index.values():
            v["functions"] = v["functions"][:-5] if len(v["functions"]) > 5 else v["functions"]
        text = json.dumps(index, ensure_ascii=False)
        if all(len(v["functions"]) <= 5 for v in index.values()):
            break
    return index


def parse_plan(text, index):
    data = _json(text)
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise ValueError("plan missing summary")
    files = []
    for f in data.get("files") or []:
        if not isinstance(f, dict) or not isinstance(f.get("path"), str):
            continue
        action = f.get("action") if f.get("action") in ("edit", "create") else "edit"
        path = f["path"].strip()[:120]
        files.append({"path": path, "action": action,
                      "changes": str(f.get("changes") or "")[:600],
                      # تحذير: ملف "تعديل" غير موجود فعلاً في السيرفر
                      "unverified": action == "edit" and os.path.basename(path) not in index})
    if not files:
        raise ValueError("plan has no files")
    data["files"] = files
    for k in ("steps", "new_tables", "new_endpoints", "app_changes", "tests", "risks"):
        data[k] = [str(x)[:400] for x in (data.get(k) or [])][:15]
    for k in ("rollout", "rollback", "estimate"):
        data[k] = str(data.get(k) or "")[:500]
    brief = data.get("implementation_brief")
    if not isinstance(brief, str) or not brief.strip():
        raise ValueError("plan missing brief")
    data["implementation_brief"] = brief[:6000]
    return data


def _plan_job(pid):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("SELECT topic_key, spec FROM feature_proposals WHERE id=%s", (pid,))
        row = c.fetchone()
    finally:
        conn.close()
    status, plan, note = "ready", None, None
    try:
        if not row or not row[1]:
            raise ValueError("no spec")
        index = code_index()
        payload = {"proposal": row[1], "code_index": index}
        text, _, _ = _d["run_internal"]("feature_plan_write",
                                        json.dumps(payload, ensure_ascii=False))
        plan = parse_plan(text, index)
    except Exception as e:
        print("FEATURE PLAN ERROR:", repr(e))
        status, note = "error", ("فشل كتابة الخطة: " + str(e))[:300]
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("UPDATE feature_proposals SET plan=%s::jsonb, plan_status=%s, plan_note=%s WHERE id=%s",
                  (json.dumps(plan, ensure_ascii=False) if plan else None, status, note, pid))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def start_plan(pid):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("UPDATE feature_proposals SET plan_status='writing', plan_note=NULL "
                  "WHERE id=%s AND status='approved' AND (plan_status IS NULL OR plan_status<>'writing')",
                  (pid,))
        ok = c.rowcount > 0
        conn.commit()
    finally:
        conn.close()
    if ok:
        threading.Thread(target=_plan_job, args=(pid,), daemon=True).start()
    return ok


# ============================================================
# العرض
# ============================================================

def _e(x):
    return html.escape(str(x if x is not None else ""))


LEVEL_AR = {"low": "منخفض", "medium": "متوسط", "high": "عالٍ"}
STATUS_AR = {"ready": "جاهز للمراجعة", "approved": "موافَق عليه (بانتظار التنفيذ)",
             "rejected": "مرفوض", "error": "لم يكتمل"}
SOURCE_AR = {"refusal": "طلب غير مدعوم", "rating": "تقييمات"}

CSS = """<style>
body{font-family:system-ui,-apple-system,Segoe UI,Tahoma,sans-serif;margin:0;padding:14px;background:#f5f6f8;color:#1c1c1e;line-height:1.55}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px;color:#1565c0}
.card{background:#fff;border-radius:14px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.muted{color:#666;font-size:13px}.ok{background:#e8f5e9;border:1px solid #81c784}.warn{background:#fff4e5;border:1px solid #ffb74d}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;text-align:right;border-bottom:1px solid #eee;vertical-align:top}
th{color:#555;font-weight:600}.bad{color:#c62828;font-weight:600}.good{color:#2e7d32;font-weight:600}
a.btn,button.btn{display:inline-block;padding:9px 16px;background:#1565c0;color:#fff;border:0;border-radius:18px;text-decoration:none;font-size:14px;cursor:pointer}
button.red{background:#c62828}button.green{background:#2e7d32}a{color:#1565c0}.wrap{overflow-x:auto}
.bar{background:#e0e0e0;border-radius:6px;height:8px;min-width:80px}.bar>i{display:block;height:8px;border-radius:6px;background:#1565c0}
</style>"""

HEAD = ("<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Asadex Features</title>" + CSS + "</head><body>")


def render_list(ts, props, message, key):
    k = quote(key)
    out = [HEAD, "<h1>اكتشاف الميزات</h1>",
           f"<div class='muted'>لا يُقترح شيء قبل {MIN_USERS} مستخدماً مختلفاً. أنت وحدك توافق أو ترفض.</div>"]
    if message:
        out.append(f"<div class='card ok'>{_e(message)}</div>")
    out.append(f"<p><a class='btn' href='/admin/features/process?key={k}'>حدّث الآن</a></p>")

    out.append("<h2>الاقتراحات</h2>")
    if props:
        out.append("<div class='card wrap'><table><tr><th>#</th><th>الميزة</th><th>مستخدمون</th>"
                   "<th>الأولوية</th><th>الحالة</th><th></th></tr>")
        for p in props:
            spec = p["spec"] or {}
            out.append(f"<tr><td>{p['id']}</td><td>{_e(spec.get('title') or p['topic_key'])}</td>"
                       f"<td>{p['users_count']}</td><td>{_e(spec.get('priority', '-'))}</td>"
                       f"<td>{_e(STATUS_AR.get(p['status'], p['status']))}</td>"
                       f"<td><a href='?key={k}&id={p['id']}'>فتح</a></td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card muted'>لا توجد اقتراحات بعد، لم تبلغ أي ميزة العتبة.</div>")

    out.append("<h2>ما يطلبه المستخدمون (التقدم نحو العتبة)</h2>")
    if ts:
        out.append("<div class='card wrap'><table><tr><th>الموضوع</th><th>المصدر</th><th>مستخدمون</th>"
                   "<th>التقدم</th></tr>")
        for t in ts:
            pct = min(100, round(t["users"] / MIN_USERS * 100))
            out.append(f"<tr><td>{_e(t['title'])}<div class='muted'>{_e(t['key'])}</div></td>"
                       f"<td>{_e(SOURCE_AR.get(t['source'], t['source']))}</td>"
                       f"<td>{t['users']} / {MIN_USERS}</td>"
                       f"<td><div class='bar'><i style='width:{pct}%'></i></div></td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card muted'>لا توجد طلبات مصنّفة بعد.</div>")
    out.append("</body></html>")
    return "".join(out)


def _form(pid, action, label, cls, key, confirm=None):
    onsubmit = f" onsubmit=\"return confirm('{_e(confirm)}')\"" if confirm else ""
    return (f"<form method='post' action='/admin/features/decide' style='display:inline-block;margin:4px'{onsubmit}>"
            f"<input type='hidden' name='key' value='{_e(key)}'><input type='hidden' name='id' value='{pid}'>"
            f"<input type='hidden' name='action' value='{action}'><button class='btn {cls}'>{label}</button></form>")


def _render_plan(p, key):
    k = quote(key)
    ps, plan = p.get("plan_status"), p.get("plan")
    out = ["<h2>خطة التنفيذ</h2>"]
    if ps == "writing":
        out.append("<div class='card'>تُكتب الخطة الآن... حدّث الصفحة بعد دقيقة.</div>")
    elif ps == "error":
        out.append(f"<div class='card warn'>{_e(p.get('plan_note'))}</div>")
    if plan:
        out.append(f"<div class='card'>{_e(plan.get('summary'))}"
                   f"<div class='muted'>التقدير: {_e(plan.get('estimate'))}</div></div>")
        out.append("<div class='card wrap'><table><tr><th>الملف</th><th>الإجراء</th><th>التغيير</th></tr>")
        for f in plan.get("files", []):
            warn = " <span class='bad'>⚠️ غير موجود في السيرفر</span>" if f.get("unverified") else ""
            out.append(f"<tr><td dir='ltr'>{_e(f['path'])}{warn}</td><td>{_e(f['action'])}</td>"
                    f"<td>{_e(f['changes'])}</td></tr>")
        out.append("</table></div>")
        for label, field in (("الخطوات", "steps"), ("جداول جديدة", "new_tables"),
                             ("مسارات جديدة", "new_endpoints"),
                             ("تعديلات تحتاج تحديث التطبيق (الموبايل)", "app_changes"),
                             ("الاختبارات", "tests"), ("المخاطر", "risks")):
            if plan.get(field):
                out.append(f"<h2>{label}</h2><div class='card'>" +
                           "".join(f"<div>• {_e(x)}</div>" for x in plan[field]) + "</div>")
        out.append(f"<h2>النشر والتراجع</h2><div class='card'><div>النشر: {_e(plan.get('rollout'))}</div>"
                   f"<div>التراجع: {_e(plan.get('rollback'))}</div></div>")
        out.append("<h2>تعليمات جاهزة للمبرمج (انسخها)</h2><div class='card'>"
                   f"<pre style='white-space:pre-wrap;direction:ltr;text-align:left;font-size:12px'>"
                   f"{_e(plan.get('implementation_brief'))}</pre></div>")
    if ps != "writing":
        label = "أعد كتابة الخطة" if plan or ps == "error" else "اكتب خطة التنفيذ"
        out.append(f"<div class='card'><a class='btn' href='/admin/features/plan?key={k}&id={p['id']}'>{label}</a></div>")
    return "".join(out)


def render_detail(p, key):
    k = quote(key)
    s = p["spec"] or {}
    out = [HEAD, f"<h1>#{p['id']} {_e(s.get('title') or p['topic_key'])}</h1>",
           f"<div class='muted'>الحالة: {_e(STATUS_AR.get(p['status'], p['status']))} | "
           f"مستخدمون مختلفون: {p['users_count']} | إشارات: {p['signals_count']}</div>",
           f"<p><a href='?key={k}'>← رجوع</a></p>"]
    if p.get("note"):
        out.append(f"<div class='card warn'>{_e(p['note'])}</div>")
    if s:
        for label, field in (("لماذا", "why"), ("ماذا تفعل", "what_it_does"),
                             ("شكل المخرجات", "outputs"), ("التصميم وما يتغير", "design")):
            out.append(f"<h2>{label}</h2><div class='card'>{_e(s.get(field))}</div>")
        cost, ben = s.get("cost") or {}, s.get("benefit") or {}
        out.append("<h2>التكلفة والفائدة</h2><div class='card'>"
                   f"<div>التكلفة: <b>{_e(LEVEL_AR.get(cost.get('level'), '-'))}</b> "
                   f"<span class='muted'>{_e(cost.get('notes'))}</span></div>"
                   f"<div>الفائدة: <b>{_e(LEVEL_AR.get(ben.get('level'), '-'))}</b> "
                   f"<span class='muted'>{_e(ben.get('notes'))}</span></div>"
                   f"<div>الأولوية: <b>{_e(s.get('priority'))}</b></div></div>")
        if s.get("risks"):
            out.append("<h2>المخاطر</h2><div class='card'>" +
                       "".join(f"<div>• {_e(r)}</div>" for r in s["risks"]) + "</div>")
    if p["status"] == "approved":
        out.append(_render_plan(p, key))
    out.append("<h2>القرار</h2><div class='card'>")
    if p["status"] == "ready":
        out.append(_form(p["id"], "approve", "وافق", "green", key))
        out.append(_form(p["id"], "reject", "ارفض", "red", key))
    else:
        out.append("<div class='muted'>لا إجراء متاح.</div>")
    out.append("</div></body></html>")
    return "".join(out)


# ============================================================
# المسارات
# ============================================================

_COLS = "id, topic_key, status, users_count, signals_count, spec, note, plan, plan_status, plan_note"


def _row(r):
    return dict(zip(["id", "topic_key", "status", "users_count", "signals_count", "spec", "note",
                     "plan", "plan_status", "plan_note"], r))


@bp.route("/admin/features", methods=["GET"])
def admin_features():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        conn = _d["get_conn"]()
        try:
            c = conn.cursor()
            pid = request.args.get("id")
            if pid:
                c.execute(f"SELECT {_COLS} FROM feature_proposals WHERE id=%s", (int(pid),))
                r = c.fetchone()
                if not r:
                    return jsonify({"ok": False, "error": "Not found"}), 404
                p = _row(r)
                if request.args.get("format") == "json":
                    return jsonify({"ok": True, "proposal": p})
                return Response(render_detail(p, key), mimetype="text/html; charset=utf-8")
            c.execute(f"SELECT {_COLS} FROM feature_proposals ORDER BY id DESC LIMIT 30")
            props = [_row(r) for r in c.fetchall()]
        finally:
            conn.close()
        ts = topics()
        if request.args.get("format") == "json":
            return jsonify({"ok": True, "topics": ts, "proposals": props, "min_users": MIN_USERS})
        return Response(render_list(ts, props, request.args.get("msg"), key),
                        mimetype="text/html; charset=utf-8")
    except Exception as e:
        print("FEATURES PAGE ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed."}), 500


@bp.route("/admin/features/process", methods=["GET"])
def admin_features_process():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        msg = process()
    except Exception as e:
        print("FEATURES PROCESS ERROR:", repr(e))
        msg = "تعذّر التحديث الآن. حاول بعد قليل."
    return redirect(f"/admin/features?key={quote(key)}&msg={quote(msg)}")


@bp.route("/admin/features/plan", methods=["GET"])
def admin_features_plan():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        pid = int(request.args.get("id", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid id"}), 400
    try:
        start_plan(pid)
    except Exception as e:
        print("FEATURES PLAN START ERROR:", repr(e))
    return redirect(f"/admin/features?key={quote(key)}&id={pid}")


@bp.route("/admin/features/decide", methods=["POST"])
def admin_features_decide():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        pid = int(request.form.get("id", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid id"}), 400
    action = request.form.get("action", "")
    if action not in ("approve", "reject"):
        return jsonify({"ok": False, "error": "Invalid action"}), 400
    status = "approved" if action == "approve" else "rejected"
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("UPDATE feature_proposals SET status=%s, decided_at=NOW() "
                  "WHERE id=%s AND status='ready'", (status, pid))
        conn.commit()
    except Exception as e:
        conn.rollback()
        print("FEATURES DECIDE ERROR:", repr(e))
    finally:
        conn.close()
    return redirect(f"/admin/features?key={quote(key)}&id={pid}")
