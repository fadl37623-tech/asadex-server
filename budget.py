"""
SciGo Budget (حارس الميزانية وحدود الباقات).

يعمل في السيرفر، فلا يمكن تجاوزه من التطبيق:
  1) سقف يومي لإنفاق Gemini (BUDGET_DAILY_USD). عند اقترابه:
       70%  وضع التوفير: تتوقف المهام الداخلية (تحليل/اقتراح/اختبار)، ويُخفَّض حد المجاني للنصف.
       100% المجاني يتوقف حتى الغد، والمشتركون يستمرون.
       150% سقف قاسٍ: يتوقف الجميع.
  2) حد يومي لكل مستخدم بحسب باقته: مجاني (قليل) أو Pro (يلبي الاحتياج، مع سقف استخدام عادل).
  3) باقة المستخدم مخزّنة في user_plans، وتُحدَّث يدوياً من صفحة الإدارة
     أو تلقائياً من إشعارات RevenueCat (انظر /webhooks/revenuecat).
  4) كل رد ناجح يرجع معه "quota" ليعرض التطبيق تذكيراً بالاشتراك.

التكلفة تُحسب من التوكنات المسجّلة في prompt_runs (monitor.record_usage).
وإن لم تتوفر التوكنات لطلب ما تُستعمل تكلفة افتراضية.

الصفحة: /admin/budget?key=ADMIN_KEY

متغيرات Render (كلها اختيارية، هذه قيمها الافتراضية):
  BUDGET_DAILY_USD=0.30          الميزانية اليومية (نحو 9 دولارات شهرياً)
  FREE_DAILY_USD=0.010           حد المجاني اليومي (نحو 20 طلباً)
  PRO_DAILY_USD=0.060            حد المشترك اليومي (نحو 120 طلباً)
  BUDGET_DEFAULT_REQ_USD=0.0005  تكلفة طلب بلا توكنات مسجّلة
  GEMINI_PRICE_IN_PER_M / GEMINI_PRICE_OUT_PER_M   كما في monitor.py
  REVENUECAT_WEBHOOK_SECRET      لتفعيل الإشعارات التلقائية
  BUDGET_ALLOW_SANDBOX=1         لقبول مشتريات الاختبار (SANDBOX)
"""

import datetime
import html
import os
import secrets
import threading
import time
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, redirect, request

bp = Blueprint("budget", __name__)
_d = {}

STATE_SAVER = 0.70
STATE_EXHAUSTED = 1.00
STATE_CEILING = 1.50
STATE_TTL = 15

_state_cache = {"at": 0.0, "spent": 0.0}
_state_lock = threading.Lock()

INTERNAL_NOTE = "وضع التوفير: المهام الداخلية متوقفة اليوم لحماية الميزانية."

MESSAGES = {
    "daily_limit": (
        "وصلت حدّك اليومي المجاني. يتجدد غداً، أو اشترك للحصول على المزيد.",
        "You've reached today's free limit. It resets tomorrow, or subscribe for more."),
    "fair_use": (
        "وصلت حدّ الاستخدام العادل لليوم. يتجدد غداً.",
        "You've reached today's fair-use limit. It resets tomorrow."),
    "budget_exhausted": (
        "الخدمة المجانية مزدحمة اليوم وستعود غداً. المشتركون يواصلون الاستخدام.",
        "The free service is at capacity today and returns tomorrow. Subscribers can keep going."),
    "service_busy": (
        "الخدمة مزدحمة جداً الآن، حاول لاحقاً.",
        "The service is very busy right now. Please try again later."),
}


class BudgetPaused(RuntimeError):
    pass


# ============================================================
# إعدادات
# ============================================================

def _f(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return float(default)


def daily_budget():
    return _f("BUDGET_DAILY_USD", "0.30")


def free_daily():
    return _f("FREE_DAILY_USD", "0.010")


def pro_daily():
    return _f("PRO_DAILY_USD", "0.060")


def _developers():
    return {int(x) for x in os.environ.get("DEVELOPER_USER_IDS", "").split(",") if x.strip().isdigit()}


def _now_utc():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# ============================================================
# منطق خالص (قابل للاختبار)
# ============================================================

def global_state(spent, budget):
    if budget <= 0:
        return "ok"
    ratio = spent / budget
    if ratio >= STATE_CEILING:
        return "ceiling"
    if ratio >= STATE_EXHAUSTED:
        return "exhausted"
    if ratio >= STATE_SAVER:
        return "saver"
    return "ok"


def user_limit(plan, state):
    limit = pro_daily() if plan == "pro" else free_daily()
    if plan != "pro" and state == "saver":
        limit *= 0.5
    return limit
def decide(plan, state, user_spent):
    """يرجع None (مسموح) أو كود الحظر."""
    if state == "ceiling":
        return "service_busy"
    if plan != "pro" and state == "exhausted":
        return "budget_exhausted"
    if user_spent >= user_limit(plan, state):
        return "fair_use" if plan == "pro" else "daily_limit"
    return None


# ============================================================
# Setup
# ============================================================

def init(app, get_conn, require_auth):
    _d.update(get_conn=get_conn, require_auth=require_auth)
    _create_tables()
    app.register_blueprint(bp)


def _create_tables():
    conn = None
    try:
        conn = _d["get_conn"]()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_plans (
                user_id INTEGER PRIMARY KEY,
                plan TEXT NOT NULL DEFAULT 'free',
                expires_at TIMESTAMP,
                source TEXT,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS budget_blocks (
                id BIGSERIAL PRIMARY KEY,
                user_id INTEGER,
                code TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS budget_blocks_created ON budget_blocks(created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS prompt_runs_user_created ON prompt_runs(user_id, created_at)")
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("BUDGET TABLES ERROR:", repr(e))
    finally:
        if conn:
            conn.close()


def _admin_ok():
    expected = os.environ.get("ADMIN_KEY", "")
    supplied = request.headers.get("X-Admin-Key") or request.values.get("key", "")
    return bool(expected) and secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8"))


# ============================================================
# حساب الإنفاق
# ============================================================

def _cost_sql():
    pin = _f("GEMINI_PRICE_IN_PER_M", "0.30")
    pout = _f("GEMINI_PRICE_OUT_PER_M", "2.50")
    dflt = _f("BUDGET_DEFAULT_REQ_USD", "0.0005")
    sql = ("COALESCE(SUM(CASE WHEN tokens_in IS NULL AND tokens_out IS NULL THEN %s "
           "ELSE (COALESCE(tokens_in, 0) * %s + COALESCE(tokens_out, 0) * %s) / 1000000.0 END), 0)")
    return sql, (dflt, pin, pout)


def _scalar(query_tail, extra_params=()):
    sql, params = _cost_sql()
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute(f"SELECT {sql} FROM prompt_runs WHERE success AND NOT cached AND {query_tail}",
                  params + tuple(extra_params))
        return float(c.fetchone()[0] or 0)
    finally:
        conn.close()


def spent_today_total(use_cache=True):
    now = time.time()
    if use_cache:
        with _state_lock:
            if now - _state_cache["at"] < STATE_TTL:
                return _state_cache["spent"]
    spent = _scalar("created_at >= date_trunc('day', NOW())")
    with _state_lock:
        _state_cache.update(at=now, spent=spent)
    return spent


def spent_today_user(user_id):
    return _scalar("user_id = %s AND created_at >= date_trunc('day', NOW())", (user_id,))


def current_state():
    return global_state(spent_today_total(), daily_budget())


# ============================================================
# الباقات
# ============================================================

def get_plan(user_id):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("SELECT plan, expires_at FROM user_plans WHERE user_id=%s", (user_id,))
        row = c.fetchone()
    finally:
        conn.close()
    if row and row[0] == "pro" and row[1] is not None and row[1] > _now_utc():
        return "pro"
    return "free"
def set_plan(user_id, plan, expires_at, source):
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO user_plans (user_id, plan, expires_at, source, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (user_id) DO UPDATE
            SET plan=EXCLUDED.plan, expires_at=EXCLUDED.expires_at,
                source=EXCLUDED.source, updated_at=NOW()
        """, (user_id, plan, expires_at, source))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ============================================================
# نقاط الاستدعاء من prompt_engine
# ============================================================

def _log_block(user_id, code):
    try:
        conn = _d["get_conn"]()
        try:
            c = conn.cursor()
            c.execute("INSERT INTO budget_blocks (user_id, code) VALUES (%s, %s)", (user_id, code))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print("BUDGET BLOCK LOG ERROR:", repr(e))


def check_user(user_id, prompt_name=""):
    """None إذا مسموح، وإلا قاموس الخطأ (يُرجَع مع 429). لا يحظر أبداً عند وجود عطل داخلي."""
    try:
        if user_id in _developers():
            return None
        plan = get_plan(user_id)
        state = current_state()
        code = decide(plan, state, spent_today_user(user_id))
        if not code:
            return None
        _log_block(user_id, code)
        ar, en = MESSAGES[code]
        return {"ok": False, "error": "budget_limited", "code": code,
                "message_ar": ar, "message_en": en, "plan": plan,
                "upgrade": plan != "pro" and code != "service_busy"}
    except Exception as e:
        print("BUDGET CHECK ERROR:", repr(e))
        return None


def check_internal(prompt_name=""):
    """للمهام الداخلية: ترمي BudgetPaused إذا لم تكن الحالة 'ok'."""
    try:
        state = current_state()
    except Exception as e:
        print("BUDGET INTERNAL CHECK ERROR:", repr(e))
        return
    if state != "ok":
        raise BudgetPaused(INTERNAL_NOTE)


def quota(user_id):
    """معلومات يعرضها التطبيق لتذكير المجاني بالاشتراك. آمنة: ترجع None عند العطل."""
    try:
        plan = get_plan(user_id)
        state = current_state()
        limit = user_limit(plan, state)
        used = spent_today_user(user_id)
        pct = min(100, round(used / limit * 100)) if limit else 100
        return {"plan": plan, "percent_used": pct, "state": state,
                "upsell": plan != "pro" and pct >= 50}
    except Exception as e:
        print("BUDGET QUOTA ERROR:", repr(e))
        return None


@bp.route("/me/quota", methods=["GET"])
def me_quota():
    """للتطبيق: كم استهلك المستخدم من حدّه اليوم (لعرض تذكير الاشتراك، خصوصاً بعد البث)."""
    from flask import g
    auth_error = _d["require_auth"]()
    if auth_error:
        return auth_error
    q = quota(g.user_id)
    return jsonify({"ok": True, **(q or {"plan": "free", "percent_used": 0, "state": "ok", "upsell": False})})


# ============================================================
# صفحة الإدارة
# ============================================================

def _e(x):
    return html.escape(str(x if x is not None else ""))


CSS = """<style>
body{font-family:system-ui,-apple-system,Segoe UI,Tahoma,sans-serif;margin:0;padding:14px;background:#f5f6f8;color:#1c1c1e;line-height:1.55}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px;color:#1565c0}
.card{background:#fff;border-radius:14px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.muted{color:#666;font-size:13px}.ok{background:#e8f5e9;border:1px solid #81c784}.warn{background:#fff4e5;border:1px solid #ffb74d}
.alert{background:#ffebee;border:1px solid #e57373}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;text-align:right;border-bottom:1px solid #eee}
.bar{background:#e0e0e0;border-radius:6px;height:10px}.bar>i{display:block;height:10px;border-radius:6px;background:#1565c0}
input,select{padding:7px;border:1px solid #ccc;border-radius:8px;font-size:14px}
button{padding:9px 16px;background:#1565c0;color:#fff;border:0;border-radius:18px;font-size:14px;cursor:pointer}
.wrap{overflow-x:auto}
</style>"""
HEAD = ("<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Asadex Budget</title>" + CSS + "</head><body>")

STATE_AR = {"ok": ("طبيعي", "ok"), "saver": ("وضع التوفير", "warn"),
            "exhausted": ("المجاني متوقف حتى الغد", "alert"), "ceiling": ("السقف القاسي: الكل متوقف", "alert")}


def collect():
    sql, params = _cost_sql()
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute(f"SELECT {sql} FROM prompt_runs WHERE success AND NOT cached "
                  "AND created_at >= date_trunc('month', NOW())", params)
        month = float(c.fetchone()[0] or 0)
        c.execute(f"SELECT user_id, COUNT(*), {sql} FROM prompt_runs WHERE success AND NOT cached "
                  "AND user_id IS NOT NULL AND created_at >= date_trunc('day', NOW()) "
                  "GROUP BY user_id ORDER BY 3 DESC LIMIT 10", params)
        top = [{"user_id": u, "runs": n, "cost": float(cost or 0)} for u, n, cost in c.fetchall()]
        c.execute("SELECT code, COUNT(*) FROM budget_blocks WHERE created_at >= date_trunc('day', NOW()) "
                  "GROUP BY code ORDER BY 2 DESC")
        blocks = c.fetchall()
        c.execute("SELECT COUNT(*) FROM user_plans WHERE plan='pro' AND expires_at > NOW()")
        pro = c.fetchone()[0]
    finally:
        conn.close()
    today = spent_today_total(use_cache=False)
    return {"today": today, "month": month, "top": top, "blocks": blocks, "pro": pro,
            "state": global_state(today, daily_budget())}


def render(data, message, key):
    b = daily_budget()
    label, cls = STATE_AR[data["state"]]
    pct = min(100, round(data["today"] / b * 100)) if b else 0
    out = [HEAD, "<h1>حارس الميزانية</h1>",
           "<div class='muted'>التكاليف تقديرية من التوكنات المسجّلة. الأرقام الحقيقية في فاتورة Google.</div>"]
    if message:
        out.append(f"<div class='card ok'>{_e(message)}</div>")
    out.append(f"<div class='card {cls}'><b>الحالة: {_e(label)}</b><div class='bar'><i style='width:{pct}%'></i></div>"
               f"<div class='muted'>اليوم ${data['today']:.3f} من ${b:.2f} ({pct}%) | هذا الشهر ${data['month']:.2f} | "
               f"مشتركون Pro: {data['pro']}</div></div>")
    out.append(f"<div class='card muted'>حد المجاني اليومي ${free_daily():.3f} | حد Pro ${pro_daily():.3f} "
               f"| التوفير عند {int(STATE_SAVER * 100)}% | إيقاف المجاني عند 100% | سقف قاسٍ عند {int(STATE_CEILING * 100)}%</div>")

    out.append("<h2>الحظر اليوم</h2>")
    if data["blocks"]:
        out.append("<div class='card'>" + "".join(f"<div>{_e(c)}: <b>{n}</b></div>" for c, n in data["blocks"]) + "</div>")
    else:
        out.append("<div class='card muted'>لا حظر اليوم.</div>")

    out.append("<h2>أكثر المستخدمين إنفاقاً اليوم</h2>")
    if data["top"]:
        out.append("<div class='card wrap'><table><tr><th>المستخدم</th><th>طلبات</th><th>تكلفة</th></tr>")
        for t in data["top"]:
            out.append(f"<tr><td>{t['user_id']}</td><td>{t['runs']}</td><td>${t['cost']:.4f}</td></tr>")
        out.append("</table></div>")
    else:
        out.append("<div class='card muted'>لا بيانات.</div>")

    out.append("<h2>تغيير باقة مستخدم (للاختبار)</h2><div class='card'>"
               "<form method='post' action='/admin/budget/plan'>"
               f"<input type='hidden' name='key' value='{_e(key)}'>"
               "<input name='user_id' placeholder='رقم المستخدم' size='10'> "
               "<select name='plan'><option value='pro'>Pro</option><option value='free'>مجاني</option></select> "
               "<input name='days' value='30' size='4'> يوماً "
               "<button>حفظ</button></form></div></body></html>")
    return "".join(out)


@bp.route("/admin/budget", methods=["GET"])
def admin_budget():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    try:
        data = collect()
        if request.args.get("format") == "json":
            return jsonify({"ok": True, **data})
        return Response(render(data, request.args.get("msg"), request.values.get("key", "")),
                        mimetype="text/html; charset=utf-8")
    except Exception as e:
        print("BUDGET PAGE ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed.", "detail": repr(e)[:200]}), 500


@bp.route("/admin/budget/plan", methods=["POST"])
def admin_budget_plan():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    try:
        uid = int(request.form.get("user_id", ""))
        days = max(0, min(int(request.form.get("days", "30")), 3650))
    except ValueError:
        return redirect(f"/admin/budget?key={quote(key)}&msg={quote('بيانات غير صالحة')}")
    plan = "pro" if request.form.get("plan") == "pro" else "free"
    exp = _now_utc() + datetime.timedelta(days=days) if plan == "pro" else _now_utc()
    try:
        set_plan(uid, plan, exp, "admin")
        msg = f"المستخدم {uid} صار {plan}" + (f" لمدة {days} يوماً." if plan == "pro" else ".")
    except Exception as e:
        print("BUDGET SET PLAN ERROR:", repr(e))
        msg = "تعذّر الحفظ."
    return redirect(f"/admin/budget?key={quote(key)}&msg={quote(msg)}")


# ============================================================
# إشعارات RevenueCat (غير مجرَّبة بأحداث حقيقية بعد، راجعها عند الربط)
# في RevenueCat اجعل app_user_id = رقم المستخدم عندك، وضع الرابط
# /webhooks/revenuecat وقيمة Authorization = REVENUECAT_WEBHOOK_SECRET.
# ============================================================

ACTIVATE_EVENTS = {"INITIAL_PURCHASE", "RENEWAL", "UNCANCELLATION", "PRODUCT_CHANGE", "NON_RENEWING_PURCHASE"}


@bp.route("/webhooks/revenuecat", methods=["POST"])
def revenuecat_webhook():
    secret = os.environ.get("REVENUECAT_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"ok": False, "error": "Not configured"}), 503
    supplied = request.headers.get("Authorization", "")
    if supplied.startswith("Bearer "):
        supplied = supplied[7:]
    if not secrets.compare_digest(supplied.encode("utf-8"), secret.encode("utf-8")):
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    ev = (request.get_json(silent=True) or {}).get("event") or {}
    if ev.get("environment") == "SANDBOX" and os.environ.get("BUDGET_ALLOW_SANDBOX") != "1":
        return jsonify({"ok": True, "ignored": "sandbox"})
    uid_raw = str(ev.get("app_user_id") or "")
    if not uid_raw.isdigit():
        return jsonify({"ok": True, "ignored": "unknown user"})
    uid, kind = int(uid_raw), ev.get("type")
    try:
        if kind in ACTIVATE_EVENTS:
            ms = ev.get("expiration_at_ms")
            if isinstance(ms, bool) or not isinstance(ms, (int, float)):
                return jsonify({"ok": True, "ignored": "no expiration"})
            exp = datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).replace(tzinfo=None)
            set_plan(uid, "pro", exp, "revenuecat")
        elif kind == "EXPIRATION":
            set_plan(uid, "free", _now_utc(), "revenuecat")
    except Exception as e:
        print("REVENUECAT WEBHOOK ERROR:", repr(e))
        return jsonify({"ok": False}), 500
    return jsonify({"ok": True})
