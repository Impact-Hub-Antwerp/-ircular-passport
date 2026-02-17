import os
import re
import sqlite3
from datetime import datetime
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

APP_NAME = "Digital Circular Passport"
TOTAL_ACTIVITIES = 50
QR_PREFIX = "CF26-"

DB_PATH = "passport.db"
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key")

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")


# ---------------- DATABASE ----------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS activities (
        code TEXT PRIMARY KEY,
        title TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS completions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        activity_code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, activity_code)
    )
    """)

    for i in range(1, 51):
        code = f"CF26-A{str(i).zfill(2)}"
        title = f"Activity {i}"
        cur.execute(
            "INSERT OR IGNORE INTO activities (code, title) VALUES (?, ?)",
            (code, title)
        )
        
    conn.commit()
    conn.close()


init_db()


# ---------------- HELPERS ----------------

def current_user(request: Request):
    return request.session.get("user_id")


def require_login(request: Request):
    user_id = current_user(request)
    if not user_id:
        raise HTTPException(status_code=401)
    return user_id


def get_progress(user_id: int):
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) as count FROM completions WHERE user_id=?", (user_id,))
    count = cur.fetchone()["count"]

    cur.execute("""
        SELECT a.code, a.title, c.created_at
        FROM completions c
        JOIN activities a ON a.code = c.activity_code
        WHERE c.user_id=?
        ORDER BY c.created_at DESC
    """, (user_id,))
    items = cur.fetchall()

    conn.close()
    return count, items


def valid_qr(code: str):
    if not code.startswith(QR_PREFIX):
        return False
    suffix = code[len(QR_PREFIX):]
    return bool(re.match(r"A\d{2}$", suffix))


# ---------------- ROUTES ----------------

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    if current_user(request):
        return RedirectResponse("/app", status_code=303)
    return RedirectResponse("/register", status_code=303)


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", {
        "request": request,
        "app_name": APP_NAME
    })


@app.post("/register")
def register(request: Request, name: str = Form(...), email: str = Form(...)):
    conn = get_db()
    cur = conn.cursor()

    email = email.strip().lower()

    cur.execute("SELECT id FROM users WHERE email=?", (email,))
    row = cur.fetchone()

    if row:
        user_id = row["id"]
    else:
        cur.execute(
            "INSERT INTO users (name, email, created_at) VALUES (?, ?, ?)",
            (name.strip(), email, datetime.utcnow().isoformat())
        )
        conn.commit()
        user_id = cur.lastrowid

    conn.close()
    request.session["user_id"] = user_id

    return RedirectResponse("/app", status_code=303)


@app.get("/app", response_class=HTMLResponse)
def app_page(request: Request):
    user_id = require_login(request)

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, email FROM users WHERE id=?", (user_id,))
    user = cur.fetchone()
    conn.close()

    count, items = get_progress(user_id)

    return templates.TemplateResponse("app.html", {
        "request": request,
        "app_name": APP_NAME,
        "user": user,
        "count": count,
        "total": TOTAL_ACTIVITIES,
        "items": items[:10]
    })


@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request):
    require_login(request)
    return templates.TemplateResponse("scan.html", {
        "request": request,
        "prefix": QR_PREFIX
    })


@app.post("/api/scan")
def scan_api(request: Request, code: str = Form(...)):
    user_id = require_login(request)
    code = code.strip()

    if not valid_qr(code):
        return JSONResponse({"ok": False, "error": "Invalid QR"}, status_code=400)

    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT code FROM activities WHERE code=?", (code,))
    if not cur.fetchone():
        conn.close()
        return JSONResponse({"ok": False, "error": "Unknown activity"}, status_code=404)

    try:
        cur.execute(
            "INSERT INTO completions (user_id, activity_code, created_at) VALUES (?, ?, ?)",
            (user_id, code, datetime.utcnow().isoformat())
        )
        conn.commit()
        added = True
    except sqlite3.IntegrityError:
        added = False

    cur.execute("SELECT COUNT(*) as count FROM completions WHERE user_id=?", (user_id,))
    count = cur.fetchone()["count"]

    conn.close()

    return {
        "ok": True,
        "added": added,
        "count": count,
        "total": TOTAL_ACTIVITIES
    }


@app.get("/progress", response_class=HTMLResponse)
def progress_page(request: Request):
    user_id = require_login(request)
    count, items = get_progress(user_id)

    return templates.TemplateResponse("progress.html", {
        "request": request,
        "count": count,
        "total": TOTAL_ACTIVITIES,
        "items": items
    })
