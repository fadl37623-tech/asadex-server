"""
SciGo Feature Builder (المرحلة 5): النظام يكتب الكود، وأنت توافق مرتين.

الدورة لكل ميزة موافَق عليها ولها خطة تنفيذ:
  1) Gemini يكتب تعديلات صغيرة (نص قديم ← نص جديد) على ملفات الخطة فقط.
  2) فحوص آلية: صياغة Python، ملفات مسموحة فقط، حجم التغيير، كلمات ممنوعة.
  3) تراجع الفرق (diff) في الصفحة. موافقة 1: يُدفع الكود إلى فرع staging.
  4) تجرّب على خدمة staging (اختياري لكن موصى به).
  5) موافقة 2: دمج staging في main ← Render ينشر تلقائياً.
  6) زر تراجع يعيد main لما قبل الميزة (إن لم يتغير بعدها).

يحتاج في Render: GITHUB_TOKEN (صلاحية Contents: read/write لهذا المستودع فقط)
و GITHUB_REPO بصيغة owner/name. اختيارياً: GITHUB_SERVER_DIR، GITHUB_MAIN_BRANCH، GITHUB_STAGING_BRANCH.

الصفحة: /admin/builds?key=ADMIN_KEY
"""

import ast
import base64
import difflib
import html
import json
import os
import re
import secrets
import threading
from urllib.parse import quote

import httpx
from flask import Blueprint, Response, jsonify, redirect, request

bp = Blueprint("feature_builder", __name__)
_d = {}

MAX_FILES = 5
MAX_CHANGED_LINES = 400
MAX_CONTEXT_CHARS = 300_000
FORBIDDEN_FILES = {"feature_builder.py"}
SAFE_PATH = re.compile(r"^[A-Za-z0-9_]+\.py$")
DENY_PATTERNS = [
    (r"\bsubprocess\b", "subprocess"),
    (r"\bos\.system\b", "os.system"),
    (r"\beval\(", "eval("),
    (r"\bexec\(", "exec("),
    (r"import", "import"),
    (r"\bDROP\s+(TABLE|DATABASE)\b", "DROP"),
    (r"\bTRUNCATE\b", "TRUNCATE"),
    (r"\bDELETE\s+FROM\s+students\b", "DELETE FROM students"),
    (r"GITHUB_TOKEN", "GITHUB_TOKEN"),
]

CODE_SYSTEM = """You are a careful senior Python/Flask engineer making a SMALL, SAFE change to the Asadex server (Flask + PostgreSQL via psycopg2; Gemini prompts live in a prompts table; helper modules are Blueprints with an init(app, get_conn, ...) function).

Input JSON: {"proposal": {...}, "plan": {...}, "files": {"name.py": "<full current source>"}}

Implement the plan with the SMALLEST possible edits. Output ONLY this format, no markdown fences, no other text:

===SUMMARY===
<2-4 short Arabic sentences: what changed and why>
===END_SUMMARY===
===EDIT file.py===
<<<<<<< OLD
exact existing text copied character for character, with indentation. It must occur EXACTLY ONCE in the file, so include enough surrounding lines to make it unique.
=======
replacement text (never empty)
>>>>>>> NEW
===CREATE newfile.py===
full file content
===END_FILE===

Rules:
- Touch only files listed in plan.files. Use EDIT for existing files and CREATE for new ones.
- Changes must be additive and backward compatible: existing mobile app versions must keep working unchanged. Never remove or rename existing functions, routes, tables, columns or prompt names.
- Never touch authentication, token creation, password handling, rate limits, usage limits, or secrets/environment variables.
- New DB tables: CREATE TABLE IF NOT EXISTS inside an init-time function with try/except and rollback, in the style of the surrounding code.
- Every new endpoint must authenticate (require_auth for users, the admin key check for admin pages) and validate its input. Use %s parameters in SQL, never string-built SQL with user input.
- Do not use subprocess, os.system, eval, exec or import.
- Existing prompt texts live in the database; editing a seed's text in code has NO effect. If the plan can only be done by changing a prompt's text, output exactly ===CANNOT=== followed by one Arabic sentence saying it must be done as a new prompt version, and nothing else.
- Match the surrounding code style. Keep comments short.
- If the plan cannot be implemented safely under these rules, output exactly ===CANNOT=== followed by one Arabic sentence explaining why, and nothing else.
"""


# ============================================================
# Setup
# ============================================================

def init(app, get_conn, run_internal):
    _d.update(get_conn=get_conn, run_internal=run_internal)
    _create_tables()
    app.register_blueprint(bp)
def _create_tables():
    conn = None
    try:
        conn = _d["get_conn"]()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS feature_builds (
                id SERIAL PRIMARY KEY,
                proposal_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'writing',
                summary TEXT,
                changes JSONB,
                checks JSONB,
                note TEXT,
                raw TEXT,
                staging_sha TEXT,
                main_before_sha TEXT,
                merge_sha TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                decided_at TIMESTAMP
            )
        """)
        c.execute("SELECT 1 FROM prompts WHERE name='feature_code_write' LIMIT 1")
        if not c.fetchone():
            c.execute(
                "INSERT INTO prompts (name, version, text, config, status, source, notes, activated_at) "
                "VALUES ('feature_code_write', 1, %s, %s::jsonb, 'active', 'seed', 'initial version', NOW())",
                (CODE_SYSTEM, json.dumps({"thinking_budget": 4096})),
            )
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("BUILDER TABLES ERROR:", repr(e))
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
# GitHub
# ============================================================

class GhError(Exception):
    pass


def _cfg():
    return {
        "token": os.environ.get("GITHUB_TOKEN", ""),
        "repo": os.environ.get("GITHUB_REPO", ""),
        "main": os.environ.get("GITHUB_MAIN_BRANCH", "main"),
        "staging": os.environ.get("GITHUB_STAGING_BRANCH", "staging"),
        "dir": os.environ.get("GITHUB_SERVER_DIR", "").strip("/"),
    }


def _missing(cfg):
    return [n for n, k in (("GITHUB_TOKEN", "token"), ("GITHUB_REPO", "repo")) if not cfg[k]]


def _repo_path(cfg, path):
    return f"{cfg['dir']}/{path}" if cfg["dir"] else path


def _gh(cfg, method, path, **kw):
    return httpx.request(
        method, "https://api.github.com/repos/" + cfg["repo"] + path,
        headers={"Authorization": "Bearer " + cfg["token"],
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"},
        timeout=20, **kw)


def fetch_file(cfg, path, ref):
    """يرجع (النص بعد توحيد نهايات الأسطر، sha، هل كان CRLF) أو (None, None, False)."""
    r = _gh(cfg, "GET", "/contents/" + _repo_path(cfg, path), params={"ref": ref})
    if r.status_code == 404:
        return None, None, False
    if r.status_code != 200:
        raise GhError(f"GitHub {r.status_code} عند قراءة {path}")
    j = r.json()
    if not isinstance(j, dict) or j.get("encoding") != "base64" or j.get("content") is None:
        raise GhError(f"تعذّرت قراءة {path} (كبير جداً؟)")
    raw = base64.b64decode(j["content"]).decode("utf-8")
    crlf = "\r\n" in raw
    return raw.replace("\r\n", "\n"), j["sha"], crlf


def _ref_sha(cfg, branch):
    r = _gh(cfg, "GET", f"/git/ref/heads/{branch}")
    if r.status_code == 404:
        return None
    if r.status_code != 200:
        raise GhError(f"GitHub {r.status_code} عند قراءة الفرع {branch}")
    return r.json()["object"]["sha"]


# ============================================================
# تحليل مخرجات النموذج وتطبيقها (منطق خالص)
# ============================================================

_EDIT_RE = re.compile(
    r"===EDIT\s+(\S+?)\s*===\s*\n<<<<<<< OLD\n(.*?)\n=======\n(.*?)\n>>>>>>> NEW", re.S)
_CREATE_RE = re.compile(r"===CREATE\s+(\S+?)\s*===\s*\n(.*?)\n===END_FILE===", re.S)
_SUMMARY_RE = re.compile(r"===SUMMARY===\s*(.*?)\s*===END_SUMMARY===", re.S)
def parse_code_output(text):
    text = text or ""
    if "===CANNOT===" in text:
        reason = text.split("===CANNOT===", 1)[1].strip()[:300]
        raise ValueError("النموذج رفض التنفيذ: " + (reason or "غير ممكن بأمان"))
    m = _SUMMARY_RE.search(text)
    summary = m.group(1).strip()[:600] if m else ""
    edits = [(p, o, n) for p, o, n in _EDIT_RE.findall(text)]
    creates = [(p, c) for p, c in _CREATE_RE.findall(text)]
    if not edits and not creates:
        raise ValueError("لم يرجع النموذج أي تعديل بالصيغة المطلوبة")
    return summary, edits, creates


def apply_changes(bases, edits, creates, allowed):
    """bases: {path: النص الحالي}. يرجع (changes, problems)."""
    work = dict(bases)
    created, problems = set(), []
    for path, old, new in edits:
        if path not in allowed:
            problems.append(f"{path}: خارج ملفات الخطة")
            continue
        if path not in work:
            problems.append(f"{path}: ملف غير موجود")
            continue
        n = work[path].count(old)
        if n != 1:
            problems.append(f"{path}: نص التعديل وُجد {n} مرة (يجب مرة واحدة)")
            continue
        work[path] = work[path].replace(old, new, 1)
    for path, content in creates:
        if path not in allowed:
            problems.append(f"{path}: خارج ملفات الخطة")
            continue
        if path in work:
            problems.append(f"{path}: موجود مسبقاً")
            continue
        work[path] = content if content.endswith("\n") else content + "\n"
        created.add(path)
    changes = []
    for path, new in work.items():
        old = bases.get(path, "")
        if path in created or new != old:
            changes.append({"path": path, "action": "create" if path in created else "edit",
                            "old": old, "new": new})
    return changes, problems


def changed_lines(old, new):
    n = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            n += 1
    return n


def run_checks(changes):
    checks, total = [], 0
    if len(changes) > MAX_FILES:
        checks.append({"check": "files", "file": "", "passed": False,
                       "detail": f"{len(changes)} ملفات (الحد {MAX_FILES})"})
    for ch in changes:
        path = ch["path"]
        if not SAFE_PATH.match(path) or path in FORBIDDEN_FILES:
            checks.append({"check": "path", "file": path, "passed": False, "detail": "ملف غير مسموح"})
            continue
        try:
            ast.parse(ch["new"])
            checks.append({"check": "syntax", "file": path, "passed": True, "detail": "ok"})
        except SyntaxError as e:
            checks.append({"check": "syntax", "file": path, "passed": False,
                           "detail": f"سطر {e.lineno}: {e.msg}"})
        n = changed_lines(ch["old"], ch["new"])
        total += n
        # الكلمات الممنوعة تُفحص في الأسطر المضافة فقط
        added = "\n".join(
            l[1:] for l in difflib.unified_diff(ch["old"].splitlines(), ch["new"].splitlines(),
                                               lineterm="", n=0)
            if l.startswith("+") and not l.startswith("+++"))
        bad = [label for rx, label in DENY_PATTERNS if re.search(rx, added)]
        checks.append({"check": "deny_list", "file": path, "passed": not bad,
                       "detail": ("ممنوع: " + ", ".join(bad)) if bad else "ok"})
    checks.append({"check": "size", "file": "", "passed": total <= MAX_CHANGED_LINES,
                   "detail": f"{total} سطراً تغيّر (الحد {MAX_CHANGED_LINES})"})
    return checks


def all_passed(checks):
    return bool(checks) and all(c["passed"] for c in checks)


# ============================================================
# كتابة الكود (خيط خلفي)
# ============================================================
def _set(bid, **fields):
    json_cols = {"changes", "checks"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in json_cols:
            sets.append(f"{k} = %s::jsonb")
            vals.append(json.dumps(v, ensure_ascii=False))
        else:
            sets.append(f"{k} = %s")
            vals.append(v)
    vals.append(bid)
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute(f"UPDATE feature_builds SET {', '.join(sets)} WHERE id = %s", vals)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _build_job(bid, proposal_id):
    cfg = _cfg()
    raw = None
    try:
        missing = _missing(cfg)
        if missing:
            raise ValueError("متغيرات ناقصة في Render: " + ", ".join(missing))

        conn = _d["get_conn"]()
        try:
            c = conn.cursor()
            c.execute("SELECT spec, plan FROM feature_proposals WHERE id=%s AND status='approved'",
                      (proposal_id,))
            row = c.fetchone()
        finally:
            conn.close()
        if not row or not row[0] or not row[1]:
            raise ValueError("الاقتراح غير موافَق عليه أو لا توجد له خطة")
        spec, plan = row

        plan_files = plan.get("files") or []
        if not plan_files or len(plan_files) > MAX_FILES:
            raise ValueError(f"الخطة تمس {len(plan_files)} ملفات (المسموح 1 إلى {MAX_FILES})")
        allowed, edit_paths = set(), []
        for f in plan_files:
            path = str(f.get("path", "")).strip()
            if not SAFE_PATH.match(path) or path in FORBIDDEN_FILES:
                raise ValueError("الخطة تذكر ملفاً غير مسموح: " + path)
            allowed.add(path)
            if f.get("action") == "edit":
                edit_paths.append(path)

        bases, crlf, context, size = {}, {}, {}, 0
        for path in edit_paths:
            text, _, was_crlf = fetch_file(cfg, path, cfg["main"])
            if text is None:
                raise ValueError(f"الملف {path} غير موجود في GitHub (تحقق من GITHUB_SERVER_DIR)")
            bases[path], crlf[path] = text, was_crlf
            context[path] = text
            size += len(text)
        if size > MAX_CONTEXT_CHARS:
            raise ValueError("ملفات الخطة كبيرة جداً على النموذج")

        payload = {
            "proposal": spec,
            "plan": {k: plan.get(k) for k in ("summary", "steps", "files", "new_tables",
                                              "new_endpoints", "implementation_brief")},
            "files": context,
        }
        raw, _, _ = _d["run_internal"]("feature_code_write", json.dumps(payload, ensure_ascii=False))
        summary, edits, creates = parse_code_output(raw)
        changes, problems = apply_changes(bases, edits, creates, allowed)
        for ch in changes:
            ch["crlf"] = bool(crlf.get(ch["path"]))
        if problems:
            _set(bid, status="error", raw=(raw or "")[:30000],
                 note="تعذّر تطبيق التعديلات: " + "؛ ".join(problems[:5]))
            return
        if not changes:
            raise ValueError("لم يتغير أي ملف")
        checks = run_checks(changes)
        if all_passed(checks):
            _set(bid, status="ready", summary=summary, changes=changes, checks=checks,
                 raw=(raw or "")[:30000])
        else:
            failed = [f"{c['file']} {c['check']}: {c['detail']}" for c in checks if not c["passed"]]
            _set(bid, status="error", summary=summary, changes=changes, checks=checks,
                 raw=(raw or "")[:30000], note="فشلت الفحوص: " + "؛ ".join(failed[:4]))
    except Exception as e:
        print("BUILD JOB ERROR:", repr(e))
        try:
            _set(bid, status="error", raw=(raw or "")[:30000], note=("فشل: " + str(e))[:400])
        except Exception as e2:
            print("BUILD SET ERROR:", repr(e2))
def start_build(proposal_id):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("UPDATE feature_builds SET status='error', note='interrupted' "
                  "WHERE status='writing' AND created_at < NOW() - INTERVAL '30 minutes'")
        c.execute("SELECT 1 FROM feature_proposals WHERE id=%s AND status='approved' "
                  "AND plan_status='ready'", (proposal_id,))
        if not c.fetchone():
            conn.commit()
            raise ValueError("يجب أن يكون الاقتراح موافَقاً عليه وله خطة جاهزة.")
        c.execute("SELECT 1 FROM feature_builds WHERE proposal_id=%s AND status IN "
                  "('writing','ready','staged')", (proposal_id,))
        if c.fetchone():
            conn.commit()
            raise ValueError("توجد محاولة جارية لهذه الميزة.")
        c.execute("INSERT INTO feature_builds (proposal_id) VALUES (%s) RETURNING id", (proposal_id,))
        bid = c.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    threading.Thread(target=_build_job, args=(bid, proposal_id), daemon=True).start()
    return bid


# ============================================================
# الموافقتان والتراجع
# ============================================================

def _load(bid):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("SELECT id, proposal_id, status, summary, changes, checks, note, "
                  "staging_sha, main_before_sha, merge_sha FROM feature_builds WHERE id=%s", (bid,))
        r = c.fetchone()
        if not r:
            return None
        return dict(zip(["id", "proposal_id", "status", "summary", "changes", "checks", "note",
                         "staging_sha", "main_before_sha", "merge_sha"], r))
    finally:
        conn.close()


def push_staging(b):
    """موافقة 1: يصفّر staging على main ثم يدفع الملفات. يرجع (staging_sha, main_sha)."""
    cfg = _cfg()
    missing = _missing(cfg)
    if missing:
        raise GhError("متغيرات ناقصة: " + ", ".join(missing))
    main_sha = _ref_sha(cfg, cfg["main"])
    if not main_sha:
        raise GhError("فرع main غير موجود")

    # لا نكتب فوق شيء تغيّر بعد كتابة التعديل
    for ch in b["changes"]:
        cur, _, _ = fetch_file(cfg, ch["path"], cfg["main"])
        if ch["action"] == "edit" and cur != ch["old"]:
            raise GhError(f"{ch['path']} تغيّر في main بعد كتابة الكود. أعد كتابة الكود.")
        if ch["action"] == "create" and cur is not None:
            raise GhError(f"{ch['path']} أصبح موجوداً في main. أعد كتابة الكود.")

    st = cfg["staging"]
    if _ref_sha(cfg, st):
        r = _gh(cfg, "PATCH", f"/git/refs/heads/{st}", json={"sha": main_sha, "force": True})
    else:
        r = _gh(cfg, "POST", "/git/refs", json={"ref": f"refs/heads/{st}", "sha": main_sha})
    if r.status_code not in (200, 201):
        raise GhError(f"تعذّر تجهيز فرع {st} ({r.status_code})")

    last = None
    for ch in b["changes"]:
        _, sha, _ = fetch_file(cfg, ch["path"], st)
        content = ch["new"].replace("\n", "\r\n") if ch.get("crlf") else ch["new"]
        body = {"message": f"feature #{b['proposal_id']}: {ch['path']}",
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "branch": st}
        if sha:
            body["sha"] = sha
        r = _gh(cfg, "PUT", "/contents/" + _repo_path(cfg, ch["path"]), json=body)
        if r.status_code not in (200, 201):
            raise GhError(f"تعذّر رفع {ch['path']} ({r.status_code})")
        last = r.json()["commit"]["sha"]
    return last, main_sha


def merge_to_main(b):
    """موافقة 2: دمج staging في main. يرجع (merge_sha، main قبل الدمج)."""
    cfg = _cfg()
    head = _ref_sha(cfg, cfg["staging"])
    if head != b["staging_sha"]:
        raise GhError("فرع staging تغيّر بعد الموافقة الأولى. أعد الإرسال إلى staging.")
    before = _ref_sha(cfg, cfg["main"])
    r = _gh(cfg, "POST", "/merges", json={
        "base": cfg["main"], "head": cfg["staging"],
        "commit_message": f"Feature #{b['proposal_id']} (build {b['id']})"})
    if r.status_code == 409:
        raise GhError("تعارض في الدمج: تغيّر main. أعد كتابة الكود.")
    if r.status_code == 204:
        raise GhError("لا يوجد جديد للدمج.")
    if r.status_code != 201:
        raise GhError(f"تعذّر الدمج ({r.status_code})")
    return r.json()["sha"], before


def rollback_main(b):
    cfg = _cfg()
    cur = _ref_sha(cfg, cfg["main"])
    if cur != b["merge_sha"]:
        raise GhError("main تغيّر بعد هذه الميزة. تراجع يدوياً بـ git revert حتى لا نفقد أعمالاً أخرى.")
    r = _gh(cfg, "PATCH", f"/git/refs/heads/{cfg['main']}",
            json={"sha": b["main_before_sha"], "force": True})
    if r.status_code != 200:
        raise GhError(f"تعذّر التراجع ({r.status_code})")


# ============================================================
# العرض
# ============================================================

def _e(x):
    return html.escape(str(x if x is not None else ""))


STATUS_AR = {
    "writing": "يُكتب الكود... (حدّث الصفحة)", "ready": "جاهز للمراجعة (موافقة 1)",
    "staged": "على staging (بانتظار موافقة النشر)", "deployed": "نُشر على الإنتاج",
    "rejected": "مرفوض", "rolled_back": "تم التراجع", "error": "لم يكتمل",
}

CSS = """<style>
body{font-family:system-ui,-apple-system,Segoe UI,Tahoma,sans-serif;margin:0;padding:14px;background:#f5f6f8;color:#1c1c1e;line-height:1.55}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px;color:#1565c0}
.card{background:#fff;border-radius:14px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.muted{color:#666;font-size:13px}.ok{background:#e8f5e9;border:1px solid #81c784}.warn{background:#fff4e5;border:1px solid #ffb74d}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;text-align:right;border-bottom:1px solid #eee;vertical-align:top}
.bad{color:#c62828;font-weight:600}.good{color:#2e7d32;font-weight:600}
a.btn,button.btn{display:inline-block;padding:9px 16px;background:#1565c0;color:#fff;border:0;border-radius:18px;text-decoration:none;font-size:14px;cursor:pointer}
button.red{background:#c62828}button.green{background:#2e7d32}button.grey{background:#757575}a{color:#1565c0}.wrap{overflow-x:auto}
pre.diff{direction:ltr;text-align:left;white-space:pre-wrap;font-size:12px;background:#fafafa;padding:8px;border-radius:8px;margin:0}
.add{background:#e6ffed}.del{background:#ffeef0}
</style>"""

HEAD = ("<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Asadex Builds</title>" + CSS + "</head><body>")


def _diff_html(old, new):
    lines = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), "قبل", "بعد", lineterm="", n=2):
        if line.startswith(("+++", "---")):
            continue
        cls = "add" if line.startswith("+") else ("del" if line.startswith("-") else "")
        lines.append(f"<span class='{cls}'>{_e(line)}</span>")
    return "<pre class='diff'>" + "\n".join(lines) + "</pre>"


def _form(bid, action, label, cls, key, confirm=None):
    onsubmit = f" onsubmit=\"return confirm('{_e(confirm)}')\"" if confirm else ""
    return (f"<form method='post' action='/admin/builds/decide' style='display:inline-block;margin:4px'{onsubmit}>"
            f"<input type='hidden' name='key' value='{_e(key)}'><input type='hidden' name='id' value='{bid}'>"
            f"<input type='hidden' name='action' value='{action}'><button class='btn {cls}'>{label}</button></form>")


def render_list(rows, message, key):
    k = quote(key)
    out = [HEAD, "<h1>بناء الميزات</h1>",
            "<div class='muted'>النظام يكتب الكود. أنت توافق مرتين: على الكود، ثم على النشر.</div>"]
    if message:
        out.append(f"<div class='card warn'>{_e(message)}</div>")
    if rows:
        out.append("<div class='card wrap'><table><tr><th>#</th><th>الاقتراح</th><th>الحالة</th><th></th></tr>")
        for r in rows:
            out.append(f"<tr><td>{r[0]}</td><td>#{r[1]}</td><td>{_e(STATUS_AR.get(r[2], r[2]))}</td>"
                       f"<td><a href='?key={k}&id={r[0]}'>فتح</a></td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card muted'>لا توجد محاولات بعد. ابدأ من صفحة اقتراح له خطة.</div>")
    out.append("</body></html>")
    return "".join(out)


def render_detail(b, key):
    k = quote(key)
    cfg = _cfg()
    out = [HEAD, f"<h1>محاولة #{b['id']} للاقتراح #{b['proposal_id']}</h1>",
           f"<div class='muted'>الحالة: {_e(STATUS_AR.get(b['status'], b['status']))}</div>",
           f"<p><a href='?key={k}'>← رجوع</a> | "
           f"<a href='/admin/features?key={k}&id={b['proposal_id']}'>صفحة الاقتراح</a></p>"]
    if b.get("note"):
        out.append(f"<div class='card warn'>{_e(b['note'])}</div>")
    if b.get("summary"):
        out.append(f"<h2>ملخص ما تغيّر</h2><div class='card'>{_e(b['summary'])}</div>")
    if b.get("checks"):
        out.append("<h2>الفحوص الآلية</h2><div class='card wrap'><table><tr><th>الفحص</th><th>الملف</th><th>النتيجة</th></tr>")
        for c in b["checks"]:
            cls = "good" if c["passed"] else "bad"
            out.append(f"<tr><td>{_e(c['check'])}</td><td dir='ltr'>{_e(c['file'])}</td>"
                       f"<td class='{cls}'>{_e(c['detail'])}</td></tr>")
        out.append("</table><div class='muted'>الفحوص تثبت سلامة الصياغة والحدود فقط. "
                   "صحة المنطق تُختبر على staging وبمراجعتك للفرق.</div></div>")
    if b.get("changes"):
        out.append("<h2>الفرق (الكود)</h2>")
        for ch in b["changes"]:
            tag = "جديد" if ch["action"] == "create" else "تعديل"
            out.append(f"<div class='card wrap'><b dir='ltr'>{_e(ch['path'])}</b> ({tag})"
                       f"{_diff_html(ch['old'], ch['new'])}</div>")

    out.append("<h2>القرار</h2><div class='card'>")
    st = b["status"]
    if st == "ready":
        out.append(_form(b["id"], "to_staging", "وافق على الكود وأرسله إلى staging", "green", key))
        out.append(_form(b["id"], "reject", "ارفض", "red", key))
    elif st == "staged":
        out.append(f"<div class='muted'>الكود الآن على فرع <b>{_e(cfg['staging'])}</b>. جرّبه على خدمة staging "
                   f"ثم قرّر. <a href='https://github.com/{_e(cfg['repo'])}/compare/{_e(cfg['main'])}...{_e(cfg['staging'])}' "
                   f"target='_blank'>عرض على GitHub</a></div>")
        out.append(_form(b["id"], "deploy", "وافق وانشر على الإنتاج", "green", key,
                         "سيُدمج الكود في main وينشره Render لكل المستخدمين. متأكد؟"))
        out.append(_form(b["id"], "reject", "ارفض", "red", key))
    elif st == "deployed":
        out.append("<div class='muted'>منشور على الإنتاج.</div>")
        out.append(_form(b["id"], "rollback", "تراجع عن الميزة", "grey", key,
                         "سيُعاد main إلى ما قبل الميزة. متأكد؟"))
    else:
        out.append("<div class='muted'>لا إجراء متاح.</div>")
    out.append("</div></body></html>")
    return "".join(out)


# ============================================================
# المسارات
# ============================================================

@bp.route("/admin/builds", methods=["GET"])
def admin_builds():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        bid = request.args.get("id")
        if bid:
            b = _load(int(bid))
            if not b:
                return jsonify({"ok": False, "error": "Not found"}), 404
            return Response(render_detail(b, key), mimetype="text/html; charset=utf-8")
        conn = _d["get_conn"]()
        try:
            c = conn.cursor()
            c.execute("UPDATE feature_builds SET status='error', note='interrupted' "
                      "WHERE status='writing' AND created_at < NOW() - INTERVAL '30 minutes'")
            conn.commit()
            c.execute("SELECT id, proposal_id, status FROM feature_builds ORDER BY id DESC LIMIT 30")
            rows = c.fetchall()
        finally:
            conn.close()
        return Response(render_list(rows, request.args.get("msg"), key),
                        mimetype="text/html; charset=utf-8")
    except Exception as e:
        print("BUILDS PAGE ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed."}), 500


@bp.route("/admin/builds/new", methods=["GET"])
def admin_builds_new():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        pid = int(request.args.get("id", ""))
        bid = start_build(pid)
    except ValueError as e:
        return redirect(f"/admin/builds?key={quote(key)}&msg={quote(str(e))}")
    except Exception as e:
        print("BUILD START ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed."}), 500
    return redirect(f"/admin/builds?key={quote(key)}&id={bid}")


@bp.route("/admin/builds/decide", methods=["POST"])
def admin_builds_decide():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        bid = int(request.form.get("id", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid id"}), 400
    action = request.form.get("action", "")
    b = _load(bid)
    if not b:
        return jsonify({"ok": False, "error": "Not found"}), 404

    def back(msg=None):
        extra = f"&msg={quote(msg)}" if msg else ""
        return redirect(f"/admin/builds?key={quote(key)}&id={bid}{extra}")

    try:
        if action == "to_staging":
            if b["status"] != "ready":
                return back("غير جاهز.")
            staging_sha, main_sha = push_staging(b)
            _set(bid, status="staged", staging_sha=staging_sha, main_before_sha=main_sha)
        elif action == "deploy":
            if b["status"] != "staged":
                return back("ليس على staging.")
            merge_sha, before = merge_to_main(b)
            _set(bid, status="deployed", merge_sha=merge_sha, main_before_sha=before)
            _mark_decided(bid)
        elif action == "rollback":
            if b["status"] != "deployed":
                return back("لا يوجد ما يُتراجع عنه.")
            rollback_main(b)
            _set(bid, status="rolled_back")
            _mark_decided(bid)
        elif action == "reject":
            if b["status"] not in ("ready", "staged"):
                return back("لا يمكن الرفض الآن.")
            _set(bid, status="rejected")
            _mark_decided(bid)
        else:
            return jsonify({"ok": False, "error": "Invalid action"}), 400
    except GhError as e:
        return back(str(e))
    except Exception as e:
        print("BUILD DECIDE ERROR:", repr(e))
        return back("حدث خطأ.")
    return back()


def _mark_decided(bid):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("UPDATE feature_builds SET decided_at = NOW() WHERE id = %s", (bid,))
        conn.commit()
    finally:
        conn.close()
