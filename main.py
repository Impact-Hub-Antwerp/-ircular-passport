import os
import re
from datetime import datetime

import psycopg2
from psycopg2.extras import RealDictCursor

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from fastapi.staticfiles import StaticFiles


APP_NAME = "Digital Circular Passport"
TOTAL_ACTIVITIES = 50
QR_PREFIX = "CF26-"

SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-key")

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
templates = Jinja2Templates(directory="templates")


def get_db():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg2.connect(database_url)


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
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
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        activity_code TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(user_id, activity_code)
    )
    """)

    for i in range(1, TOTAL_ACTIVITIES + 1):
        code = f"CF26-A{str(i).zfill(2)}"
        title = f"Activity {i}"
        cur.execute(
            "INSERT INTO activities (code, title) VALUES (%s, %s) ON CONFLICT (code) DO NOTHING",
            (code, title),
        )

    conn.commit()
    cur.close()
    conn.close()


@app.on_event("startup")
def on_startup():
    init_db()


def current_user(request: Request):
    return request.session.get("user_id")


def require_login(request: Request):
    user_id = current_user(request)
    if not user_id:
        raise HTTPException(status_code=401)
    return user_id


def get_progress(user_id: int):
    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT COUNT(*) AS count FROM completions WHERE user_id=%s", (user_id,))
    count = cur.fetchone()["count"]

    cur.execute("""
        SELECT a.code, a.title, c.created_at
        FROM completions c
        JOIN activities a ON a.code = c.activity_code
        WHERE c.user_id=%s
        ORDER BY c.created_at DESC
    """, (user_id,))
    items = cur.fetchall()

    cur.close()
    conn.close()
    return count, items


def valid_qr(code: str):
    if not code.startswith(QR_PREFIX):
        return False
    suffix = code[len(QR_PREFIX):]
    return bool(re.match(r"A\d{2}$", suffix))


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
    cur = conn.cursor(cursor_factory=RealDictCursor)

    email = email.strip().lower()
    name = name.strip()

    cur.execute("SELECT id FROM users WHERE email=%s", (email,))
    row = cur.fetchone()

    if row:
        user_id = row["id"]
    else:
        cur.execute(
            "INSERT INTO users (name, email, created_at) VALUES (%s, %s, %s) RETURNING id",
            (name, email, datetime.utcnow().isoformat())
        )
        user_id = cur.fetchone()["id"]
        conn.commit()

    cur.close()
    conn.close()

    request.session["user_id"] = user_id
    return RedirectResponse("/app", status_code=303)


@app.get("/app", response_class=HTMLResponse)
def app_page(request: Request):
    user_id = require_login(request)

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute("SELECT name, email FROM users WHERE id=%s", (user_id,))
    user = cur.fetchone()
    cur.close()
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
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT code FROM activities WHERE code=%s", (code,))
    if not cur.fetchone():
        cur.close()
        conn.close()
        return JSONResponse({"ok": False, "error": "Unknown activity"}, status_code=404)

    cur.execute(
        "INSERT INTO completions (user_id, activity_code, created_at) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, activity_code) DO NOTHING RETURNING id",
        (user_id, code, datetime.utcnow().isoformat())
    )
    added = cur.fetchone() is not None
    conn.commit()

    cur.execute("SELECT COUNT(*) AS count FROM completions WHERE user_id=%s", (user_id,))
    count = cur.fetchone()["count"]

    cur.close()
    conn.close()

    return {"ok": True, "added": added, "count": count, "total": TOTAL_ACTIVITIES}


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


@app.get("/admin/students", response_class=HTMLResponse)
def admin_students(request: Request, key: str = ""):
    if key != os.getenv("ADMIN_KEY", ""):
        return PlainTextResponse("Forbidden", status_code=403)

    conn = get_db()
    cur = conn.cursor(cursor_factory=RealDictCursor)

    cur.execute("SELECT COUNT(*) AS n FROM users")
    total_users = cur.fetchone()["n"]

    cur.execute("""
        SELECT u.name AS name, u.email AS email, COUNT(c.id) AS cnt
        FROM users u
        LEFT JOIN completions c ON c.user_id = u.id
        GROUP BY u.id
        ORDER BY cnt DESC, u.name ASC
    """)
    rows = cur.fetchall()

    cur.close()
    conn.close()

    return templates.TemplateResponse("admin_students.html", {
        "request": request,
        "rows": rows,
        "total": TOTAL_ACTIVITIES,
        "total_users": total_users
    })
@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/register", status_code=303)
