"""
SciGo Prompt Engine (المرحلة أ)

- يخزّن الـ prompts في قاعدة البيانات بنسخ (v1, v2, ...) ونسخة فعّالة واحدة لكل اسم.
- التطبيق يرسل اسم الـ prompt فقط، والسيرفر يضيف النص الفعّال.
- كل طلب يُسجَّل في prompt_runs (run_id + نسخة الـ prompt) ليربط التقييم بالنسخة.
- تفعيل نسخة أو التراجع عنها بطلب واحد، بدون تحديث التطبيق.
"""

import json
import os
import re
import secrets
import threading
import time
import uuid

from flask import Blueprint, Response, jsonify, request, stream_with_context
from google import genai
from google.genai import types

bp = Blueprint("prompt_engine", __name__)

MODEL = "gemini-2.5-flash"
CACHE_TTL_SECONDS = 30
MAX_TEXT_LEN = 30000
MAX_IMAGE_B64 = 10_000_000
MAX_CONTENTS = 100
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,59}$")
VAR_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
ALLOWED_MIME = {"image/jpeg", "image/png", "image/webp"}
STREAM_ERROR_MARK = "[[STREAM_ERROR]]"

_deps = {}
_cache = {}
_cache_lock = threading.Lock()
_client = None


# ============================================================
# Setup
# ============================================================

def init(app, get_conn, require_auth, limiter, api_key):
    _deps.update(get_conn=get_conn, require_auth=require_auth, api_key=api_key)
    _create_tables()
    _seed_defaults()
    app.register_blueprint(bp)

    # حد معدل لكل مستخدم/IP على نقاط الذكاء الاصطناعي
    for endpoint in ("prompt_engine.ai_run", "prompt_engine.ai_run_stream"):
        app.view_functions[endpoint] = limiter.limit("40 per minute")(
            app.view_functions[endpoint]
        )


def _get_client():
    global _client
    if _client is None:
        _client = genai.Client(
            api_key=_deps["api_key"],
            http_options={"api_version": "v1beta"},
        )
    return _client


def _create_tables():
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS prompts (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                version INTEGER NOT NULL,
                text TEXT NOT NULL,
                config JSONB NOT NULL DEFAULT '{}'::jsonb,
                status TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft','active','archived')),
                source TEXT NOT NULL DEFAULT 'manual',
                notes TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                activated_at TIMESTAMP,
                UNIQUE (name, version)
            )
        """)
        c.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS prompts_one_active
            ON prompts(name) WHERE status = 'active'
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS prompt_runs (
                run_id TEXT PRIMARY KEY,
                user_id INTEGER,
                prompt_name TEXT NOT NULL,
                prompt_version INTEGER NOT NULL,
                latency_ms DOUBLE PRECISION,
                success BOOLEAN NOT NULL DEFAULT TRUE,
                error_type TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        c.execute("""
            CREATE INDEX IF NOT EXISTS prompt_runs_name_version
            ON prompt_runs(prompt_name, prompt_version)
        """)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("PROMPT TABLES ERROR:", repr(e))
    finally:
        if conn:
            conn.close()

    # أعمدة ربط التقييم بنسخة الـ prompt
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute("ALTER TABLE answer_feedback ADD COLUMN IF NOT EXISTS prompt_name TEXT")
        c.execute("ALTER TABLE answer_feedback ADD COLUMN IF NOT EXISTS prompt_version INTEGER")
        c.execute("ALTER TABLE answer_feedback ADD COLUMN IF NOT EXISTS run_id TEXT")
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("FEEDBACK PROMPT COLUMNS (run /init first):", repr(e))
    finally:
        if conn:
            conn.close()


def _seed_defaults():
    """يضيف النسخة 1 الفعّالة لكل prompt غير موجود. لا يلمس الموجود."""
    from prompt_seeds import SEEDS

    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        for name, seed in SEEDS.items():
            c.execute("SELECT 1 FROM prompts WHERE name=%s LIMIT 1", (name,))
            if c.fetchone():
                continue
            c.execute(
                """
                INSERT INTO prompts (name, version, text, config, status, source, notes, activated_at)
                VALUES (%s, 1, %s, %s::jsonb, 'active', 'seed', 'initial version', NOW())
                """,
                (name, seed["text"], json.dumps(seed.get("config", {}))),
            )
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("PROMPT SEED ERROR:", repr(e))
    finally:
        if conn:
            conn.close()


# ============================================================
# Active prompt lookup (مع كاش قصير)
# ============================================================

def get_active(name):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(name)
        if hit and now - hit["at"] < CACHE_TTL_SECONDS:
            return hit["row"]

    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute(
            "SELECT version, text, config FROM prompts WHERE name=%s AND status='active'",
            (name,),
        )
        row = c.fetchone()
    finally:
        if conn:
            conn.close()

    result = None
    if row:
        cfg = row[2] if isinstance(row[2], dict) else json.loads(row[2] or "{}")
        result = {"name": name, "version": row[0], "text": row[1], "config": cfg}

    with _cache_lock:
        _cache[name] = {"at": now, "row": result}
    return result


def _clear_cache():
    with _cache_lock:
        _cache.clear()


# ============================================================
# Request validation
# ============================================================

def _bad(msg, code=400):
    return jsonify({"ok": False, "error": msg}), code


def _valid_contents(contents):
    if not isinstance(contents, list) or not contents or len(contents) > MAX_CONTENTS:
        return False
    for item in contents:
        if not isinstance(item, dict) or item.get("role") not in ("user", "model"):
            return False
        parts = item.get("parts")
        if not isinstance(parts, list) or not parts or len(parts) > 6:
            return False
        for part in parts:
            if not isinstance(part, dict):
                return False
            if "text" in part:
                if not isinstance(part["text"], str) or len(part["text"]) > MAX_TEXT_LEN * 2:
                    return False
            elif "inline_data" in part:
                inline = part["inline_data"]
                if (
                    not isinstance(inline, dict)
                    or inline.get("mime_type") not in ALLOWED_MIME
                    or not isinstance(inline.get("data"), str)
                    or len(inline["data"]) > MAX_IMAGE_B64
                ):
                    return False
            else:
                return False
    return True


def _prepare(data):
    name = data.get("prompt")
    if not isinstance(name, str) or not NAME_RE.match(name):
        return None, _bad("Invalid prompt name.")

    active = get_active(name)
    if not active:
        return None, _bad("Unknown prompt.", 404)
    variables = data.get("variables") or {}
    if not isinstance(variables, dict) or len(variables) > 20:
        return None, _bad("Invalid variables.")
    system = active["text"]
    for key, value in variables.items():
        if not VAR_RE.match(str(key)) or not isinstance(value, (str, int, float)):
            return None, _bad("Invalid variables.")
        value = str(value)
        if len(value) > MAX_TEXT_LEN:
            return None, _bad("Variable too long.")
        system = system.replace("{{" + key + "}}", value)

    contents = data.get("contents")
    if contents is None:
        user_input = data.get("user_input")
        if not isinstance(user_input, str) or not user_input.strip():
            return None, _bad("user_input or contents is required.")
        if len(user_input) > MAX_TEXT_LEN * 2:
            return None, _bad("Input too long.")
        contents = user_input
    elif not _valid_contents(contents):
        return None, _bad("Invalid contents.")

    return {
        "name": name,
        "version": active["version"],
        "system": system,
        "contents": contents,
        "cfg": active["config"],
    }, None


# ============================================================
# Gemini
# ============================================================

def _gen_config(system, cfg):
    kwargs = {"system_instruction": system}
    if cfg.get("thinking_budget") is not None:
        kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_budget=int(cfg["thinking_budget"])
        )
    if cfg.get("google_search"):
        kwargs["tools"] = [types.Tool(google_search=types.GoogleSearch())]
    return types.GenerateContentConfig(**kwargs)


def _generate(p, attempts=3):
    client = _get_client()
    config = _gen_config(p["system"], p["cfg"])
    for attempt in range(1, attempts + 1):
        try:
            return client.models.generate_content(
                model=MODEL, contents=p["contents"], config=config
            )
        except Exception as e:
            text = str(e)
            temporary = any(k in text for k in ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED"))
            if not temporary or attempt >= attempts:
                raise
            time.sleep(2 * attempt)


def _grounding_sources(response):
    sources, seen = [], set()
    try:
        for cand in getattr(response, "candidates", None) or []:
            meta = getattr(cand, "grounding_metadata", None)
            for chunk in (getattr(meta, "grounding_chunks", None) or []):
                web = getattr(chunk, "web", None)
                uri = getattr(web, "uri", None) if web else None
                if uri and uri not in seen:
                    seen.add(uri)
                    sources.append({"url": uri, "title": getattr(web, "title", "") or ""})
    except Exception as e:
        print("GROUNDING ERROR:", repr(e))
    return sources


def _log_run(run_id, user_id, p, latency_ms, success, error_type=None):
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO prompt_runs
            (run_id, user_id, prompt_name, prompt_version, latency_ms, success, error_type)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, user_id, p["name"], p["version"], latency_ms, success, error_type),
        )
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("PROMPT RUN LOG ERROR:", repr(e))
    finally:
        if conn:
            conn.close()


# ============================================================
# /ai/run  و  /ai/run-stream
# ============================================================

@bp.route("/ai/run", methods=["POST"])
def ai_run():
    from flask import g

    auth_error = _deps["require_auth"]()
    if auth_error:
        return auth_error

    p, err = _prepare(request.get_json(silent=True) or {})
    if err:
        return err
    run_id = uuid.uuid4().hex
    start = time.perf_counter()
    try:
        response = _generate(p)
        answer = response.text
        if not answer:
            raise RuntimeError("empty answer")
        ms = round((time.perf_counter() - start) * 1000, 2)
        _log_run(run_id, g.user_id, p, ms, True)

        out = {
            "ok": True,
            "answer": answer,
            "run_id": run_id,
            "prompt_name": p["name"],
            "prompt_version": p["version"],
        }
        if p["cfg"].get("google_search"):
            out["grounding_sources"] = _grounding_sources(response)
        return jsonify(out)

    except Exception as e:
        ms = round((time.perf_counter() - start) * 1000, 2)
        print("AI RUN ERROR:", repr(e))
        _log_run(run_id, g.user_id, p, ms, False, type(e).__name__)
        return jsonify({"ok": False, "error": "Something went wrong.", "run_id": run_id}), 500


@bp.route("/ai/run-stream", methods=["POST"])
def ai_run_stream():
    from flask import g

    auth_error = _deps["require_auth"]()
    if auth_error:
        return auth_error

    p, err = _prepare(request.get_json(silent=True) or {})
    if err:
        return err

    run_id = uuid.uuid4().hex
    user_id = g.user_id

    def generate():
        start = time.perf_counter()
        try:
            stream = _get_client().models.generate_content_stream(
                model=MODEL,
                contents=p["contents"],
                config=_gen_config(p["system"], p["cfg"]),
            )
            for chunk in stream:
                text = getattr(chunk, "text", None)
                if text:
                    yield text
            _log_run(run_id, user_id, p, round((time.perf_counter() - start) * 1000, 2), True)
        except Exception as e:
            print("AI RUN STREAM ERROR:", repr(e))
            _log_run(run_id, user_id, p, round((time.perf_counter() - start) * 1000, 2),
                     False, type(e).__name__)
            yield "\n" + STREAM_ERROR_MARK

    return Response(
        stream_with_context(generate()),
        mimetype="text/plain",
        headers={"X-Run-Id": run_id, "X-Prompt-Version": str(p["version"])},
    )


# ============================================================
# Admin: عرض / إنشاء نسخة / تفعيل / تراجع
# الحماية: هيدر X-Admin-Key = متغير البيئة ADMIN_KEY
# ============================================================

def _admin_ok():
    expected = os.environ.get("ADMIN_KEY", "")
    supplied = request.headers.get("X-Admin-Key", "")
    return bool(expected) and secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


def _clean_config(cfg):
    if not isinstance(cfg, dict):
        return {}
    out = {}
    tb = cfg.get("thinking_budget")
    if isinstance(tb, int) and not isinstance(tb, bool) and 0 <= tb <= 8192:
        out["thinking_budget"] = tb
    if cfg.get("google_search") is True:
        out["google_search"] = True
    return out


@bp.route("/admin/prompts", methods=["GET"])
def admin_list():
    if not _admin_ok():
        return _bad("Unauthorized", 403)
    name = request.args.get("name")
    full = request.args.get("full") == "1"
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        if name:
            c.execute(
                "SELECT name, version, status, source, notes, created_at, activated_at, text, config "
                "FROM prompts WHERE name=%s ORDER BY version DESC", (name,))
        else:
            c.execute(
                "SELECT name, version, status, source, notes, created_at, activated_at, text, config "
                "FROM prompts ORDER BY name, version DESC")
        rows = []
        for n, v, st, src, notes, cr, ac, text, cfg in c.fetchall():
            rows.append({
                "name": n, "version": v, "status": st, "source": src, "notes": notes,
                "created_at": cr.isoformat() if cr else None,
                "activated_at": ac.isoformat() if ac else None,
                "config": cfg,
                "text": text if full else text[:200] + ("..." if len(text) > 200 else ""),
            })
        return jsonify({"ok": True, "prompts": rows})
    except Exception as e:
        print("ADMIN LIST ERROR:", repr(e))
        return _bad("Failed.", 500)
    finally:
        if conn:
            conn.close()


@bp.route("/admin/prompts", methods=["POST"])
def admin_create():
    """ينشئ نسخة جديدة كمسودة (draft) ولا يفعّلها."""
    if not _admin_ok():
        return _bad("Unauthorized", 403)
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    text = data.get("text")
    if not isinstance(name, str) or not NAME_RE.match(name):
        return _bad("Invalid name.")
    if not isinstance(text, str) or not text.strip() or len(text) > 60000:
        return _bad("Invalid text.")
    notes = str(data.get("notes") or "")[:500]

    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM prompts WHERE name=%s", (name,))
        version = c.fetchone()[0]
        c.execute(
            """
            INSERT INTO prompts (name, version, text, config, status, source, notes)
            VALUES (%s, %s, %s, %s::jsonb, 'draft', %s, %s)
            """,
            (name, version, text, json.dumps(_clean_config(data.get("config"))),
             "engine" if data.get("source") == "engine" else "manual", notes),
        )
        conn.commit()
        return jsonify({"ok": True, "name": name, "version": version, "status": "draft"})
    except Exception as e:
        if conn:
            conn.rollback()
        print("ADMIN CREATE ERROR:", repr(e))
        return _bad("Failed.", 500)
    finally:
        if conn:
            conn.close()


@bp.route("/admin/prompts/activate", methods=["POST"])
def admin_activate():
    """تفعيل نسخة. التراجع = تفعيل نسخة أقدم بنفس الطلب."""
    if not _admin_ok():
        return _bad("Unauthorized", 403)
    data = request.get_json(silent=True) or {}
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not NAME_RE.match(name) or not isinstance(version, int):
        return _bad("Invalid name or version.")

    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute("SELECT 1 FROM prompts WHERE name=%s AND version=%s", (name, version))
        if not c.fetchone():
            return _bad("Version not found.", 404)
        c.execute("UPDATE prompts SET status='archived' WHERE name=%s AND status='active'", (name,))
        c.execute(
            "UPDATE prompts SET status='active', activated_at=NOW() WHERE name=%s AND version=%s",
            (name, version),
        )
        conn.commit()
        _clear_cache()
        return jsonify({"ok": True, "name": name, "active_version": version})
    except Exception as e:
        if conn:
            conn.rollback()
        print("ADMIN ACTIVATE ERROR:", repr(e))
        return _bad("Failed.", 500)
    finally:
        if conn:
            conn.close()


# ============================================================
# Admin: إحصاءات التقييم لكل نسخة prompt (بداية المحلل)
# يقبل المفتاح في الهيدر X-Admin-Key أو في ?key= (للاختبار من المتصفح)
# ============================================================

@bp.route("/admin/prompt-stats", methods=["GET"])
def admin_prompt_stats():
    expected = os.environ.get("ADMIN_KEY", "")
    supplied = request.headers.get("X-Admin-Key") or request.args.get("key", "")
    if not expected or not secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    ):
        return _bad("Unauthorized", 403)
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute("""
            SELECT prompt_name, prompt_version,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE rating = 1),
                   COUNT(*) FILTER (WHERE rating = -1),
                   AVG(rating)
            FROM answer_feedback
            WHERE prompt_name IS NOT NULL
            GROUP BY prompt_name, prompt_version
            ORDER BY prompt_name, prompt_version
        """)
        feedback = [
            {"prompt": n, "version": v, "total": t, "positive": p, "negative": ng,
             "average_rating": round(float(a), 2) if a is not None else 0}
            for n, v, t, p, ng, a in c.fetchall()
        ]
        c.execute("""
            SELECT prompt_name, prompt_version,
                   COUNT(*),
                   COUNT(*) FILTER (WHERE success),
                   AVG(latency_ms)
            FROM prompt_runs
            GROUP BY prompt_name, prompt_version
            ORDER BY prompt_name, prompt_version
        """)
        runs = [
            {"prompt": n, "version": v, "runs": t, "succeeded": s,
             "avg_latency_ms": round(float(l), 1) if l is not None else None}
            for n, v, t, s, l in c.fetchall()
        ]
        return jsonify({"ok": True, "feedback": feedback, "runs": runs})
    except Exception as e:
        print("PROMPT STATS ERROR:", repr(e))
        return _bad("Failed.", 500)
    finally:
        if conn:
            conn.close()
