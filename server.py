from flask import Flask, request, jsonify
import psycopg2
import hashlib
import re
import os

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")

# ==== Added: daily limit settings ====
DAILY_LIMIT = 23  # allowed questions per day (text + images)

def get_conn():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def validate_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w{2,}$'
    return re.match(pattern, email.strip()) is not None

@app.route("/init", methods=["GET"])
def init_db():
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS students (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            name TEXT NOT NULL
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS questions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER,
            question TEXT,
            answer TEXT,
            date TEXT
        )''')
        # ==== Added: table to track each user's daily usage ====
        c.execute('''CREATE TABLE IF NOT EXISTS usage_limits (
            user_id INTEGER PRIMARY KEY,
            solve_count INTEGER NOT NULL DEFAULT 0,
            last_reset_time TIMESTAMP NOT NULL DEFAULT NOW()
        )''')
        conn.commit()
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

@app.route("/register", methods=["POST"])
def register():
    data = request.json
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    name = data.get("name", "").strip()

    if not validate_email(email):
        return jsonify({"ok": False, "msg": "Invalid email format"})
    if len(password) < 6:
        return jsonify({"ok": False, "msg": "Password must be at least 6 characters"})
    if len(name) < 2:
        return jsonify({"ok": False, "msg": "Name must be at least 2 characters"})

    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO students (email, password, name) VALUES (%s, %s, %s)",
            (email, hash_password(password), name)
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "msg": ""})
    except psycopg2.errors.UniqueViolation:
        return jsonify({"ok": False, "msg": "This email is already registered"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")

    if not validate_email(email):
        return jsonify({"user": None, "msg": "Invalid email format"})
    if len(password) < 8:
        return jsonify({"user": None, "msg": "Password must be at least 8 characters"})

    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT * FROM students WHERE email=%s AND password=%s",
            (email, hash_password(password))
        )
        user = c.fetchone()
        conn.close()
        if user:
            return jsonify({"user": list(user), "msg": ""})
        return jsonify({"user": None, "msg": "Incorrect email or password"})
    except Exception as e:
        return jsonify({"user": None, "msg": str(e)})

@app.route("/get_user/<int:user_id>", methods=["GET"])
def get_user_by_id(user_id):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute("SELECT * FROM students WHERE id=%s", (user_id,))
        user = c.fetchone()
        conn.close()
        return jsonify({"user": list(user) if user else None})
    except Exception as e:
        return jsonify({"user": None, "msg": str(e)})
# ==== Added: endpoint to check the daily limit ====
@app.route("/check_limit/<int:user_id>", methods=["POST"])
def check_limit(user_id):
    """
    Called before answering any question (text or image).
    Returns allowed=True/False + a message + remaining questions.
    """
    import datetime
    try:
        conn = get_conn()
        c = conn.cursor()
        now = datetime.datetime.now()

        c.execute("SELECT solve_count, last_reset_time FROM usage_limits WHERE user_id=%s", (user_id,))
        row = c.fetchone()

        if row is None:
            c.execute(
                "INSERT INTO usage_limits (user_id, solve_count, last_reset_time) VALUES (%s, 1, %s)",
                (user_id, now)
            )
            conn.commit()
            conn.close()
            remaining = DAILY_LIMIT - 1
            return jsonify({"allowed": True, "remaining": remaining, "msg": f"Success! You have {remaining} questions left today."})

        solve_count, last_reset_time = row
        time_passed = now - last_reset_time

        if time_passed >= datetime.timedelta(hours=24):
            c.execute(
                "UPDATE usage_limits SET solve_count=1, last_reset_time=%s WHERE user_id=%s",
                (now, user_id)
            )
            conn.commit()
            conn.close()
            remaining = DAILY_LIMIT - 1
            return jsonify({"allowed": True, "remaining": remaining, "msg": f"New 24-hour cycle started! You have {remaining} questions left today."})

        if solve_count < DAILY_LIMIT:
            c.execute(
                "UPDATE usage_limits SET solve_count = solve_count + 1 WHERE user_id=%s",
                (user_id,)
            )
            conn.commit()
            conn.close()
            remaining = DAILY_LIMIT - (solve_count + 1)
            return jsonify({"allowed": True, "remaining": remaining, "msg": f"Success! You have {remaining} questions left today."})

        conn.close()
        time_to_wait = datetime.timedelta(hours=24) - time_passed
        hours, remainder = divmod(int(time_to_wait.total_seconds()), 3600)
        minutes = remainder // 60
        return jsonify({
            "allowed": False,
            "remaining": 0,
            "msg": f"Daily limit reached ({DAILY_LIMIT} questions). Come back tomorrow! {hours}h {minutes}m remaining."
        })

    except Exception as e:
        return jsonify({"allowed": False, "remaining": 0, "msg": str(e)}), 500

@app.route("/save_question", methods=["POST"])
def save_question():
    data = request.json
    from datetime import datetime
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO questions (user_id, question, answer, date) VALUES (%s, %s, %s, %s)",
            (data["user_id"], data["question"], data["answer"], date)
        )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})

@app.route("/get_questions/<int:user_id>", methods=["GET"])
def get_questions(user_id):
    try:
        conn = get_conn()
        c = conn.cursor()
        c.execute(
            "SELECT question, answer, date FROM questions WHERE user_id=%s ORDER BY id DESC",
            (user_id,)
        )
        rows = c.fetchall()
        conn.close()
        return jsonify({"questions": [list(r) for r in rows]})
    except Exception as e:
        return jsonify({"questions": [], "msg": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
