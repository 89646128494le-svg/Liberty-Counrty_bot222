import os
import hmac
import json
import time
import base64
import hashlib
import secrets
import asyncio
import subprocess
from typing import Dict, Any, Optional

from fastapi import FastAPI, Request, Form, Depends, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware
from itsdangerous import URLSafeSerializer
from sqlalchemy import create_engine, text, inspect, Table, MetaData
from sqlalchemy.exc import SQLAlchemyError

# -------------------------
# Конфиг
# -------------------------
PORT = int(os.getenv("OWNER_PORT", "9090"))
DB_PATH = os.getenv("DB_PATH", "liberty_country.db")

OWNER_USERNAME = os.getenv("OWNER_USERNAME", "admin")
OWNER_PASSWORD = os.getenv("OWNER_PASSWORD", "admin")
SESSION_SECRET = os.getenv("OWNER_SESSION_SECRET", "change_me_please_1234567890")
BROWSER_CHECK_SECRET = os.getenv("BROWSER_CHECK_SECRET", SESSION_SECRET)

BOT_PY    = os.getenv("BOT_PY", "liberty_country_bot.py")
ADMIN_PY  = os.getenv("ADMIN_PY", "lc_admin_app.py")
SITE_PY   = os.getenv("SITE_PY", "lc_main_site/lc_main_site.py")
LAUNCHER_PY = os.getenv("LAUNCHER_PY", "run_launcher.py")

# -------------------------
# App init
# -------------------------
app = FastAPI()
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax", https_only=False)

# статические (если захочешь положить лого/иконки)
if not os.path.isdir("static"):
    os.makedirs("static", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")

# шаблоны
if not os.path.isdir("templates"):
    os.makedirs("templates", exist_ok=True)
templates = Jinja2Templates(directory="templates")

# сериализатор для CSRF и browser-check
signer = URLSafeSerializer(SESSION_SECRET, salt="owner-portal")

# -------------------------
# Security headers middleware
# -------------------------
@app.middleware("send")
async def security_headers(request, call_next):
    response = await call_next
    headers = {
        "X-Frame-Options": "DENY",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "strict-origin-when-cross-origin",
        "Permissions-Policy": "geolocation=(), microphone=()",
        "Strict-Transport-Security": "max-age=63072000; includeSubDomains; preload",
        "Content-Security-Policy":
            "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self' ws:; frame-ancestors 'none';"
    }
    for k, v in headers.items():
        response.headers.setdefault(k, v)
    return response

# -------------------------
# Простая «проверка браузера»
# -------------------------
def has_browser_pass(request: Request) -> bool:
    return bool(request.cookies.get("browser_ok"))

@app.middleware("http")
async def browser_challenge(request: Request, call_next):
    path = request.url.path
    if path.startswith("/static") or path.startswith("/ws"):  # пропускаем статику и WS
        return await call_next(request)
    if path in ("/browser-check", "/browser-verify"):
        return await call_next(request)

    if not has_browser_pass(request):
        return RedirectResponse(url="/browser-check")
    return await call_next(request)

@app.get("/browser-check", response_class=HTMLResponse)
async def browser_check(request: Request):
    # генерим челлендж = HMAC(ts, secret)
    ts = str(int(time.time()))
    mac = hmac.new(BROWSER_CHECK_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
    return templates.TemplateResponse("browser_check.html", {"request": request, "ts": ts, "mac": mac})

@app.post("/browser-verify")
async def browser_verify(request: Request, ts: str = Form(...), mac: str = Form(...)):
    mac2 = hmac.new(BROWSER_CHECK_SECRET.encode(), ts.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac, mac2):
        raise HTTPException(400, "Browser check failed")
    resp = RedirectResponse("/", status_code=303)
    # cookie на 1 день
    resp.set_cookie("browser_ok", "1", max_age=86400, httponly=True, samesite="Lax")
    return resp

# -------------------------
# Аутентификация
# -------------------------
def require_auth(request: Request):
    if request.session.get("user") == OWNER_USERNAME:
        return
    raise HTTPException(401)

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # CSRF для формы
    csrf = signer.dumps({"at": time.time(), "rnd": secrets.token_hex(8)})
    request.session["csrf"] = csrf
    return templates.TemplateResponse("login.html", {"request": request, "csrf": csrf})

@app.post("/login")
async def do_login(request: Request, username: str = Form(...), password: str = Form(...), csrf: str = Form(...)):
    saved = request.session.get("csrf")
    if not saved or not secrets.compare_digest(saved, csrf):
        raise HTTPException(400, "Invalid CSRF")
    if username == OWNER_USERNAME and password == OWNER_PASSWORD:
        request.session["user"] = OWNER_USERNAME
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/login", status_code=303)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("browser_ok")
    return resp

# -------------------------
# Процессы (бот/админ/сайт)
# -------------------------
PROCS: Dict[str, subprocess.Popen] = {}   # key -> Popen

def start_proc(key: str, cmd: list[str]):
    if key in PROCS and PROCS[key].poll() is None:
        return False
    PROCS[key] = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return True

def stop_proc(key: str):
    p = PROCS.get(key)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(6)
        except subprocess.TimeoutExpired:
            p.kill()
    PROCS.pop(key, None)

def proc_state(key: str) -> str:
    p = PROCS.get(key)
    if not p:
        return "stopped"
    return "running" if p.poll() is None else "stopped"

# -------------------------
# База данных (SQLAlchemy + SQLite)
# -------------------------
engine = create_engine(f"sqlite:///{DB_PATH}", future=True)
metadata = MetaData()

def list_tables():
    insp = inspect(engine)
    return insp.get_table_names()

def table_rows(name: str, limit: int = 100, offset: int = 0):
    with engine.connect() as conn:
        result = conn.execute(text(f"SELECT * FROM '{name}' LIMIT :limit OFFSET :offset"),
                              {"limit": limit, "offset": offset})
        rows = [dict(r._mapping) for r in result]
        cols = list(rows[0].keys()) if rows else [c["name"] for c in inspect(engine).get_columns(name)]
        count = conn.execute(text(f"SELECT COUNT(*) AS c FROM '{name}'")).scalar_one()
        return cols, rows, count

def upsert_row(name: str, pk_col: Optional[str], payload: Dict[str, Any]):
    # если есть первичный ключ — UPDATE, иначе INSERT
    with engine.begin() as conn:
        if pk_col and pk_col in payload and payload[pk_col] not in (None, ""):
            pk_val = payload[pk_col]
            sets = ", ".join([f"\"{k}\" = :{k}" for k in payload if k != pk_col])
            q = text(f'UPDATE "{name}" SET {sets} WHERE "{pk_col}" = :pk')
            params = {**payload, "pk": pk_val}
            conn.execute(q, params)
        else:
            cols = ", ".join([f'"{k}"' for k in payload.keys()])
            vals = ", ".join([f":{k}" for k in payload.keys()])
            q = text(f'INSERT INTO "{name}" ({cols}) VALUES ({vals})')
            conn.execute(q, payload)

def delete_row(name: str, pk_col: str, pk_val: Any):
    with engine.begin() as conn:
        conn.execute(text(f'DELETE FROM "{name}" WHERE "{pk_col}" = :v'), {"v": pk_val})

def primary_key_of(table_name: str) -> Optional[str]:
    insp = inspect(engine)
    pks = insp.get_pk_constraint(table_name).get("constrained_columns") or []
    return pks[0] if pks else None

# -------------------------
# Websocket для «живых» обновлений
# -------------------------
clients: Dict[str, set[WebSocket]] = {}  # table -> websockets

async def notify(table: str, kind: str):
    if table not in clients:
        return
    dead = set()
    for ws in clients[table]:
        try:
            await ws.send_text(json.dumps({"type": kind, "table": table, "ts": time.time()}))
        except Exception:
            dead.add(ws)
    for d in dead:
        clients[table].discard(d)

@app.websocket("/ws/{table}")
async def ws_table(ws: WebSocket, table: str):
    await ws.accept()
    clients.setdefault(table, set()).add(ws)
    try:
        while True:
            await ws.receive_text()  # ping/pong
    except WebSocketDisconnect:
        clients[table].discard(ws)

# -------------------------
# Маршруты UI
# -------------------------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    require_auth(request)
    tables = list_tables()
    states = {
        "bot": proc_state("bot"),
        "admin": proc_state("admin"),
        "site": proc_state("site"),
    }
    return templates.TemplateResponse("index.html", {"request": request, "tables": tables, "states": states})

@app.post("/process/{name}/{action}")
async def process_action(request: Request, name: str, action: str, csrf: str = Form(...)):
    require_auth(request)
    if not secrets.compare_digest(request.session.get("csrf") or "", csrf):
        raise HTTPException(400, "Bad CSRF")

    if name not in ("bot", "admin", "site", "launcher"):
        raise HTTPException(404)

    if action == "start":
        if name == "bot":
            started = start_proc("bot", ["python", BOT_PY])
        elif name == "admin":
            started = start_proc("admin", ["python", ADMIN_PY])
        elif name == "site":
            started = start_proc("site", ["python", SITE_PY])
        else:
            started = start_proc("launcher", ["python", LAUNCHER_PY])
        return PlainTextResponse("started" if started else "already running")
    elif action == "stop":
        stop_proc(name if name != "launcher" else "launcher")
        return PlainTextResponse("stopped")
    else:
        raise HTTPException(400)

@app.get("/db", response_class=HTMLResponse)
async def db_list(request: Request):
    require_auth(request)
    tables = list_tables()
    return templates.TemplateResponse("db.html", {"request": request, "tables": tables})

@app.get("/db/{table}", response_class=HTMLResponse)
async def db_table(request: Request, table: str, page: int = 1, size: int = 50):
    require_auth(request)
    size = max(1, min(size, 200))
    offset = (page - 1) * size
    cols, rows, total = table_rows(table, size, offset)
    pk = primary_key_of(table)
    # новый CSRF на каждую страницу
    csrf = signer.dumps({"at": time.time(), "rnd": secrets.token_hex(8)})
    request.session["csrf"] = csrf
    return templates.TemplateResponse("table.html", {
        "request": request, "table": table, "cols": cols, "rows": rows,
        "total": total, "page": page, "size": size, "pk": pk, "csrf": csrf
    })

@app.post("/db/{table}/save")
async def db_save(request: Request, table: str, payload: str = Form(...), csrf: str = Form(...)):
    require_auth(request)
    if not secrets.compare_digest(request.session.get("csrf") or "", csrf):
        raise HTTPException(400, "Bad CSRF")
    data = json.loads(payload)
    pk = primary_key_of(table)
    try:
        upsert_row(table, pk, data)
    except SQLAlchemyError as e:
        raise HTTPException(400, f"SQL error: {e}") from e
    await notify(table, "changed")
    return PlainTextResponse("ok")

@app.post("/db/{table}/delete")
async def db_delete(request: Request, table: str, pk: str = Form(...), val: str = Form(...), csrf: str = Form(...)):
    require_auth(request)
    if not secrets.compare_digest(request.session.get("csrf") or "", csrf):
        raise HTTPException(400, "Bad CSRF")
    if not pk:
        raise HTTPException(400, "No PK")
    delete_row(table, pk, val)
    await notify(table, "deleted")
    return PlainTextResponse("ok")

# -------------------------
# main
# -------------------------
if __name__ == "__main__":
    import uvicorn
    print(f"Owner portal on http://127.0.0.1:{PORT}")
    uvicorn.run("owner_portal:app", host="127.0.0.1", port=PORT, reload=False)
