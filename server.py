from flask import Flask, request, jsonify
import psycopg2
import hashlib
import re
import os

app = Flask(name)

DATABASE_URL = os.environ.get("DATABASE_URL")

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
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
