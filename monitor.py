"""
SciGo Monitor (المرحلة 8): هل رفعت التحسينات رضا المستخدمين؟ وكم تكلف؟

- يقارن كل نسخة prompt فعّالة بالنسخة التي قبلها: رضا المستخدمين، الفشل، السرعة.
- لا يطلق إنذاراً على فرق صغير: يستعمل اختباراً إحصائياً (z) ويشترط حداً أدنى من التقييمات.
- يقدّر تكلفة Gemini من عدد التوكنات (تقدير، والأسعار تُضبط من Render).
- يعرض اتجاه آخر 14 يوماً.
- زر تراجع إلى النسخة السابقة (بتأكيدك). لا يغيّر شيئاً بنفسه.

الصفحة: /admin/monitor?key=ADMIN_KEY    (للبيانات الخام أضف &format=json)

متغيرات اختيارية في Render:
  MONITOR_MIN_RATINGS      (الافتراضي 20)  أقل عدد تقييمات لكل نسخة قبل المقارنة
  GEMINI_PRICE_IN_PER_M    (الافتراضي 0.30) دولار لكل مليون توكن دخل
  GEMINI_PRICE_OUT_PER_M   (الافتراضي 2.50) دولار لكل مليون توكن خرج (يشمل التفكير)
  تحقق من أسعار نموذجك الحالي في صفحة أسعار Gemini، فقد تتغير.
"""

import html
import math
import os
import secrets
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, redirect, request

bp = Blueprint("monitor", __name__)
_d = {}

MIN_RATINGS = int(os.environ.get("MONITOR_MIN_RATINGS", "20"))
MIN_RUNS = 20
NEG_RISE = 0.05          # أقل ارتفاع في نسبة السلبي يُعدّ مهماً
Z_ONE_SIDED_95 = 1.64
FAIL_RISE = 0.03
LATENCY_FACTOR = 1.3


def _price_in():
    return float(os.environ.get("GEMINI_PRICE_IN_PER_M", "0.30"))


def _price_out():
    return float(os.environ.get("GEMINI_PRICE_OUT_PER_M", "2.50"))


def init(app, get_conn, engine):
    _d.update(get_conn=get_conn, engine=engine)
    _create_tables()
    app.register_blueprint(bp)


def _create_tables():
    conn = None
    try:
        conn = _d["get_conn"]()
        c = conn.cursor()
        c.execute("ALTER TABLE prompt_runs ADD COLUMN IF NOT EXISTS tokens_in INTEGER")
        c.execute("ALTER TABLE prompt_runs ADD COLUMN IF NOT EXISTS tokens_out INTEGER")
        c.execute("CREATE INDEX IF NOT EXISTS prompt_runs_created ON prompt_runs(created_at)")
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("MONITOR TABLES ERROR:", repr(e))
    finally:
        if conn:
            conn.close()


def _admin_ok():
    expected = os.environ.get("ADMIN_KEY", "")
    supplied = request.headers.get("X-Admin-Key") or request.values.get("key", "")
    return bool(expected) and secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8"))


# ============================================================
# تسجيل التوكنات (يُستدعى من prompt_engine بعد كل رد)
# ============================================================

def record_usage(run_id, response):
    """آمن: لا يرمي أخطاء. يحفظ عدد التوكنات لهذا الطلب."""
    try:
        u = getattr(response, "usage_metadata", None)
        if not u or not run_id:
            return
        tin = getattr(u, "prompt_token_count", None) or 0
        tout = (getattr(u, "candidates_token_count", None) or 0) + \
               (getattr(u, "thoughts_token_count", None) or 0)
        conn = _d["get_conn"]()
        try:
            c = conn.cursor()
            c.execute("UPDATE prompt_runs SET tokens_in=%s, tokens_out=%s WHERE run_id=%s",
                      (int(tin), int(tout), run_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print("MONITOR USAGE ERROR:", repr(e))


# ============================================================
# منطق خالص (قابل للاختبار)
# ============================================================

def _rate(part, total):
    return part / total if total else None


def z_score(p1, n1, p2, n2):
    """z لاختبار أن النسبة الأولى أكبر من الثانية."""
    if not n1 or not n2:
        return 0.0
    pooled = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    return (p1 - p2) / se if se else 0.0
def evaluate(prev, act):
    """prev/act: {version, runs, failed, lat, ratings, neg}. يرجع قائمة {level, text}."""
    out = []
    if act["ratings"] < MIN_RATINGS or prev["ratings"] < MIN_RATINGS:
        out.append({"level": "info",
                    "text": f"التقييمات غير كافية للحكم (النسخة الحالية {act['ratings']} والسابقة "
                            f"{prev['ratings']}، المطلوب {MIN_RATINGS} لكل منهما)."})
    else:
        pa, pp = act["neg"] / act["ratings"], prev["neg"] / prev["ratings"]
        z = z_score(pa, act["ratings"], pp, prev["ratings"])
        if pa - pp >= NEG_RISE and z >= Z_ONE_SIDED_95:
            out.append({"level": "bad",
                        "text": f"ساء رضا المستخدمين: السلبي {round(pa * 100)}% مقابل {round(pp * 100)}% "
                                f"في v{prev['version']}، والفرق ذو دلالة إحصائية. فكّر في التراجع."})
        elif pp - pa >= NEG_RISE and -z >= Z_ONE_SIDED_95:
            out.append({"level": "good",
                        "text": f"تحسّن الرضا: السلبي {round(pa * 100)}% مقابل {round(pp * 100)}% "
                                f"في v{prev['version']}."})
        else:
            out.append({"level": "info",
                        "text": f"لا فرق مؤكد في الرضا (السلبي {round(pa * 100)}% مقابل {round(pp * 100)}%). "
                                "قد يكون الفرق ضجيجاً أو عيّنة قليلة."})
    if act["runs"] >= MIN_RUNS:
        fa = act["failed"] / act["runs"]
        fp = prev["failed"] / prev["runs"] if prev["runs"] else 0
        if fa >= 0.05 and fa - fp >= FAIL_RISE:
            out.append({"level": "bad",
                        "text": f"زادت الأخطاء: {round(fa * 100)}% من الطلبات تفشل (كانت {round(fp * 100)}%)."})
        if act["lat"] and prev["lat"] and prev["runs"] >= MIN_RUNS and act["lat"] > prev["lat"] * LATENCY_FACTOR:
            out.append({"level": "bad",
                        "text": f"أبطأ: {act['lat'] / 1000:.1f} ث مقابل {prev['lat'] / 1000:.1f} ث."})
    return out


FEATURES = [
    ("سؤال دردشة", ["chat_concise", "chat_detailed"], "avg"),
    ("خطة دراسة", ["plan_create", "plan_create_lecture"], "avg"),
    ("اختبار", ["quiz_make", "quiz_make_lecture"], "avg"),
    ("متابعة (Similar/Challenge/Simpler/Summary)",
     ["followup_similar", "followup_challenge", "followup_simpler", "followup_summary"], "avg"),
    ("تحليل محاضرة/ملف", ["lecture_summary"], "avg"),
    ("رسم بياني", ["diagram_plan"], "avg"),
    ("تقرير (التوليد + البحث عن المصادر)", ["report_generate", "source_search"], "sum"),
]


def feature_costs(per_prompt):
    """per_prompt: {name: {"runs","avg","max"}}. يرجع تكلفة كل ميزة (متوسط، أقصى)."""
    out = []
    for label, names, mode in FEATURES:
        rows = [per_prompt[n] for n in names if n in per_prompt and per_prompt[n]["runs"]]
        if not rows:
            out.append({"label": label, "runs": 0, "avg": None, "max": None})
            continue
        if mode == "sum":
            avg, mx = sum(r["avg"] for r in rows), sum(r["max"] for r in rows)
        else:
            total = sum(r["runs"] for r in rows)
            avg = sum(r["avg"] * r["runs"] for r in rows) / total
            mx = max(r["max"] for r in rows)
        runs = max(r["runs"] for r in rows) if mode == "sum" else sum(r["runs"] for r in rows)
        out.append({"label": label, "runs": runs, "avg": avg, "max": mx})
    return out


def cost_usd(tin, tout):
    return ((tin or 0) * _price_in() + (tout or 0) * _price_out()) / 1_000_000


# ============================================================
# جمع البيانات
# ============================================================

def collect():
    conn = _d["get_conn"]()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT name, version, status, activated_at FROM prompts
            ORDER BY name, version
        """)
        meta = {}
        for n, v, st, ac in c.fetchall():
            meta.setdefault(n, {})[v] = {"status": st, "activated_at": ac.isoformat() if ac else None}
        c.execute("""
            SELECT prompt_name, prompt_version,
                   COUNT(*) FILTER (WHERE NOT cached),
                   COUNT(*) FILTER (WHERE NOT success),
                   AVG(latency_ms) FILTER (WHERE NOT cached),
                   COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0)
            FROM prompt_runs GROUP BY 1, 2
        """)
        runs = {(n, v): {"runs": r, "failed": f, "lat": float(l) if l is not None else None,
                         "tin": int(ti), "tout": int(to)}
                for n, v, r, f, l, ti, to in c.fetchall()}

        c.execute("""
            SELECT prompt_name, prompt_version, COUNT(*), COUNT(*) FILTER (WHERE rating = -1)
            FROM answer_feedback WHERE prompt_name IS NOT NULL GROUP BY 1, 2
        """)
        fb = {(n, v): {"ratings": t, "neg": ng} for n, v, t, ng in c.fetchall()}

        prompts = []
        for name, versions in sorted(meta.items()):
            rows = []
            for v, m in sorted(versions.items()):
                r = runs.get((name, v), {"runs": 0, "failed": 0, "lat": None, "tin": 0, "tout": 0})
                f = fb.get((name, v), {"ratings": 0, "neg": 0})
                rows.append({"version": v, m, r, **f, "cost": cost_usd(r["tin"], r["tout"])})
            active = next((x for x in rows if x["status"] == "active"), None)
            prev = None
            if active:
                older = [x for x in rows if x["version"] < active["version"] and (x["runs"] or x["ratings"])]
                prev = older[-1] if older else None
            alerts = evaluate(prev, active) if (active and prev) else []
            if rows and any(x["runs"] or x["ratings"] for x in rows):
                prompts.append({"name": name, "versions": rows, "active": active,
                                "prev": prev, "alerts": alerts})

        c.execute("""
            SELECT date_trunc('day', created_at)::date,
                   COUNT(*) FILTER (WHERE NOT cached),
                   COUNT(*) FILTER (WHERE NOT success),
                   COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0)
            FROM prompt_runs WHERE created_at > NOW() - INTERVAL '14 days'
            GROUP BY 1 ORDER BY 1 DESC
        """)
        days = {d: {"day": d.isoformat(), "runs": r, "failed": f, "tin": int(ti), "tout": int(to),
                    "ratings": 0, "neg": 0} for d, r, f, ti, to in c.fetchall()}
        c.execute("""
            SELECT date_trunc('day', created_at)::date, COUNT(*), COUNT(*) FILTER (WHERE rating = -1)
            FROM answer_feedback WHERE created_at > NOW() - INTERVAL '14 days' GROUP BY 1
        """)
        for d, t, ng in c.fetchall():
            days.setdefault(d, {"day": d.isoformat(), "runs": 0, "failed": 0, "tin": 0, "tout": 0,
                                "ratings": 0, "neg": 0}).update(ratings=t, neg=ng)
        trend = sorted(days.values(), key=lambda x: x["day"], reverse=True)
        for t in trend:
            t["cost"] = cost_usd(t["tin"], t["tout"])
        pin, pout = _price_in(), _price_out()
        c.execute("""
            SELECT prompt_name, COUNT(*), AVG(c), MAX(c), AVG(ti), AVG(tou)
            FROM (SELECT prompt_name,
                         (COALESCE(tokens_in, 0) * %s + COALESCE(tokens_out, 0) * %s) / 1000000.0 AS c,
                         COALESCE(tokens_in, 0) AS ti, COALESCE(tokens_out, 0) AS tou
                  FROM prompt_runs
                  WHERE success AND NOT cached AND tokens_in IS NOT NULL
                    AND created_at > NOW() - INTERVAL '30 days') t
            GROUP BY prompt_name ORDER BY 3 DESC
        """, (pin, pout))
        per_prompt = {n: {"runs": r, "avg": float(a), "max": float(m), "tin": float(ti), "tout": float(to)}
                      for n, r, a, m, ti, to in c.fetchall()}
        c.execute("""
            SELECT created_at, prompt_name, prompt_version, tokens_in, tokens_out, latency_ms, success
            FROM prompt_runs WHERE NOT cached ORDER BY created_at DESC LIMIT 20
        """)
        recent = [{"at": at.strftime("%m-%d %H:%M:%S") if at else "", "prompt": n, "version": v,
                   "tin": ti, "tout": to, "lat": float(l) if l is not None else None, "ok": ok,
                   "cost": cost_usd(ti, to) if ti is not None or to is not None else None}
                  for at, n, v, ti, to, l, ok in c.fetchall()]
        return {"prompts": prompts, "trend": trend,
                "tokens_tracked": any(t["tin"] or t["tout"] for t in trend),
                "per_prompt": per_prompt, "features": feature_costs(per_prompt), "recent": recent}
    finally:
        conn.close()


# ============================================================
# العرض
# ============================================================

def _e(x):
    return html.escape(str(x if x is not None else ""))


def _pct(part, total):
    return f"{round(part / total * 100)}%" if total else "-"


CSS = """<style>
body{font-family:system-ui,-apple-system,Segoe UI,Tahoma,sans-serif;margin:0;padding:14px;background:#f5f6f8;color:#1c1c1e;line-height:1.55}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px;color:#1565c0}
.card{background:#fff;border-radius:14px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.muted{color:#666;font-size:13px}.ok{background:#e8f5e9;border:1px solid #81c784}.warn{background:#fff4e5;border:1px solid #ffb74d}
.alert{background:#ffebee;border:1px solid #e57373}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;text-align:right;border-bottom:1px solid #eee}
th{color:#555;font-weight:600}.bad{color:#c62828;font-weight:600}.good{color:#2e7d32;font-weight:600}.wrap{overflow-x:auto}
button.btn{display:inline-block;padding:9px 16px;background:#757575;color:#fff;border:0;border-radius:18px;font-size:14px;cursor:pointer}
a{color:#1565c0}
</style>"""
HEAD = ("<!doctype html><html lang='ar' dir='rtl'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Asadex Monitor</title>" + CSS + "</head><body>")


def render(data, message, key):
    out = [HEAD, "<h1>المراقبة</h1>",
           "<div class='muted'>هل رفعت كل نسخة رضا المستخدمين؟ لا إنذار على فرق صغير: يُشترط عدد كافٍ من التقييمات وفرق ذو دلالة.</div>"]
    if message:
        out.append(f"<div class='card ok'>{_e(message)}</div>")
    if not data["tokens_tracked"]:
        out.append("<div class='card warn'>التكلفة غير متاحة بعد: لم تُسجَّل توكنات. أضف استدعاء "
                   "<code>monitor.record_usage</code> في prompt_engine (انظر التعليمات).</div>")

    for p in data["prompts"]:
        out.append(f"<h2>{_e(p['name'])}</h2>")
        for a in p["alerts"]:
            cls = {"bad": "alert", "good": "ok"}.get(a["level"], "card")
            out.append(f"<div class='card {cls}'>{_e(a['text'])}</div>")
        if p["active"] and not p["prev"]:
            out.append("<div class='card muted'>لا توجد نسخة سابقة بها بيانات للمقارنة.</div>")
        out.append("<div class='card wrap'><table><tr><th>نسخة</th><th>الحالة</th><th>طلبات</th><th>فشل</th>"
                   "<th>زمن (ث)</th><th>تقييمات</th><th>سلبي</th><th>تكلفة</th></tr>")
        for v in p["versions"]:
            lat = f"{v['lat'] / 1000:.1f}" if v["lat"] else "-"
            out.append(f"<tr><td>v{v['version']}</td><td>{_e(v['status'])}</td><td>{v['runs']}</td>"
                       f"<td>{_pct(v['failed'], v['runs'])}</td><td>{lat}</td><td>{v['ratings']}</td>"
                       f"<td>{_pct(v['neg'], v['ratings'])}</td><td>${v['cost']:.3f}</td></tr>")
        out.append("</table>")
        if p["active"] and p["prev"]:
            out.append(
                f"<form method='post' action='/admin/monitor/rollback' style='margin-top:8px' "
                f"onsubmit=\"return confirm('سيتغير سلوك التطبيق عند كل المستخدمين فوراً. متأكد؟')\">"
                f"<input type='hidden' name='key' value='{_e(key)}'>"
                f"<input type='hidden' name='name' value='{_e(p['name'])}'>"
                f"<input type='hidden' name='version' value='{p['prev']['version']}'>"
                f"<button class='btn'>تراجع إلى v{p['prev']['version']}</button></form>")
        out.append("</div>")

    out.append("<h2>تكلفة كل ميزة (متوسط آخر 30 يوماً)</h2>")
    out.append("<div class='card wrap'><table><tr><th>الميزة</th><th>طلبات</th><th>المتوسط</th><th>الأعلى</th></tr>")
    for f in data["features"]:
        if f["avg"] is None:
            out.append(f"<tr><td>{_e(f['label'])}</td><td>0</td><td>-</td><td>-</td></tr>")
        else:
            out.append(f"<tr><td>{_e(f['label'])}</td><td>{f['runs']}</td>"
                       f"<td>${f['avg']:.4f}</td><td>${f['max']:.4f}</td></tr>")
    out.append("</table><div class='muted'>لا يشمل رسم بحث Google (يُحسب لكل طلب بحث مؤهَّل بعد الحد المجاني، "
               "تحقق من صفحة التسعير). والمتوسط يخلط الطلبات النصية بالمصورة في الدردشة، فاستعمل جدول "
               "'آخر الطلبات' لقياس الصور والملفات منفردة.</div></div>")

    out.append("<h2>آخر 20 طلباً (للقياس المباشر)</h2>")
    out.append("<div class='card wrap'><table><tr><th>الوقت</th><th>Prompt</th><th>توكن دخل</th>"
               "<th>توكن خرج</th><th>تكلفة</th><th>زمن (ث)</th></tr>")
    for r in data["recent"]:
        cost = f"${r['cost']:.4f}" if r["cost"] is not None else "-"
        lat = f"{r['lat'] / 1000:.1f}" if r["lat"] else "-"
        bad = "" if r["ok"] else " class='bad'"
        out.append(f"<tr{bad}><td>{_e(r['at'])}</td><td>{_e(r['prompt'])} v{r['version']}</td>"
                   f"<td>{r['tin'] if r['tin'] is not None else '-'}</td>"
                   f"<td>{r['tout'] if r['tout'] is not None else '-'}</td><td>{cost}</td><td>{lat}</td></tr>")
    out.append("</table></div>")

    out.append("<h2>آخر 14 يوماً</h2>")
    if data["trend"]:
        out.append("<div class='card wrap'><table><tr><th>اليوم</th><th>طلبات</th><th>فشل</th>"
                   "<th>تقييمات</th><th>سلبي</th><th>تكلفة تقديرية</th></tr>")
        for t in data["trend"]:
            out.append(f"<tr><td>{_e(t['day'])}</td><td>{t['runs']}</td><td>{_pct(t['failed'], t['runs'])}</td>"
                       f"<td>{t['ratings']}</td><td>{_pct(t['neg'], t['ratings'])}</td><td>${t['cost']:.3f}</td></tr>")
        out.append("</table><div class='muted'>التكلفة تقدير: تحقق من أسعار Gemini الحالية وعدّلها في Render.</div></div>")
    else:
        out.append("<div class='card muted'>لا بيانات بعد.</div>")
    out.append("</body></html>")
    return "".join(out)


@bp.route("/admin/monitor", methods=["GET"])
def admin_monitor():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    try:
        data = collect()
        if request.args.get("format") == "json":
            return jsonify({"ok": True, **data})
        return Response(render(data, request.args.get("msg"), request.values.get("key", "")),
                        mimetype="text/html; charset=utf-8")
    except Exception as e:
        print("MONITOR PAGE ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed.", "detail": repr(e)[:200]}), 500


@bp.route("/admin/monitor/rollback", methods=["POST"])
def admin_monitor_rollback():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    key = request.values.get("key", "")
    name = request.form.get("name", "")
    try:
        version = int(request.form.get("version", ""))
    except ValueError:
        return jsonify({"ok": False, "error": "Invalid version"}), 400
    try:
        ok = _d["engine"].activate_version(name, version)
        msg = f"تم التراجع: {name} صار v{version}." if ok else "تعذّر التراجع: النسخة غير موجودة."
    except Exception as e:
        print("MONITOR ROLLBACK ERROR:", repr(e))
        msg = "حدث خطأ أثناء التراجع."
    return redirect(f"/admin/monitor?key={quote(key)}&msg={quote(msg)}")
