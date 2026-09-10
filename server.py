from flask import Flask, request, jsonify, redirect
from authlib.integrations.flask_client import OAuth
import psycopg2
import hashlib
import re
import os
import httpx

# ============================================================
# Flask
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    os.urandom(32)
)

DATABASE_URL = os.environ.get("DATABASE_URL")

GOOGLE_ANDROID_CLIENT_ID = os.environ.get(
    "GOOGLE_ANDROID_CLIENT_ID"
)

GOOGLE_ANDROID_CLIENT_SECRET = os.environ.get(
    "GOOGLE_ANDROID_CLIENT_SECRET"
)

GOOGLE_CLIENT_ID = os.environ.get(
    "GOOGLE_CLIENT_ID"
)

GOOGLE_CLIENT_SECRET = os.environ.get(
    "GOOGLE_CLIENT_SECRET"
)
# ============================================================
# Google OAuth
# ============================================================

oauth = OAuth(app)

google = oauth.register(
    name="google",
    client_id=os.environ.get("GOOGLE_CLIENT_ID"),
    client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
    server_metadata_url=(
        "https://accounts.google.com/"
        ".well-known/openid-configuration"
    ),
    client_kwargs={
        "scope": "openid email profile"
    },
)

# ============================================================
# Settings
# ============================================================

DAILY_LIMIT = 23

BASE_URL = "https://asadex-server.onrender.com"

# ============================================================
# Database
# ============================================================


def get_conn():
    return psycopg2.connect(
        DATABASE_URL,
        sslmode="require"
    )


def hash_password(password):
    return hashlib.sha256(
        password.encode()
    ).hexdigest()


# ============================================================
# Validation
# ============================================================


def validate_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w{2,}$'

    return (
        re.match(
            pattern,
            email.strip()
        )
        is not None
    )


# ============================================================
# Google OAuth
# ============================================================


@app.route(
    "/auth/google",
    methods=["GET"]
)
def google_login():

    redirect_uri = (
        f"{BASE_URL}/auth/google/callback"
    )

    return google.authorize_redirect(
        redirect_uri
    )


@app.route(
    "/auth/google/callback",
    methods=["GET"]
)
def google_callback():

    try:

        token = google.authorize_access_token()

        user_info = token.get("userinfo")

        if not user_info:

            user_info = google.userinfo(
                token=token
            )

        google_id = str(
            user_info.get("sub", "")
        ).strip()

        email = (
            user_info.get(
                "email",
                ""
            )
            .strip()
            .lower()
        )

        name = (
            user_info.get(
                "name",
                ""
            )
            .strip()
        )

        if not google_id:

            return jsonify({
                "ok": False,
                "msg": "Google ID is missing."
            }), 400

        if not validate_email(email):

            return jsonify({
                "ok": False,
                "msg": "Invalid Google email."
            }), 400

        if not name:

            name = email.split("@")[0]

        conn = get_conn()
        c = conn.cursor()

        c.execute(
            """
            SELECT *
            FROM students
            WHERE google_id=%s
            """,
            (google_id,)
        )

        user = c.fetchone()

        if user:

            conn.close()
            return jsonify({
                "ok": True,
                "user": list(user),
                "google_id": google_id,
                "msg": "Google login successful"
            })

        c.execute(
            """
            SELECT *
            FROM students
            WHERE email=%s
            """,
            (email,)
        )

        user = c.fetchone()

        if user:

            user_id = user[0]
            c.execute(
                """
                UPDATE students
                SET google_id=%s
                WHERE id=%s
                """,
                (
                    google_id,
                    user_id
                )
            )

            conn.commit()

            c.execute(
                """
                SELECT *
                FROM students
                WHERE id=%s
                """,
                (user_id,)
            )

            user = c.fetchone()

            conn.close()

            return jsonify({
                "ok": True,
                "user": list(user),
                "google_id": google_id,
                "msg": (
                    "Google account linked "
                    "to existing account"
                )
            })

        random_password = os.urandom(
            32
        ).hex()

        c.execute(
            """
            INSERT INTO students
            (
                email,
                password,
                name,
                google_id
            )
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (
                email,
                hash_password(
                    random_password
                ),
                name,
                google_id
            )
        )

        user = c.fetchone()

        conn.commit()
        conn.close()

        return jsonify({
            "ok": True,
            "user": list(user),
            "google_id": google_id,
            "msg": (
                "Google account "
                "created successfully"
            )
        })

    except Exception as e:

        print(
            "Google callback error:",
            e
        )

        return jsonify({
            "ok": False,
            "msg": str(e)
        }), 500


# ============================================================
# Google login API
# ============================================================

@app.route(
    "/google-login",
    methods=["POST"]
)
def google_login_api():

    data = request.json or {}

    google_id = str(
        data.get(
            "google_id",
            ""
        )
    ).strip()

    email = (
        data.get(
            "email",
            ""
        )
        .strip()
        .lower()
    )

    name = (
        data.get(
            "name",
            ""
        )
        .strip()
    )

    if not google_id:

        return jsonify({
            "user": None,
            "msg": "Google ID is missing"
        }), 400

    if not validate_email(email):

        return jsonify({
            "user": None,
            "msg": "Invalid Google email"
        }), 400

    if not name:

        name = email.split("@")[0]

    try:

        conn = get_conn()
        c = conn.cursor()

        c.execute(
            """
            SELECT *
            FROM students
            WHERE google_id=%s
            """,
            (google_id,)
        )

        user = c.fetchone()

        if user:

            conn.close()

            return jsonify({
                "user": list(user),
                "google_id": google_id,
                "msg": ""
            })

        c.execute(
            """
            SELECT *
            FROM students
            WHERE email=%s
            """,
            (email,)
        )

        user = c.fetchone()

        if user:

            user_id = user[0]

            c.execute(
                """
                UPDATE students
                SET google_id=%s
                WHERE id=%s
                """,
                (
                    google_id,
                    user_id
                )
            )

            conn.commit()

            c.execute(
                """
                SELECT *
                FROM students
                WHERE id=%s
                """,
                (user_id,)
            )

            user = c.fetchone()

            conn.close()
            return jsonify({
                "user": list(user),
                "google_id": google_id,
                "msg": ""
            })

        random_password = os.urandom(
            32
        ).hex()

        c.execute(
            """
            INSERT INTO students
            (
                email,
                password,
                name,
                google_id
            )
            VALUES (%s, %s, %s, %s)
            RETURNING *
            """,
            (
                email,
                hash_password(
                    random_password
                ),
                name,
                google_id
            )
        )

        user = c.fetchone()

        conn.commit()
        conn.close()

        return jsonify({
            "user": list(user),
            "google_id": google_id,
            "msg": ""
        })

    except Exception as e:

        print(
            "Google API error:",
            e
        )

        return jsonify({
            "user": None,
            "msg": str(e)
        }), 500


# ============================================================
# Mobile Google OAuth - Exchange code for token (Android/iOS)
#
# يُستخدم من تطبيق Flet (Android/iOS) بدل تبادل الكود مباشرة
# مع جوجل، حتى لا يحتوي التطبيق على GOOGLE_CLIENT_SECRET.
# السر يبقى محفوظاً هنا فقط كمتغير بيئة على السيرفر.
# ============================================================


def upsert_google_user(google_id, email, name):
    """
    نفس منطق البحث/الربط/الإنشاء المستخدم بـ /google-login
    """

    conn = get_conn()
    c = conn.cursor()

    c.execute(
        """
        SELECT *
        FROM students
        WHERE google_id=%s
        """,
        (google_id,)
    )

    user = c.fetchone()

    if user:
        conn.close()
        return list(user)

    c.execute(
        """
        SELECT *
        FROM students
        WHERE email=%s
        """,
        (email,)
    )

    user = c.fetchone()

    if user:
        user_id = user[0]

        c.execute(
            """
            UPDATE students
            SET google_id=%s
            WHERE id=%s
            """,
            (google_id, user_id)
        )

        conn.commit()

        c.execute(
            """
            SELECT *
            FROM students
            WHERE id=%s
            """,
            (user_id,)
        )

        user = c.fetchone()
        conn.close()
        return list(user)

    random_password = os.urandom(32).hex()

    c.execute(
        """
        INSERT INTO students
        (email, password, name, google_id)
        VALUES (%s, %s, %s, %s)
        RETURNING *
        """,
        (
            email,
            hash_password(random_password),
            name,
            google_id
        )
    )

    user = c.fetchone()
    conn.commit()
    conn.close()

    return list(user)


@app.route(
    "/google/mobile-exchange",
    methods=["POST"]
)
def google_mobile_exchange():

    data = request.json or {}

    code = data.get("code")
    code_verifier = data.get("code_verifier")
    redirect_uri = data.get("redirect_uri")

    if not code or not code_verifier or not redirect_uri:

        return jsonify({
            "ok": False,
            "msg": "Missing code, code_verifier or redirect_uri"
        }), 400

    try:

        token_response = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_ANDROID_CLIENT_ID,
                "client_secret": GOOGLE_ANDROID_CLIENT_SECRET,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=20
        )
        if token_response.status_code != 200:

            print(
                "Mobile token exchange failed:",
                token_response.text
            )
            return jsonify({
                "ok": False,
                "msg": f"Token exchange failed: {token_response.text}"
            }), 400

        token_data = token_response.json()
        access_token = token_data.get("access_token")

        if not access_token:

            return jsonify({
                "ok": False,
                "msg": "Google did not return access_token."
            }), 400

        userinfo_response = httpx.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=20
        )

        userinfo_response.raise_for_status()

        user_info = userinfo_response.json()

        google_id = str(user_info.get("sub", "")).strip()
        email = user_info.get("email", "").strip().lower()
        name = user_info.get("name", "").strip()

        if not google_id or not validate_email(email):

            return jsonify({
                "ok": False,
                "msg": "Invalid Google account data"
            }), 400

        if not name:
            name = email.split("@")[0]

        user = upsert_google_user(google_id, email, name)

        return jsonify({
            "ok": True,
            "user": user,
            "google_id": google_id,
            "msg": ""
        })

    except Exception as e:

        print("Mobile Google exchange error:", e)

        return jsonify({
            "ok": False,
            "msg": str(e)
        }), 500


# ============================================================
# Initialize database
# ============================================================


@app.route(
    "/init",
    methods=["GET"]
)
def init_db():

    try:

        conn = get_conn()
        c = conn.cursor()

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS students (
                id SERIAL PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                name TEXT NOT NULL,
                google_id TEXT
            )
            """
        )

        c.execute(
            """
            ALTER TABLE students
            ADD COLUMN IF NOT EXISTS google_id TEXT
            """
        )

        c.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS
            students_google_id_unique
            ON students (google_id)
            WHERE google_id IS NOT NULL
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS questions (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                question TEXT,
                answer TEXT,
                date TEXT
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_limits (
                user_id INTEGER PRIMARY KEY,
                solve_count INTEGER NOT NULL DEFAULT 0,
                last_reset_time
                TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )

        c.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                user_id INTEGER,
                rating INTEGER,
                feedback_type TEXT,
                message TEXT,
                date TIMESTAMP DEFAULT NOW()
            )
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS usage_limits_v2 (
                user_id INTEGER PRIMARY KEY,
                heavy_count INTEGER NOT NULL DEFAULT 0,
                heavy_locked_until TIMESTAMP,
                image_convert_count INTEGER NOT NULL DEFAULT 0,
                drawing_count INTEGER NOT NULL DEFAULT 0,
                similar_challenge_count INTEGER NOT NULL DEFAULT 0,
                daily_reset_time TIMESTAMP NOT NULL DEFAULT NOW()
            )
            """
        )
        c.execute(
            """
            ALTER TABLE usage_limits_v2
            ADD COLUMN IF NOT EXISTS grace_text_count INTEGER NOT NULL DEFAULT 0
            """
        )
        c.execute(
            """
            ALTER TABLE usage_limits_v2
            ADD COLUMN IF NOT EXISTS grace_feature_used BOOLEAN NOT NULL DEFAULT FALSE
            """
        )
        conn.commit()
        conn.close()

        return jsonify({
            "status": "ok"
        })

    except Exception as e:

        print(
            "Database init error:",
            e
        )

        return jsonify({
            "status": "error",
            "msg": str(e)
        }), 500


# ============================================================
# Register
# ============================================================


@app.route(
    "/register",
    methods=["POST"]
)
def register():

    data = request.json or {}

    email = (
        data.get(
            "email",
            ""
        )
        .strip()
        .lower()
    )
    password = data.get(
        "password",
        ""
    )

    name = (
        data.get(
            "name",
            ""
        )
        .strip()
    )

    if not validate_email(email):

        return jsonify({
            "ok": False,
            "msg": "Invalid email format"
        })

    if len(password) < 8:

        return jsonify({
            "ok": False,
            "msg": (
                "Password must be at least "
                "8 characters"
            )
        })

    if len(name) < 2:

        return jsonify({
            "ok": False,
            "msg": (
                "Name must be at least "
                "2 characters"
            )
        })

    try:

        conn = get_conn()
        c = conn.cursor()

        c.execute(
            """
            INSERT INTO students
            (
                email,
                password,
                name
            )
            VALUES (%s, %s, %s)
            """,
            (
                email,
                hash_password(password),
                name
            )
        )

        conn.commit()
        conn.close()

        return jsonify({
            "ok": True,
            "msg": ""
        })

    except psycopg2.errors.UniqueViolation:

        return jsonify({
            "ok": False,
            "msg": (
                "This email is already registered"
            )
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "msg": str(e)
        })


# ============================================================
# Login
# ============================================================


@app.route(
    "/login",
    methods=["POST"]
)
def login():

    data = request.json or {}

    email = (
        data.get(
            "email",
            ""
        )
        .strip()
        .lower()
    )

    password = data.get(
        "password",
        ""
    )

    if not validate_email(email):

        return jsonify({
            "user": None,
            "msg": "Invalid email format"
        })

    if len(password) < 8:
        return jsonify({
            "user": None,
            "msg": (
                "Password must be at least "
                "8 characters"
            )
        })

    try:

        conn = get_conn()
        c = conn.cursor()

        c.execute(
            """
            SELECT *
            FROM students
            WHERE email=%s
            AND password=%s
            """,
            (
                email,
                hash_password(password)
            )
        )

        user = c.fetchone()

        conn.close()

        if user:

            return jsonify({
                "user": list(user),
                "msg": ""
            })

        return jsonify({
            "user": None,
            "msg": (
                "Incorrect email or password"
            )
        })

    except Exception as e:

        return jsonify({
            "user": None,
            "msg": str(e)
        })


# ============================================================
# Get user
# ============================================================


@app.route(
    "/get_user/<int:user_id>",
    methods=["GET"]
)
def get_user_by_id(user_id):

    try:

        conn = get_conn()
        c = conn.cursor()

        c.execute(
            """
            SELECT *
            FROM students
            WHERE id=%s
            """,
            (user_id,)
        )

        user = c.fetchone()

        conn.close()

        return jsonify({
            "user": (
                list(user)
                if user
                else None
            )
        })

    except Exception as e:

        return jsonify({
            "user": None,
            "msg": str(e)
        })


# ============================================================
# Daily limit
# ============================================================


@app.route(
    "/check_limit/<int:user_id>",
    methods=["POST"]
)
def check_limit(user_id):

    import datetime

    try:
        conn = get_conn()
        c = conn.cursor()

        now = datetime.datetime.now()

        c.execute(
            """
            SELECT
                solve_count,
                last_reset_time
            FROM usage_limits
            WHERE user_id=%s
            """,
            (user_id,)
        )

        row = c.fetchone()

        if row is None:

            c.execute(
                """
                INSERT INTO usage_limits
                (
                    user_id,
                    solve_count,
                    last_reset_time
                )
                VALUES (%s, 1, %s)
                """,
                (
                    user_id,
                    now
                )
            )

            conn.commit()
            conn.close()

            remaining = DAILY_LIMIT - 1

            return jsonify({
                "allowed": True,
                "remaining": remaining,
                "msg": (
                    f"Success! You have "
                    f"{remaining} questions "
                    f"left today."
                )
            })

        solve_count, last_reset_time = row

        time_passed = (
            now - last_reset_time
        )

        if time_passed >= datetime.timedelta(
            hours=24
        ):

            c.execute(
                """
                UPDATE usage_limits
                SET
                    solve_count=1,
                    last_reset_time=%s
                WHERE user_id=%s
                """,
                (
                    now,
                    user_id
                )
            )

            conn.commit()
            conn.close()

            remaining = DAILY_LIMIT - 1
            return jsonify({
                "allowed": True,
                "remaining": remaining,
                "msg": (
                    "New 24-hour cycle "
                    "started! You have "
                    f"{remaining} questions "
                    "left today."
                )
            })

        if solve_count < DAILY_LIMIT:

            c.execute(
                """
                UPDATE usage_limits
                SET solve_count =
                    solve_count + 1
                WHERE user_id=%s
                """,
                (user_id,)
            )

            conn.commit()
            conn.close()

            remaining = (
                DAILY_LIMIT
                - (solve_count + 1)
            )

            return jsonify({
                "allowed": True,
                "remaining": remaining,
                "msg": (
                    f"Success! You have "
                    f"{remaining} questions "
                    "left today."
                )
            })

        conn.close()

        time_to_wait = (
            datetime.timedelta(hours=24)
            - time_passed
        )

        hours, remainder = divmod(
            int(
                time_to_wait.total_seconds()
            ),
            3600
        )

        minutes = remainder // 60

        return jsonify({
            "allowed": False,
            "remaining": 0,
            "msg": (
                f"Daily limit reached "
                f"({DAILY_LIMIT} questions). "
                "Come back tomorrow! "
                f"{hours}h {minutes}m "
                "remaining."
            )
        })

    except Exception as e:

        return jsonify({
            "allowed": False,
            "remaining": 0,
            "msg": str(e)
        }), 500


# ============================================================
# Save question
# ============================================================


@app.route(
    "/save_question",
    methods=["POST"]
)
def save_question():

    data = request.json or {}

    from datetime import datetime

    date = datetime.now().strftime(
        "%Y-%m-%d %H:%M"
    )

    try:

        conn = get_conn()
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO questions
            (
                user_id,
                question,
                answer,
                date
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                data["user_id"],
                data["question"],
                data["answer"],
                date
            )
        )

        conn.commit()
        conn.close()

        return jsonify({
            "ok": True
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "msg": str(e)
        })


# ============================================================
# Get questions
# ============================================================


@app.route(
    "/get_questions/<int:user_id>",
    methods=["GET"]
)
def get_questions(user_id):

    try:

        conn = get_conn()
        c = conn.cursor()

        c.execute(
            """
            SELECT
                question,
                answer,
                date
            FROM questions
            WHERE user_id=%s
            ORDER BY id DESC
            """,
            (user_id,)
        )

        rows = c.fetchall()

        conn.close()

        return jsonify({
            "questions": [
                list(row)
                for row in rows
            ]
        })

    except Exception as e:

        return jsonify({
            "questions": [],
            "msg": str(e)
        })


# ============================================================
# Feedback
# ============================================================


@app.route(
    "/feedback",
    methods=["POST"]
)
def submit_feedback():

    data = request.json or {}

    user_id = data.get(
        "user_id"
    )

    rating = data.get(
        "rating"
    )

    feedback_type = data.get(
        "feedback_type",
        "General feedback"
    )

    message = data.get(
        "message",
        ""
    ).strip()

    if not rating:

        return jsonify({
            "ok": False,
            "msg": (
                "Please select a rating."
            )
        }), 400

    if not message:

        return jsonify({
            "ok": False,
            "msg": (
                "Please write your feedback."
            )
        }), 400

    try:

        rating = int(rating)

        if rating < 1 or rating > 5:

            return jsonify({
                "ok": False,
                "msg": (
                    "Rating must be "
                    "between 1 and 5."
                )
            }), 400

    except Exception:

        return jsonify({
            "ok": False,
            "msg": "Invalid rating."
        }), 400

    try:

        conn = get_conn()
        c = conn.cursor()

        c.execute(
            """
            INSERT INTO feedback
            (
                user_id,
                rating,
                feedback_type,
                message
            )
            VALUES (%s, %s, %s, %s)
            """,
            (
                user_id,
                rating,
                feedback_type,
                message
            )
        )

        conn.commit()
        conn.close()

        return jsonify({
            "ok": True,
            "msg": (
                "Feedback submitted "
                "successfully."
            )
        })

    except Exception as e:

        return jsonify({
            "ok": False,
            "msg": str(e)
        }), 500


# ============================================================
# Android Google OAuth callback
# ============================================================

@app.route(
    "/oauth_callback",
    methods=["GET"]
)
def android_oauth_callback():

    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Asadex Google Login</title>
    </head>
    <body>
        <h3>Google login is processing...</h3>
        <p>You can return to the Asadex app.</p>
    </body>
    </html>
    """
# ============================================================
# Health check
# ============================================================


@app.route(
    "/",
    methods=["GET"]
)
def home():

    return jsonify({
        "status": "ok",
        "service": "Asadex Server"
    })

# ============================================================
# ضيفهن لملف السيرفر (app.py) - مو ملف منفصل، هذا فقط للمراجعة
# ============================================================

import datetime

# ─── إعدادات نظام النقاط الجديد ───────────────────────────────
HEAVY_LIMIT = 3            # نص/تقرير/صورة/ملف/لقطة شاشة - مجتمعين
HEAVY_LOCK_HOURS = 10       # مدة القفل الكامل بعد الوصول للحد

FEATURE_LIMITS = {
    "image_convert": 2,     # تحويل الجواب إلى صورة
    "drawing": 2,            # الرسم
    "similar_challenge": 3,  # Similar + Challenge me مجتمعين
}

DAILY_RESET_HOURS = 24


# ============================================================
# الحد "الثقيل" المشترك (3 طلبات -> قفل التطبيق كامل 10 ساعات)
# ============================================================

@app.route("/check_heavy_limit/<int:user_id>", methods=["POST"])
def check_heavy_limit(user_id):

    data = request.json or {}
    action_type = data.get("type", "unknown")  # للـ logging فقط
    weight = int(data.get("weight", 1))
    try:
        conn = get_conn()
        c = conn.cursor()
        now = datetime.datetime.now()

        c.execute(
            """
            SELECT heavy_count, heavy_locked_until, daily_reset_time
            FROM usage_limits_v2
            WHERE user_id=%s
            """,
            (user_id,)
        )
        row = c.fetchone()

        # ─── مستخدم جديد بالجدول ───
        if row is None:
            c.execute(
                """
                INSERT INTO usage_limits_v2
                (user_id, heavy_count, daily_reset_time)
                VALUES (%s, 1, %s)
                """,
                (user_id, now)
            )
            conn.commit()
            conn.close()
            return jsonify({
                "allowed": True,
                "remaining": HEAVY_LIMIT - 1,
                "msg": f"You have {HEAVY_LIMIT - 1} requests left before a 10-hour lock."
            })

        heavy_count, heavy_locked_until, daily_reset_time = row

        # ─── لسا مقفول من قبل؟ ───
        if heavy_locked_until is not None and now < heavy_locked_until:
            conn.close()
            remaining_time = heavy_locked_until - now
            hours, rem = divmod(int(remaining_time.total_seconds()), 3600)
            minutes = rem // 60
            return jsonify({
                "allowed": False,
                "remaining": 0,
                "msg": f"App is locked. Try again in {hours}h {minutes}m."
            })

        # ─── القفل انتهى مدته -> يصفّر ───
        if heavy_locked_until is not None and now >= heavy_locked_until:
            heavy_count = 0
            heavy_locked_until = None

        # ─── إعادة تصفير يومية عامة (24 ساعة من أول استخدام) ───
        if now - daily_reset_time >= datetime.timedelta(hours=DAILY_RESET_HOURS):
            heavy_count = 0
            daily_reset_time = now

        new_count = heavy_count + weight

        # ─── هذا الطلب يوصّل للحد -> يُسمح به، ويُقفل بعده ───
        if new_count >= HEAVY_LIMIT:
            lock_until = now + datetime.timedelta(hours=HEAVY_LOCK_HOURS)
            c.execute(
                """
                UPDATE usage_limits_v2
                SET heavy_count=%s, heavy_locked_until=%s, daily_reset_time=%s
                WHERE user_id=%s
                """,
                (new_count, lock_until, daily_reset_time, user_id)
            )
            conn.commit()
            conn.close()
            return jsonify({
                "allowed": True,
                "remaining": 0,
                "msg": f"This was your last request. App will lock for {HEAVY_LOCK_HOURS} hours."
            })

        # ─── طلب عادي، لسا ضمن الحد ───
        c.execute(
            """
            UPDATE usage_limits_v2
            SET heavy_count=%s, heavy_locked_until=NULL, daily_reset_time=%s
            WHERE user_id=%s
            """,
            (new_count, daily_reset_time, user_id)
        )
        conn.commit()
        conn.close()
        return jsonify({
            "allowed": True,
            "remaining": HEAVY_LIMIT - new_count,
            "msg": f"You have {HEAVY_LIMIT - new_count} requests left before a 10-hour lock."
        })

    except Exception as e:
        return jsonify({"allowed": False, "remaining": 0, "msg": str(e)}), 500

@app.route("/debug_reset_limit/<int:user_id>", methods=["POST", "GET"])
def debug_reset_limit(user_id):
    secret = request.args.get("key", "")
    if secret != os.environ.get("DEBUG_RESET_KEY", "change-me-please"):
        return jsonify({"ok": False, "msg": "Unauthorized"}), 403
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            """
            UPDATE usage_limits_v2
            SET heavy_count=0, heavy_locked_until=NULL,
                grace_text_count=0, grace_feature_used=FALSE
            WHERE user_id=%s
            """,
            (user_id,)
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "msg": "Limit reset."})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500
# ============================================================
# الحصص المنفصلة (تحويل لصورة / رسم / Similar+Challenge)
# ============================================================

@app.route("/check_feature_limit/<int:user_id>", methods=["POST"])
def check_feature_limit(user_id):

    data = request.json or {}
    feature = data.get("feature", "")

    if feature not in FEATURE_LIMITS:
        return jsonify({
            "allowed": False,
            "remaining": 0,
            "msg": f"Unknown feature: {feature}"
        }), 400

    column_map = {
        "image_convert": "image_convert_count",
        "drawing": "drawing_count",
        "similar_challenge": "similar_challenge_count",
    }
    column = column_map[feature]
    limit = FEATURE_LIMITS[feature]

    try:
        conn = get_conn()
        c = conn.cursor()
        now = datetime.datetime.now()

        c.execute(
            f"""
            SELECT {column}, daily_reset_time
            FROM usage_limits_v2
            WHERE user_id=%s
            """,
            (user_id,)
        )
        row = c.fetchone()

        if row is None:
            c.execute(
                f"""
                INSERT INTO usage_limits_v2
                (user_id, {column}, daily_reset_time)
                VALUES (%s, 1, %s)
                """,
                (user_id, now)
            )
            conn.commit()
            conn.close()
            return jsonify({
                "allowed": True,
                "remaining": limit - 1,
                "msg": f"{limit - 1} left today for this feature."
            })

        count, daily_reset_time = row

        # ─── تصفير يومي (24 ساعة) ───
        if now - daily_reset_time >= datetime.timedelta(hours=DAILY_RESET_HOURS):
            count = 0
            daily_reset_time = now

        if count >= limit:
            conn.close()
            time_left = datetime.timedelta(hours=DAILY_RESET_HOURS) - (now - daily_reset_time)
            hours, rem = divmod(int(time_left.total_seconds()), 3600)
            minutes = rem // 60
            return jsonify({
                "allowed": False,
                "remaining": 0,
                "msg": f"Daily limit reached for this feature. Resets in {hours}h {minutes}m."
            })

        new_count = count + 1

        c.execute(
            f"""
            UPDATE usage_limits_v2
            SET {column}=%s, daily_reset_time=%s
            WHERE user_id=%s
            """,
            (new_count, daily_reset_time, user_id)
        )
        conn.commit()
        conn.close()

        return jsonify({
            "allowed": True,
            "remaining": limit - new_count,
            "msg": f"{limit - new_count} left today for this feature."
        })

    except Exception as e:
        return jsonify({"allowed": False, "remaining": 0, "msg": str(e)}), 500

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            8080
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
