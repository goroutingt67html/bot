import os
import secrets
import aiosqlite
import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL", "").rstrip("/")
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

# Путь к базе данных (в папке /data, если подключен Volume)
DB_DIR = os.getenv("DB_DIR", ".")
DB_PATH = os.path.join(DB_DIR, "keys.db")

app = FastAPI(title="API Gateway")
security = HTTPBasic()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                key TEXT PRIMARY KEY,
                name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

@app.on_event("startup")
async def startup():
    await init_db()

def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if credentials.password != ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный пароль",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

@app.get("/", response_class=HTMLResponse)
async def admin_panel(request: Request, user: str = Depends(verify_admin)):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key, name, created_at FROM api_keys ORDER BY created_at DESC") as cursor:
            rows = await cursor.fetchall()

    host_url = str(request.base_url).rstrip("/")

    table_rows = "".join([
        f"<tr><td style='padding:8px;border:1px solid #ddd;'>{name}</td>"
        f"<td style='padding:8px;border:1px solid #ddd;'><code>{key}</code></td>"
        f"<td style='padding:8px;border:1px solid #ddd;'>{created}</td>"
        f"<td style='padding:8px;border:1px solid #ddd;'>"
        f"<form method='POST' action='/keys/delete' style='display:inline;'>"
        f"<input type='hidden' name='key' value='{key}'>"
        f"<button type='submit' style='background:#ff4d4f;color:white;border:none;padding:4px 8px;border-radius:4px;cursor:pointer;'>Удалить</button>"
        f"</form></td></tr>"
        for key, name, created in rows
    ])

    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>API Key Gateway</title>
        <meta charset="utf-8">
        <style>
            body {{ font-family: sans-serif; max-width: 900px; margin: 40px auto; padding: 0 20px; }}
            input[type=text] {{ padding: 8px; border: 1px solid #ccc; border-radius: 4px; width: 260px; }}
            button {{ padding: 8px 16px; background: #0070f3; color: white; border: none; border-radius: 4px; cursor: pointer; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            .card {{ background: #f7f7f7; padding: 20px; border-radius: 8px; margin-bottom: 20px; }}
        </style>
    </head>
    <body>
        <h2>Панель управления API-ключами</h2>
        
        <div class="card">
            <h3>Создать новый ключ</h3>
            <form method="POST" action="/keys/create">
                <input type="text" name="name" placeholder="Название (например: NextChat)" required>
                <button type="submit">Сгенерировать</button>
            </form>
        </div>

        <div class="card">
            <h3>Параметры для подключения клиентов</h3>
            <p><strong>Base URL:</strong> <code>{host_url}/v1</code></p>
        </div>

        <h3>Список ключей</h3>
        <table>
            <thead>
                <tr style="background:#eaeaea;">
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Название</th>
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Ключ</th>
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Создан</th>
                    <th style="padding:8px;border:1px solid #ddd;text-align:left;">Действие</th>
                </tr>
            </thead>
            <tbody>
                {table_rows or "<tr><td colspan='4' style='text-align:center;padding:12px;'>Ключей нет</td></tr>"}
            </tbody>
        </table>
    </body>
    </html>
    """

@app.post("/keys/create")
async def create_key(request: Request, user: str = Depends(verify_admin)):
    form = await request.form()
    name = form.get("name", "Unnamed")
    new_key = f"sk-custom-{secrets.token_urlsafe(24)}"
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO api_keys (key, name) VALUES (?, ?)", (new_key, name))
        await db.commit()
    
    return HTMLResponse("<script>window.location.href='/';</script>")

@app.post("/keys/delete")
async def delete_key(request: Request, user: str = Depends(verify_admin)):
    form = await request.form()
    key_to_delete = form.get("key")
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM api_keys WHERE key = ?", (key_to_delete,))
        await db.commit()
        
    return HTMLResponse("<script>window.location.href='/';</script>")

@app.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"])
async def proxy(path: str, request: Request):
    if request.method == "OPTIONS":
        return HTMLResponse(status_code=200)

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Отсутствует заголовок Authorization")

    client_key = auth_header.replace("Bearer ", "").strip()
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key FROM api_keys WHERE key = ?", (client_key,)) as cursor:
            if not await cursor.fetchone():
                raise HTTPException(status_code=403, detail="Неверный API-ключ")

    target_url = f"{UPSTREAM_BASE_URL}/{path}"
    
    headers = dict(request.headers)
    headers["authorization"] = f"Bearer {UPSTREAM_API_KEY}"
    headers.pop("host", None)
    headers.pop("content-length", None)

    body = await request.body()
    client = httpx.AsyncClient(timeout=120.0)

    req = client.build_request(
        method=request.method,
        url=target_url,
        headers=headers,
        content=body,
        params=request.query_params
    )

    response = await client.send(req, stream=True)

    return StreamingResponse(
        response.aiter_raw(),
        status_code=response.status_code,
        headers=dict(response.headers),
        background=httpx._transports.default.ResponseClosed(response.aclose)
    )
