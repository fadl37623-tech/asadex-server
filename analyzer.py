"""
SciGo Analyzer (المرحلة ب)

- يقرأ التقييمات السلبية التي لم تُحلَّل بعد، ويصنّفها بالذكاء الاصطناعي
  (التصنيف + أين يكمن العلاج: prompt / كود / واجهة / مصادر).
- يعرض صفحة تقرير واحدة: الخدمات، نسخ الـ prompts، أهم المشكلات، الأخطاء الفنية.
- لا يعدّل أي شيء في التطبيق. للقراءة والتشخيص فقط.

الاستخدام: افتح  /admin/report?key=ADMIN_KEY
وللتحليل: أضف  &analyze=1
وللبيانات الخام: أضف  &format=json
"""

import html
import json
import os
import re
import secrets
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, request

bp = Blueprint("analyzer", __name__)
_deps = {}

BATCH_SIZE = 15
MIN_NEGATIVE_FOR_CONFIDENCE = 10

CATEGORIES = {
    "scientific_error": "خطأ علمي",
    "unclear_explanation": "شرح غير واضح",
    "wrong_topic": "موضوعات خاطئة أو غير مناسبة",
    "too_simple": "بسيط جداً",
    "too_hard": "صعب جداً",
    "too_crowded": "مزدحم",
    "bad_images": "صور غير مناسبة",
    "bad_sources": "مصادر غير مفيدة",
    "ui_problem": "مشكلة في الواجهة",
    "wrong_language": "لغة خاطئة",
    "format_problem": "مشكلة في التنسيق",
    "other": "أخرى",
}

CAUSES = {
    "prompt": "يُعالج بتعديل الـ prompt",
    "code": "يحتاج تعديل كود",
    "ui": "يحتاج تعديل الواجهة",
    "data": "مصادر/صور (كود البحث)",
    "unclear": "غير واضح بعد",
}

FEATURE_NAMES = {"chat": "الدردشة", "plan": "الخطة", "quiz": "الاختبار", "report": "التقرير"}


def init(app, get_conn, run_internal):
    _deps.update(get_conn=get_conn, run_internal=run_internal)
    _create_tables()
    app.register_blueprint(bp)


def _create_tables():
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS feedback_analysis (
                feedback_id BIGINT PRIMARY KEY,
                category TEXT NOT NULL,
                cause TEXT NOT NULL,
                summary TEXT,
                analyzer_version INTEGER,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        print("ANALYZER TABLE ERROR:", repr(e))
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
# التصنيف
# ============================================================

def parse_analysis(text, valid_ids):
    """يحوّل رد النموذج إلى [(id, category, cause, summary)] بعد تنظيف القيم."""
    text = re.sub(r"^`(?:json)?\s*|\s*```$", "", (text or "").strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("No JSON in analyzer response")
    data = json.loads(text[start:end + 1])

    found = {}
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
        fid = item.get("id")
        if isinstance(fid, bool) or not isinstance(fid, int) or fid not in valid_ids:
            continue
        category = item.get("category")
        cause = item.get("cause")
        found.setdefault(fid, (
            fid,
            category if category in CATEGORIES else "other",
            cause if cause in CAUSES else "unclear",
            str(item.get("summary") or "")[:300],
        ))
    return list(found.values())


def analyze_pending(limit=BATCH_SIZE):
    """يصنّف حتى `limit` تقييماً سلبياً جديداً. يرجع عدد ما صُنّف."""
    conn = None
    try:
        conn = _deps["get_conn"]()
        c = conn.cursor()
        c.execute(
            """
            SELECT f.id, f.feature, f.reasons, f.comment, f.context
            FROM answer_feedback f
            LEFT JOIN feedback_analysis a ON a.feedback_id = f.id
            WHERE f.rating = -1 AND a.feedback_id IS NULL
            ORDER BY f.id
            LIMIT %s
            """,
            (limit,),
        )
        rows = c.fetchall()
        if not rows:
            return 0

        items = []
        for fid, feature, reasons, comment, context in rows:
            items.append({
                "id": fid,
                "feature": feature,
                "reasons": reasons if isinstance(reasons, list) else [],
                "comment": (comment or "")[:500],
                "context": json.dumps(context or {}, ensure_ascii=False)[:1500],
            })

        text, version, _ = _deps["run_internal"](
            "analyzer_classify", json.dumps({"items": items}, ensure_ascii=False)
        )
        results = parse_analysis(text, {r[0] for r in rows})

        for fid, category, cause, summary in results:
            c.execute(
                """
                INSERT INTO feedback_analysis (feedback_id, category, cause, summary, analyzer_version)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (feedback_id) DO NOTHING
                """,
                (fid, category, cause, summary, version),
            )
        conn.commit()
        return len(results)
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()


# ============================================================
# جمع البيانات
# ============================================================

def collect():
    conn = _deps["get_conn"]()
    try:
        c = conn.cursor()
        data = {}

        c.execute("""
            SELECT feature, COUNT(*), COUNT(*) FILTER (WHERE rating = -1)
            FROM answer_feedback GROUP BY feature ORDER BY feature
        """)
        data["features"] = [
            {"feature": f, "total": t, "negative": n} for f, t, n in c.fetchall()
        ]
        data["total_ratings"] = sum(x["total"] for x in data["features"])
        data["total_negative"] = sum(x["negative"] for x in data["features"])

        c.execute("""
            SELECT f.feature, reason, COUNT(*)
            FROM answer_feedback f
            CROSS JOIN LATERAL jsonb_array_elements_text(f.reasons) AS reason
            WHERE f.rating = -1 AND jsonb_typeof(f.reasons) = 'array'
            GROUP BY f.feature, reason
            ORDER BY COUNT(*) DESC
        """)
        reasons = {}
        for feature, reason, n in c.fetchall():
            reasons.setdefault(feature, []).append({"reason": reason, "count": n})
        data["reasons"] = reasons

        c.execute("""
            SELECT prompt_name, prompt_version, COUNT(*), COUNT(*) FILTER (WHERE rating = -1)
            FROM answer_feedback WHERE prompt_name IS NOT NULL
            GROUP BY prompt_name, prompt_version
        """)
        fb = {(n, v): (t, ng) for n, v, t, ng in c.fetchall()}

        c.execute("""
            SELECT prompt_name, prompt_version,
                   COUNT(*) FILTER (WHERE NOT cached),
                   COUNT(*) FILTER (WHERE NOT success),
                   COUNT(*) FILTER (WHERE cached),
                   AVG(latency_ms) FILTER (WHERE NOT cached)
            FROM prompt_runs GROUP BY prompt_name, prompt_version
            ORDER BY prompt_name, prompt_version
        """)
        versions = []
        for n, v, runs, failed, cached, lat in c.fetchall():
            total, neg = fb.get((n, v), (0, 0))
            versions.append({
                "prompt": n, "version": v, "runs": runs, "failed": failed,
                "cache_hits": cached,
                "avg_latency_ms": round(float(lat), 0) if lat is not None else None,
                "ratings": total, "negative": neg,
            })
        data["versions"] = versions
        c.execute("""
            SELECT prompt_name, prompt_version, COALESCE(error_type, '?'),
                   COALESCE(LEFT(error_detail, 160), ''), COUNT(*)
            FROM prompt_runs WHERE NOT success
            GROUP BY 1, 2, 3, 4 ORDER BY COUNT(*) DESC LIMIT 15
        """)
        data["failures"] = [
            {"prompt": n, "version": v, "type": t, "detail": d, "count": k}
            for n, v, t, d, k in c.fetchall()
        ]

        c.execute("""
            SELECT f.feature, a.category, a.cause, COUNT(*),
                   (ARRAY_AGG(a.summary ORDER BY a.feedback_id DESC))[1]
            FROM feedback_analysis a
            JOIN answer_feedback f ON f.id = a.feedback_id
            GROUP BY f.feature, a.category, a.cause
            ORDER BY COUNT(*) DESC LIMIT 10
        """)
        data["issues"] = [
            {"feature": f, "category": cat, "cause": cause, "count": k, "sample": s or ""}
            for f, cat, cause, k, s in c.fetchall()
        ]

        c.execute("""
            SELECT COUNT(*) FROM answer_feedback f
            LEFT JOIN feedback_analysis a ON a.feedback_id = f.id
            WHERE f.rating = -1 AND a.feedback_id IS NULL
        """)
        data["pending"] = c.fetchone()[0]

        c.execute("""
            SELECT feature, comment, prompt_name, prompt_version, created_at
            FROM answer_feedback
            WHERE rating = -1 AND comment IS NOT NULL AND BTRIM(comment) <> ''
            ORDER BY created_at DESC LIMIT 10
        """)
        data["comments"] = [
            {"feature": f, "comment": cm, "prompt": n, "version": v,
             "at": at.isoformat() if at else None}
            for f, cm, n, v, at in c.fetchall()
        ]
        return data
    finally:
        conn.close()


# ============================================================
# العرض
# ============================================================

def _e(x):
    return html.escape(str(x if x is not None else ""))


def _pct(part, total):
    return f"{round(part / total * 100)}%" if total else "-"


def render_html(data, message=None, key=""):
    parts = []
    add = parts.append

    add("""<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asadex Report</title>
<style>
body{font-family:system-ui,-apple-system,Segoe UI,Tahoma,sans-serif;margin:0;padding:14px;background:#f5f6f8;color:#1c1c1e;line-height:1.55}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:22px 0 8px;color:#1565c0}
.card{background:#fff;border-radius:14px;padding:12px 14px;margin-bottom:10px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
.muted{color:#666;font-size:13px}.warn{background:#fff4e5;border:1px solid #ffb74d}
.ok{background:#e8f5e9;border:1px solid #81c784}
table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:6px 4px;text-align:right;border-bottom:1px solid #eee}
th{color:#555;font-weight:600}.bad{color:#c62828;font-weight:600}.tag{display:inline-block;padding:1px 8px;border-radius:10px;background:#e3f2fd;font-size:12px}
a.btn{display:inline-block;padding:9px 16px;background:#1565c0;color:#fff;border-radius:20px;text-decoration:none;font-size:14px}
.wrap{overflow-x:auto}
</style></head><body>""")

    add("<h1>تقرير جودة Asadex</h1>")
    add(f"<div class='muted'>إجمالي التقييمات: {data['total_ratings']} | السلبية: {data['total_negative']} "
        f"({_pct(data['total_negative'], data['total_ratings'])})</div>")

    if message:
        add(f"<div class='card ok'>{_e(message)}</div>")

    if data["total_negative"] < MIN_NEGATIVE_FOR_CONFIDENCE:
        add(f"<div class='card warn'>⚠️ العيّنة صغيرة (أقل من {MIN_NEGATIVE_FOR_CONFIDENCE} تقييمات سلبية). "
            "الأرقام إشارات مبكرة وليست أدلة كافية لاتخاذ قرار.</div>")
    # --- أهم المشكلات ---
    add("<h2>أهم المشكلات</h2>")
    if data["issues"]:
        add("<div class='card wrap'><table><tr><th>الخدمة</th><th>المشكلة</th><th>العدد</th><th>العلاج</th></tr>")
        for i in data["issues"][:5]:
            add(f"<tr><td>{_e(FEATURE_NAMES.get(i['feature'], i['feature']))}</td>"
                f"<td>{_e(CATEGORIES.get(i['category'], i['category']))}"
                f"<div class='muted'>{_e(i['sample'])}</div></td>"
                f"<td class='bad'>{i['count']}</td>"
                f"<td><span class='tag'>{_e(CAUSES.get(i['cause'], i['cause']))}</span></td></tr>")
        add("</table></div>")
    else:
        add("<div class='card muted'>لا يوجد تصنيف بعد.</div>")

    if data["pending"]:
        add(f"<div class='card'>تقييمات سلبية بانتظار التحليل: <b>{data['pending']}</b><br><br>"
            f"<a class='btn' href='?key={quote(key)}&analyze=1'>حلّل الآن</a></div>")

    # --- الخدمات ---
    add("<h2>الخدمات</h2><div class='card wrap'><table><tr><th>الخدمة</th><th>التقييمات</th><th>السلبية</th><th>أكثر الأسباب</th></tr>")
    for f in data["features"]:
        top = "، ".join(f"{_e(r['reason'])} ({r['count']})" for r in data["reasons"].get(f["feature"], [])[:3])
        add(f"<tr><td>{_e(FEATURE_NAMES.get(f['feature'], f['feature']))}</td><td>{f['total']}</td>"
            f"<td class='bad'>{f['negative']} ({_pct(f['negative'], f['total'])})</td><td>{top or '-'}</td></tr>")
    add("</table></div>")

    # --- نسخ الـ prompts ---
    add("<h2>نسخ الـ prompts</h2><div class='card wrap'><table><tr><th>Prompt</th><th>نسخة</th><th>طلبات</th>"
        "<th>فشل</th><th>كاش</th><th>زمن (ث)</th><th>تقييم سلبي</th></tr>")
    for v in data["versions"]:
        lat = f"{v['avg_latency_ms'] / 1000:.1f}" if v["avg_latency_ms"] else "-"
        failed = f"<span class='bad'>{v['failed']}</span>" if v["failed"] else "0"
        add(f"<tr><td>{_e(v['prompt'])}</td><td>v{v['version']}</td><td>{v['runs']}</td><td>{failed}</td>"
            f"<td>{v['cache_hits']}</td><td>{lat}</td>"
            f"<td>{v['negative']}/{v['ratings']}</td></tr>")
    add("</table></div>")

    # --- الأخطاء الفنية ---
    add("<h2>الأخطاء الفنية</h2>")
    if data["failures"]:
        add("<div class='card wrap'><table><tr><th>Prompt</th><th>نوع الخطأ</th><th>التفصيل</th><th>العدد</th></tr>")
        for x in data["failures"]:
            add(f"<tr><td>{_e(x['prompt'])} v{x['version']}</td><td>{_e(x['type'])}</td>"
                f"<td class='muted'>{_e(x['detail'])}</td><td class='bad'>{x['count']}</td></tr>")
        add("</table></div>")
    else:
        add("<div class='card ok'>لا توجد أخطاء مسجّلة.</div>")

    # --- تعليقات ---
    add("<h2>آخر التعليقات السلبية</h2>")
    if data["comments"]:
        for cm in data["comments"]:
            add(f"<div class='card'>{_e(cm['comment'])}"
                f"<div class='muted'>{_e(FEATURE_NAMES.get(cm['feature'], cm['feature']))} | "
                f"{_e(cm['prompt'] or '-')} v{_e(cm['version'] or '-')}</div></div>")
    else:
        add("<div class='card muted'>لا توجد تعليقات.</div>")

    add("</body></html>")
    return "".join(parts)


@bp.route("/admin/report", methods=["GET"])
def admin_report():
    if not _admin_ok():
        return jsonify({"ok": False, "error": "Unauthorized"}), 403

    message = None
    if request.args.get("analyze") == "1":
        try:
            n = analyze_pending()
            message = f"تم تحليل {n} تقييماً."
        except Exception as e:
            print("ANALYZE ERROR:", repr(e))
            message = "تعذّر التحليل الآن. حاول مرة أخرى بعد قليل."

    try:
        data = collect()
    except Exception as e:
        print("REPORT COLLECT ERROR:", repr(e))
        return jsonify({"ok": False, "error": "Failed."}), 500

    if request.args.get("format") == "json":
        return jsonify({"ok": True, **data})

    key = request.args.get("key", "")
    return Response(render_html(data, message, key), mimetype="text/html; charset=utf-8")
