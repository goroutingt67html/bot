import os
import io
import re
import math
import time
import json
import base64
import zipfile
import asyncio
import secrets
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timedelta, date
import aiohttp
import asyncpg
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from pypdf import PdfReader
import docx
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatMemberStatus, ContentType
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter, TelegramNetworkError
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    CallbackQuery,
    BufferedInputFile,
    LabeledPrice,
    PreCheckoutQuery
)
from openai import AsyncOpenAI

load_dotenv()

# ─── Конфигурация токенов и базы ───

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip().strip('"').strip("'")
FLASH_API_KEY = os.getenv("FLASH_API_KEY", "")
PRO_API_KEY = os.getenv("PRO_API_KEY", "")

# CryptoBot (@CryptoBot -> Crypto Pay -> Create App)
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN", "")
CRYPTO_PAY_API = os.getenv("CRYPTO_PAY_API", "https://pay.crypt.bot/api")
CRYPTO_ASSET = os.getenv("CRYPTO_ASSET", "USDT")

# Telegram Stars: сколько звёзд стоит 1 доллар (курс Telegram ≈ 0.02$ за звезду)
STARS_PER_USD = int(os.getenv("STARS_PER_USD", "50"))

RAW_DB_URL = os.getenv("DATABASE_URL", "")
DATABASE_URL = RAW_DB_URL.replace("postgres://", "postgresql://") if RAW_DB_URL else ""

ADMIN_ID = 5480751648

CHANNEL_USERNAME = "@Quantum_Evo"
CHANNEL_URL = "https://t.me/Quantum_Evo"

FLASH_BASE_URL = "https://gorouter.app/v1"
MODEL_FLASH = "claude-opus-4-8"

PRO_BASE_URL = "https://gorouter.app/v1"
MODEL_PRO = "claude-opus-5-thinking"

# ─── Тарифная сетка (валюта — доллар США) ───

PRICE_PER_REQUEST = {
    "flash": Decimal("0.050"),
    "pro": Decimal("0.120"),
}

MIN_REQUESTS_PACK = 8
MAX_REQUESTS_PACK = 500
PACK_PRESETS = [8, 25, 50, 100, 250, 500]

PLANS = {
    "free": {
        "title": "Free",
        "emoji": "🆓",
        "price_month": Decimal("0"),
        "price_week": Decimal("0"),
        "flash_day": 15,
        "pro_day": 0,
        "history": 6,
        "rate_limit": 20,
        "system_prompt": False,
        "perks": [
            "15 запросов Flash в сутки",
            "Pro-модель — только за купленные запросы",
            "Контекст 6 сообщений",
        ],
    },
    "plus": {
        "title": "Plus",
        "emoji": "✨",
        "price_month": Decimal("9.99"),
        "price_week": Decimal("3.99"),
        "flash_day": 150,
        "pro_day": 15,
        "history": 12,
        "rate_limit": 10,
        "system_prompt": False,
        "perks": [
            "150 запросов Flash в сутки",
            "15 запросов Pro в сутки",
            "Контекст 12 сообщений",
            "Антифлуд 10 сек.",
        ],
    },
    "pro": {
        "title": "Pro",
        "emoji": "🚀",
        "price_month": Decimal("24.99"),
        "price_week": Decimal("8.99"),
        "flash_day": 600,
        "pro_day": 80,
        "history": 20,
        "rate_limit": 5,
        "system_prompt": True,
        "perks": [
            "600 запросов Flash в сутки",
            "80 запросов Pro в сутки",
            "Системные промты (текст и файл)",
            "Контекст 20 сообщений",
            "Антифлуд 5 сек.",
        ],
    },
    "max5": {
        "title": "Max 5x",
        "emoji": "🔥",
        "price_month": Decimal("49.99"),
        "price_week": Decimal("17.99"),
        "flash_day": 1500,
        "pro_day": 250,
        "history": 30,
        "rate_limit": 3,
        "system_prompt": True,
        "perks": [
            "1500 запросов Flash в сутки",
            "250 запросов Pro в сутки",
            "Системные промты (текст и файл)",
            "Контекст 30 сообщений",
            "Антифлуд 3 сек.",
        ],
    },
    "max20": {
        "title": "Max 20x",
        "emoji": "👑",
        "price_month": Decimal("99.99"),
        "price_week": Decimal("34.99"),
        "flash_day": 4000,
        "pro_day": 700,
        "history": 40,
        "rate_limit": 2,
        "system_prompt": True,
        "perks": [
            "4000 запросов Flash в сутки",
            "700 запросов Pro в сутки",
            "Системные промты (текст и файл)",
            "Контекст 40 сообщений",
            "Антифлуд 2 сек.",
        ],
    },
}

PLAN_ORDER = ["free", "plus", "pro", "max5", "max20"]
PERIODS = {"week": ("7 дней", 7, "price_week"), "month": ("30 дней", 30, "price_month")}

# ─── Инициализация ───

session = AiohttpSession(timeout=60.0)
bot = Bot(token=TG_BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())

client_flash = AsyncOpenAI(api_key=FLASH_API_KEY, base_url=FLASH_BASE_URL)
client_pro = AsyncOpenAI(api_key=PRO_API_KEY, base_url=PRO_BASE_URL)

db_pool: asyncpg.Pool = None
user_last_request_time: dict[int, float] = {}

# ─── Утилиты денег ───

def usd(value) -> Decimal:
    """Приводит любое число к Decimal с 3 знаками (шаг цены 0.001$)."""
    if value is None:
        value = 0
    return Decimal(str(value)).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)

def fmt_usd(value) -> str:
    return f"${usd(value):.2f}" if usd(value) == usd(value).quantize(Decimal("0.01")) else f"${usd(value):.3f}"

def usd_to_stars(value) -> int:
    stars = math.ceil(float(usd(value)) * STARS_PER_USD)
    return max(1, stars)

def plan_rank(plan: str) -> int:
    return PLAN_ORDER.index(plan) if plan in PLAN_ORDER else 0

def plan_allows_system_prompt(plan: str) -> bool:
    return bool(PLANS.get(plan, PLANS["free"])["system_prompt"])

def pack_price(model: str, count: int) -> Decimal:
    return usd(PRICE_PER_REQUEST[model] * count)

# ─── Словари расширений файлов ───

EXT_MAP = {
    'python': 'py', 'py': 'py', 'питон': 'py', 'пайтон': 'py',
    'javascript': 'js', 'js': 'js', 'node': 'js', 'джаваскрипт': 'js',
    'typescript': 'ts', 'ts': 'ts', 'тайпскрипт': 'ts',
    'html': 'html', 'htm': 'html', 'хтмл': 'html',
    'css': 'css', 'цсс': 'css',
    'json': 'json', 'джсон': 'json', 'джейсон': 'json',
    'csv': 'csv', 'ксв': 'csv',
    'xml': 'xml',
    'markdown': 'md', 'md': 'md', 'маркдаун': 'md',
    'txt': 'txt', 'text': 'txt', 'текст': 'txt', 'тхт': 'txt',
    'c': 'c', 'си': 'c',
    'cpp': 'cpp', 'c++': 'cpp', 'cxx': 'cpp', 'плюсы': 'cpp',
    'csharp': 'cs', 'cs': 'cs', 'c#': 'cs', 'сишарп': 'cs',
    'java': 'java', 'джава': 'java', 'ява': 'java',
    'kotlin': 'kt', 'kt': 'kt', 'котлин': 'kt',
    'swift': 'swift', 'свифт': 'swift',
    'go': 'go', 'golang': 'go', 'го': 'go', 'голанг': 'go',
    'rust': 'rs', 'rs': 'rs', 'раст': 'rs',
    'php': 'php', 'пхп': 'php',
    'ruby': 'rb', 'rb': 'rb', 'руби': 'rb',
    'sql': 'sql', 'скюэль': 'sql', 'скуль': 'sql',
    'bash': 'sh', 'sh': 'sh', 'shell': 'sh', 'zsh': 'sh', 'баш': 'sh', 'шелл': 'sh',
    'powershell': 'ps1', 'ps1': 'ps1',
    'yaml': 'yaml', 'yml': 'yml', 'ямл': 'yaml',
    'toml': 'toml',
    'ini': 'ini', 'cfg': 'cfg', 'conf': 'conf', 'env': 'env',
    'dockerfile': 'dockerfile',
    'bat': 'bat', 'cmd': 'cmd'
}

CODE_EXTENSIONS = {
    'py', 'js', 'ts', 'html', 'css', 'json', 'xml', 'cpp', 'c', 'cs',
    'java', 'kt', 'swift', 'go', 'rs', 'php', 'rb', 'sql', 'sh', 'ps1',
    'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf', 'env', 'bat', 'cmd', 'dockerfile'
}

DEFAULT_FILENAMES = {
    'py': 'main.py', 'js': 'index.js', 'ts': 'index.ts', 'html': 'index.html',
    'css': 'style.css', 'json': 'data.json', 'csv': 'data.csv', 'sql': 'query.sql',
    'sh': 'script.sh', 'md': 'README.md', 'txt': 'document.txt', 'cpp': 'main.cpp',
    'c': 'main.c', 'cs': 'Program.cs', 'java': 'Main.java', 'go': 'main.go',
    'rs': 'main.rs', 'php': 'index.php', 'yaml': 'config.yaml', 'yml': 'config.yml'
}

SYSTEM_PROMPT_LIMIT = 8000

# ─── Инициализация и операции Базы Данных ───

async def init_db():
    global db_pool
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не задана в переменных окружения!")

    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=15)

    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            balance NUMERIC DEFAULT 0,
            is_banned BOOLEAN DEFAULT FALSE,
            is_blocked BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        # ── Миграции под тарифы, кредиты запросов и системные промты ──
        await conn.execute("""
        ALTER TABLE users
            ADD COLUMN IF NOT EXISTS balance NUMERIC DEFAULT 0,
            ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free',
            ADD COLUMN IF NOT EXISTS plan_expires TIMESTAMP,
            ADD COLUMN IF NOT EXISTS flash_credits INT DEFAULT 0,
            ADD COLUMN IF NOT EXISTS pro_credits INT DEFAULT 0,
            ADD COLUMN IF NOT EXISTS used_flash INT DEFAULT 0,
            ADD COLUMN IF NOT EXISTS used_pro INT DEFAULT 0,
            ADD COLUMN IF NOT EXISTS usage_date DATE DEFAULT CURRENT_DATE,
            ADD COLUMN IF NOT EXISTS active_model TEXT DEFAULT 'flash',
            ADD COLUMN IF NOT EXISTS system_prompt TEXT,
            ADD COLUMN IF NOT EXISTS total_spent NUMERIC DEFAULT 0;
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            active_chat_id INTEGER
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_logs (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            content_type TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            reward NUMERIC DEFAULT 0,
            max_activations INT DEFAULT 1,
            used_count INT DEFAULT 0,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await conn.execute("""
        ALTER TABLE promocodes
            ADD COLUMN IF NOT EXISTS grant_plan TEXT,
            ADD COLUMN IF NOT EXISTS grant_days INT DEFAULT 0,
            ADD COLUMN IF NOT EXISTS grant_flash INT DEFAULT 0,
            ADD COLUMN IF NOT EXISTS grant_pro INT DEFAULT 0;
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS promocode_activations (
            id SERIAL PRIMARY KEY,
            code TEXT REFERENCES promocodes(code) ON DELETE CASCADE,
            user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, user_id)
        );
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            provider TEXT,
            external_id TEXT,
            amount_usd NUMERIC DEFAULT 0,
            stars INT DEFAULT 0,
            purpose TEXT,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            paid_at TIMESTAMP
        );
        """)
        await conn.execute("""
        ALTER TABLE payments
            ADD COLUMN IF NOT EXISTS user_id BIGINT,
            ADD COLUMN IF NOT EXISTS provider TEXT,
            ADD COLUMN IF NOT EXISTS external_id TEXT,
            ADD COLUMN IF NOT EXISTS amount_usd NUMERIC DEFAULT 0,
            ADD COLUMN IF NOT EXISTS stars INT DEFAULT 0,
            ADD COLUMN IF NOT EXISTS purpose TEXT,
            ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending',
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ADD COLUMN IF NOT EXISTS paid_at TIMESTAMP;
        """)
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            kind TEXT,
            details TEXT,
            amount_usd NUMERIC DEFAULT 0,
            source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """)
        await conn.execute("""
        ALTER TABLE purchases
            ADD COLUMN IF NOT EXISTS user_id BIGINT,
            ADD COLUMN IF NOT EXISTS kind TEXT,
            ADD COLUMN IF NOT EXISTS details TEXT,
            ADD COLUMN IF NOT EXISTS amount_usd NUMERIC DEFAULT 0,
            ADD COLUMN IF NOT EXISTS source TEXT,
            ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP;
        """)

async def track_user(user: types.User):
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO users (user_id, username, first_name, last_activity)
            VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
            ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                is_blocked = FALSE,
                last_activity = CURRENT_TIMESTAMP;
        """, user.id, user.username or "", user.first_name or "")

async def is_user_banned(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        val = await conn.fetchval("SELECT is_banned FROM users WHERE user_id = $1", user_id)
        return bool(val)

async def log_activity(user_id: int, content_type: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO activity_logs (user_id, content_type) VALUES ($1, $2)", user_id, content_type)

# ─── Тарифы: состояние пользователя, лимиты, списание ───

async def get_user_state(user_id: int) -> dict:
    """Возвращает актуальный тариф, лимиты и балансы. Сбрасывает суточный счётчик и истёкшую подписку."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT user_id, balance, plan, plan_expires, flash_credits, pro_credits,
                   used_flash, used_pro, usage_date, active_model, system_prompt, created_at
            FROM users WHERE user_id = $1
        """, user_id)
        if not row:
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", user_id)
            row = await conn.fetchrow("""
                SELECT user_id, balance, plan, plan_expires, flash_credits, pro_credits,
                       used_flash, used_pro, usage_date, active_model, system_prompt, created_at
                FROM users WHERE user_id = $1
            """, user_id)

        plan = row["plan"] or "free"
        expires = row["plan_expires"]
        if plan != "free" and expires and expires < datetime.now():
            await conn.execute(
                "UPDATE users SET plan = 'free', plan_expires = NULL WHERE user_id = $1", user_id
            )
            plan = "free"
            expires = None

        used_flash = row["used_flash"] or 0
        used_pro = row["used_pro"] or 0
        if row["usage_date"] != date.today():
            await conn.execute(
                "UPDATE users SET used_flash = 0, used_pro = 0, usage_date = CURRENT_DATE WHERE user_id = $1",
                user_id
            )
            used_flash, used_pro = 0, 0

    cfg = PLANS.get(plan, PLANS["free"])
    return {
        "user_id": user_id,
        "balance": usd(row["balance"]),
        "plan": plan,
        "plan_expires": expires,
        "cfg": cfg,
        "flash_credits": row["flash_credits"] or 0,
        "pro_credits": row["pro_credits"] or 0,
        "used_flash": used_flash,
        "used_pro": used_pro,
        "left_flash": max(0, cfg["flash_day"] - used_flash),
        "left_pro": max(0, cfg["pro_day"] - used_pro),
        "active_model": row["active_model"] or "flash",
        "system_prompt": row["system_prompt"] or "",
        "created_at": row["created_at"],
    }

async def consume_request(user_id: int, model: str) -> tuple[bool, str]:
    """Списывает один запрос атомарно: сначала суточная квота тарифа, затем купленные запросы."""
    if user_id == ADMIN_ID:
        return True, "admin"

    st = await get_user_state(user_id)
    cfg_quota = st["cfg"]["flash_day"] if model == "flash" else st["cfg"]["pro_day"]
    used_col = "used_flash" if model == "flash" else "used_pro"
    credits_col = "flash_credits" if model == "flash" else "pro_credits"

    async with db_pool.acquire() as conn:
        res_plan = await conn.fetchval(f"""
            UPDATE users 
            SET {used_col} = COALESCE({used_col}, 0) + 1 
            WHERE user_id = $1 AND COALESCE({used_col}, 0) < $2
            RETURNING {used_col}
        """, user_id, cfg_quota)
        if res_plan is not None:
            return True, "plan"

        res_credits = await conn.fetchval(f"""
            UPDATE users 
            SET {credits_col} = GREATEST(COALESCE({credits_col}, 0) - 1, 0)
            WHERE user_id = $1 AND COALESCE({credits_col}, 0) > 0
            RETURNING {credits_col}
        """, user_id)
        if res_credits is not None:
            return True, "credits"

    return False, "empty"

async def refund_request(user_id: int, model: str, source: str):
    """Возврат запроса при ошибке модели."""
    if source not in ("plan", "credits"):
        return
    async with db_pool.acquire() as conn:
        if source == "plan":
            col = "used_flash" if model == "flash" else "used_pro"
            await conn.execute(f"UPDATE users SET {col} = GREATEST(COALESCE({col}, 0) - 1, 0) WHERE user_id = $1", user_id)
        else:
            col = "flash_credits" if model == "flash" else "pro_credits"
            await conn.execute(f"UPDATE users SET {col} = COALESCE({col}, 0) + 1 WHERE user_id = $1", user_id)

async def add_balance(user_id: int, amount) -> Decimal:
    async with db_pool.acquire() as conn:
        new_val = await conn.fetchval(
            "UPDATE users SET balance = COALESCE(balance, 0) + $1 WHERE user_id = $2 RETURNING balance",
            usd(amount), user_id
        )
    return usd(new_val)

async def try_charge_balance(user_id: int, amount) -> bool:
    """Атомарное списание с баланса, если хватает средств."""
    amount = usd(amount)
    async with db_pool.acquire() as conn:
        res = await conn.fetchval("""
            UPDATE users SET balance = COALESCE(balance, 0) - $1, total_spent = COALESCE(total_spent, 0) + $1
            WHERE user_id = $2 AND COALESCE(balance, 0) >= $1
            RETURNING balance
        """, amount, user_id)
    return res is not None

async def grant_plan(user_id: int, plan: str, days: int):
    """Продлевает или повышает тариф. При апгрейде остаток дней не теряется."""
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT plan, plan_expires FROM users WHERE user_id = $1", user_id)
        cur_plan = (row["plan"] if row else "free") or "free"
        cur_exp = row["plan_expires"] if row else None
        now = datetime.now()
        base = cur_exp if (cur_exp and cur_exp > now and plan_rank(cur_plan) == plan_rank(plan)) else now
        new_exp = base + timedelta(days=days)
        await conn.execute(
            "UPDATE users SET plan = $1, plan_expires = $2 WHERE user_id = $3",
            plan, new_exp, user_id
        )
    return new_exp

async def grant_requests(user_id: int, model: str, count: int):
    col = "flash_credits" if model == "flash" else "pro_credits"
    async with db_pool.acquire() as conn:
        await conn.execute(f"UPDATE users SET {col} = COALESCE({col}, 0) + $1 WHERE user_id = $2", count, user_id)

async def log_purchase(user_id: int, kind: str, details: str, amount, source: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO purchases (user_id, kind, details, amount_usd, source) VALUES ($1, $2, $3, $4, $5)",
            user_id, kind, details, usd(amount), source
        )

async def set_system_prompt(user_id: int, text: str | None):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET system_prompt = $1 WHERE user_id = $2", text, user_id)

async def set_active_model(user_id: int, model: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET active_model = $1 WHERE user_id = $2", model, user_id)

# ─── Чаты и сообщения ───

async def get_or_create_active_chat(user_id: int) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT active_chat_id FROM active_sessions WHERE user_id = $1", user_id)
        if row and row["active_chat_id"]:
            return row["active_chat_id"]

        chat_id = await conn.fetchval(
            "INSERT INTO chats (user_id, title) VALUES ($1, $2) RETURNING id",
            user_id, "Основной диалог"
        )
        await conn.execute(
            "INSERT INTO active_sessions (user_id, active_chat_id) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET active_chat_id = $2",
            user_id, chat_id
        )
        return chat_id

async def set_active_chat(user_id: int, chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO active_sessions (user_id, active_chat_id) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET active_chat_id = $2",
            user_id, chat_id
        )

async def get_user_chats(user_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, title FROM chats WHERE user_id = $1 ORDER BY id DESC", user_id)
        return [(r["id"], r["title"]) for r in rows]

async def get_chat_title(chat_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT title FROM chats WHERE id = $1", chat_id)
        return row["title"] if row else "Без названия"

async def delete_chat_db(chat_id: int, user_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM chats WHERE id = $1", chat_id)
        row = await conn.fetchrow("SELECT active_chat_id FROM active_sessions WHERE user_id = $1", user_id)
        if row and row["active_chat_id"] == chat_id:
            await conn.execute("DELETE FROM active_sessions WHERE user_id = $1", user_id)

async def rename_chat_db(chat_id: int, new_title: str):
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE chats SET title = $1 WHERE id = $2", new_title, chat_id)

async def save_message(chat_id: int, role: str, content: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO messages (chat_id, role, content) VALUES ($1, $2, $3)", chat_id, role, content)

async def get_chat_messages(chat_id: int, limit: int = 10):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content, created_at FROM messages WHERE chat_id = $1 ORDER BY id DESC LIMIT $2",
            chat_id, limit
        )
        return [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in reversed(rows)]

async def get_all_chat_messages(chat_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT role, content, created_at FROM messages WHERE chat_id = $1 ORDER BY id ASC",
            chat_id
        )
        return [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]

# ─── FSM Состояния ───

class ChatStates(StatesGroup):
    waiting_for_chat_rename = State()

class UserStates(StatesGroup):
    waiting_for_promo_code = State()
    waiting_for_custom_pack = State()
    waiting_for_topup_amount = State()
    waiting_for_system_prompt = State()

class AdminStates(StatesGroup):
    waiting_for_user_query = State()
    waiting_for_promo_reward = State()
    waiting_for_promo_activations = State()
    waiting_for_promo_duration = State()
    waiting_for_broadcast_target = State()
    waiting_for_broadcast_content = State()
    waiting_for_broadcast_buttons = State()
    waiting_for_grant_balance = State()

# ─── CryptoBot (Crypto Pay API) ───

class CryptoPayError(Exception):
    pass

async def crypto_request(method: str, payload: dict | None = None) -> dict:
    if not CRYPTO_PAY_TOKEN:
        raise CryptoPayError("CRYPTO_PAY_TOKEN не задан в переменных окружения")
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    url = f"{CRYPTO_PAY_API}/{method}"
    timeout = aiohttp.ClientTimeout(total=25)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.post(url, json=payload or {}) as resp:
            data = await resp.json(content_type=None)
    if not data or not data.get("ok"):
        raise CryptoPayError(str(data.get("error") if data else "нет ответа от Crypto Pay"))
    return data["result"]

async def crypto_create_invoice(user_id: int, amount, purpose: str, description: str) -> tuple[str, str]:
    """Создаёт чек CryptoBot. Возвращает (invoice_id, pay_url)."""
    amount = usd(amount)
    result = await crypto_request("createInvoice", {
        "asset": CRYPTO_ASSET,
        "amount": f"{amount:.2f}",
        "description": description[:1024],
        "payload": json.dumps({"user_id": user_id, "purpose": purpose}),
        "allow_comments": False,
        "allow_anonymous": False,
        "expires_in": 3600,
    })
    invoice_id = str(result.get("invoice_id"))
    pay_url = result.get("mini_app_invoice_url") or result.get("bot_invoice_url") or result.get("pay_url")
    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments (user_id, provider, external_id, amount_usd, purpose, status)
            VALUES ($1, 'cryptobot', $2, $3, $4, 'pending')
        """, user_id, invoice_id, amount, purpose)
    return invoice_id, pay_url

async def crypto_check_invoice(invoice_id: str) -> str:
    result = await crypto_request("getInvoices", {"invoice_ids": invoice_id})
    items = result.get("items") or []
    if not items:
        return "missing"
    return items[0].get("status", "unknown")

# ─── Единая выдача покупки ───

def parse_purpose(purpose: str) -> dict:
    """
    Форматы purpose:
      topup:<amount>
      plan:<plan_key>:<period>
      pack:<model>:<count>
    """
    parts = purpose.split(":")
    kind = parts[0]
    if kind == "topup":
        try:
            amt = usd(parts[1])
        except Exception:
            amt = Decimal("0")
        return {"kind": "topup", "amount": amt}
    if kind == "plan":
        if len(parts) >= 3:
            return {"kind": "plan", "plan": parts[1], "period": parts[2]}
        return {"kind": "unknown"}
    if kind == "pack":
        if len(parts) >= 3:
            model = parts[1] if parts[1] in ("flash", "pro") else "flash"
            try:
                cnt = int(parts[2])
            except Exception:
                cnt = 0
            return {"kind": "pack", "model": model, "count": cnt}
        elif len(parts) == 2:
            try:
                cnt = int(parts[1])
            except Exception:
                cnt = 0
            return {"kind": "pack", "model": "flash", "count": cnt}
    return {"kind": "unknown"}

def purpose_title(purpose: str) -> str:
    p = parse_purpose(purpose)
    if p["kind"] == "topup":
        return f"Пополнение баланса на {fmt_usd(p['amount'])}"
    if p["kind"] == "plan":
        cfg = PLANS.get(p["plan"], PLANS["free"])
        period_text = PERIODS.get(p["period"], ("период", 0, "price_month"))[0]
        return f"Подписка {cfg['emoji']} {cfg['title']} на {period_text}"
    if p["kind"] == "pack":
        model = "Flash" if p.get("model") == "flash" else "Pro"
        return f"{p['count']} запросов {model}"
    return "Покупка"

def purpose_amount(purpose: str) -> Decimal:
    p = parse_purpose(purpose)
    if p["kind"] == "topup":
        return usd(p["amount"])
    if p["kind"] == "plan":
        cfg = PLANS.get(p["plan"], PLANS["free"])
        period_key = PERIODS.get(p["period"], ("30 дней", 30, "price_month"))[2]
        return usd(cfg[period_key])
    if p["kind"] == "pack":
        return pack_price(p["model"], p["count"])
    return usd(0)

async def deliver_purchase(user_id: int, purpose: str, source: str) -> str:
    """Начисляет купленное строго по типу покупки. Возвращает текст подтверждения."""
    p = parse_purpose(purpose)
    amount = purpose_amount(purpose)

    if p["kind"] == "topup":
        new_bal = await add_balance(user_id, p["amount"])
        await log_purchase(user_id, "topup", f"{p['amount']}", amount, source)
        return (
            f"✅ Баланс пополнен на *{fmt_usd(p['amount'])}*.\n"
            f"💰 Текущий баланс: *{fmt_usd(new_bal)}*"
        )

    if p["kind"] == "plan":
        cfg = PLANS[p["plan"]]
        days = PERIODS[p["period"]][1]
        exp = await grant_plan(user_id, p["plan"], days)
        await log_purchase(user_id, "plan", f"{p['plan']}:{p['period']}", amount, source)
        return (
            f"✅ Подписка *{cfg['emoji']} {cfg['title']}* активирована!\n"
            f"⏳ Действует до: *{exp.strftime('%Y-%m-%d %H:%M')}*\n"
            f"⚡ Flash: `{cfg['flash_day']}`/сутки · 🧠 Pro: `{cfg['pro_day']}`/сутки"
        )

    if p["kind"] == "pack":
        await grant_requests(user_id, p["model"], p["count"])
        await log_purchase(user_id, "pack", f"{p['model']}:{p['count']}", amount, source)
        model_name = "Flash" if p["model"] == "flash" else "Pro"
        return (
            f"✅ Начислено *{p['count']}* запросов к модели *{model_name}*.\n"
            f"Они не сгорают в конце суток и тратятся после суточной квоты тарифа."
        )

    return "⚠️ Неизвестный тип покупки."

# ─── Клавиатуры ───

def get_sub_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Перейти в канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Подтвердить подписку", callback_data="verify_subscription")]
        ]
    )

def get_main_reply_keyboard(is_admin: bool = False):
    keyboard = [
        [KeyboardButton(text="➕ Новый чат"), KeyboardButton(text="🖨️ История чатов")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🛒 Магазин")],
        [KeyboardButton(text="🧩 Системный промт"), KeyboardButton(text="🎁 Промокод")]
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text="⚡ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_chat_actions_keyboard(chat_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить диалог", callback_data=f"chat_use:{chat_id}")],
            [InlineKeyboardButton(text="📥 Экспорт .md", callback_data=f"chat_exp_md:{chat_id}"),
             InlineKeyboardButton(text="📥 Экспорт .txt", callback_data=f"chat_exp_txt:{chat_id}")],
            [InlineKeyboardButton(text="✏️ Переименовать", callback_data=f"chat_rename:{chat_id}"),
             InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"chat_delete:{chat_id}")],
            [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="chat_list_back")]
        ]
    )

def get_admin_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Метрики аудитории", callback_data="admin_metrics_audience"),
             InlineKeyboardButton(text="📈 Метрики активности", callback_data="admin_metrics_activity")],
            [InlineKeyboardButton(text="💵 Метрики продаж", callback_data="admin_metrics_sales")],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast_start"),
             InlineKeyboardButton(text="🔍 Поиск пользователя", callback_data="admin_user_search")],
            [InlineKeyboardButton(text="🎁 Создать промокод", callback_data="admin_create_promo")],
            [InlineKeyboardButton(text="❌ Закрыть панель", callback_data="admin_close")]
        ]
    )

def get_profile_keyboard(st: dict):
    rows = [
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="topup_menu")],
        [InlineKeyboardButton(text="🛒 Купить подписку", callback_data="shop_plans"),
         InlineKeyboardButton(text="⚡ Купить запросы", callback_data="shop_packs")],
        [InlineKeyboardButton(text="🎁 Активировать промокод", callback_data="profile_promo")],
        [InlineKeyboardButton(
            text=f"🔀 Модель: {'⚡ Flash' if st['active_model'] == 'flash' else '🧠 Pro'}",
            callback_data="toggle_model"
        )],
    ]
    if plan_allows_system_prompt(st["plan"]):
        rows.append([InlineKeyboardButton(text="🧩 Системный промт", callback_data="sysprompt_menu")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить", callback_data="profile_refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_plans_keyboard():
    rows = []
    for key in PLAN_ORDER:
        if key == "free":
            continue
        cfg = PLANS[key]
        rows.append([InlineKeyboardButton(
            text=f"{cfg['emoji']} {cfg['title']} — {fmt_usd(cfg['price_month'])}/мес",
            callback_data=f"plan_view:{key}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="shop_root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_plan_view_keyboard(key: str):
    cfg = PLANS[key]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"📅 Неделя — {fmt_usd(cfg['price_week'])}", callback_data=f"buy:plan:{key}:week")],
        [InlineKeyboardButton(text=f"🗓 Месяц — {fmt_usd(cfg['price_month'])}", callback_data=f"buy:plan:{key}:month")],
        [InlineKeyboardButton(text="◀️ К тарифам", callback_data="shop_plans")]
    ])

def get_packs_model_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚡ Flash — {fmt_usd(PRICE_PER_REQUEST['flash'])}/запрос", callback_data="pack_model:flash")],
        [InlineKeyboardButton(text=f"🧠 Pro — {fmt_usd(PRICE_PER_REQUEST['pro'])}/запрос", callback_data="pack_model:pro")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="shop_root")]
    ])

def get_pack_counts_keyboard(model: str):
    rows, buf = [], []
    for c in PACK_PRESETS:
        buf.append(InlineKeyboardButton(
            text=f"{c} — {fmt_usd(pack_price(model, c))}",
            callback_data=f"buy:pack:{model}:{c}"
        ))
        if len(buf) == 2:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton(text="✏️ Своё количество (8–500)", callback_data=f"pack_custom:{model}")])
    rows.append([InlineKeyboardButton(text="◀️ К выбору модели", callback_data="shop_packs")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_shop_root_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Подписки", callback_data="shop_plans")],
        [InlineKeyboardButton(text="⚡ Пакеты запросов", callback_data="shop_packs")],
        [InlineKeyboardButton(text="💳 Пополнить баланс", callback_data="topup_menu")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile_refresh")]
    ])

def get_payment_methods_keyboard(purpose: str, balance_enough: bool):
    rows = []
    if balance_enough:
        rows.append([InlineKeyboardButton(text="💰 Оплатить с баланса", callback_data=f"pay:balance:{purpose}")])
    rows.append([InlineKeyboardButton(text="⭐ Telegram Stars", callback_data=f"pay:stars:{purpose}")])
    rows.append([InlineKeyboardButton(text="🪙 CryptoBot (USDT)", callback_data=f"pay:crypto:{purpose}")])
    rows.append([InlineKeyboardButton(text="◀️ В магазин", callback_data="shop_root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_topup_keyboard():
    presets = [Decimal("3"), Decimal("5"), Decimal("10"), Decimal("25"), Decimal("50"), Decimal("100")]
    rows, buf = [], []
    for p in presets:
        buf.append(InlineKeyboardButton(text=fmt_usd(p), callback_data=f"topup:{p}"))
        if len(buf) == 3:
            rows.append(buf)
            buf = []
    if buf:
        rows.append(buf)
    rows.append([InlineKeyboardButton(text="✏️ Своя сумма", callback_data="topup_custom")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="shop_root")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def get_sysprompt_keyboard(has_prompt: bool):
    rows = [[InlineKeyboardButton(text="✏️ Задать / заменить", callback_data="sysprompt_set")]]
    if has_prompt:
        rows.append([InlineKeyboardButton(text="👁 Показать текущий", callback_data="sysprompt_show"),
                     InlineKeyboardButton(text="🗑 Удалить", callback_data="sysprompt_clear")])
    rows.append([InlineKeyboardButton(text="◀️ Профиль", callback_data="profile_refresh")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ─── Текстовые блоки ───

def render_profile(st: dict, chat_count: int, req_count: int) -> str:
    cfg = st["cfg"]
    plan_line = f"{cfg['emoji']} *{cfg['title']}*"
    if st["plan"] != "free" and st["plan_expires"]:
        plan_line += f" (до {st['plan_expires'].strftime('%Y-%m-%d %H:%M')})"
    reg_date = st["created_at"].strftime("%Y-%m-%d %H:%M") if st["created_at"] else "Неизвестно"
    sp = "включены" if plan_allows_system_prompt(st["plan"]) else "недоступны (нужен Pro и выше)"
    return (
        f"👤 *Ваш профиль*\n\n"
        f"🆔 ID: `{st['user_id']}`\n"
        f"💰 Баланс: *{fmt_usd(st['balance'])}*\n"
        f"🎫 Тариф: {plan_line}\n\n"
        f"📊 *Суточные лимиты:*\n"
        f" ├ ⚡ Flash: `{st['left_flash']}` из `{cfg['flash_day']}`\n"
        f" └ 🧠 Pro: `{st['left_pro']}` из `{cfg['pro_day']}`\n\n"
        f"🎟 *Купленные запросы:*\n"
        f" ├ ⚡ Flash: `{st['flash_credits']}`\n"
        f" └ 🧠 Pro: `{st['pro_credits']}`\n\n"
        f"🔀 Активная модель: *{'⚡ Flash' if st['active_model'] == 'flash' else '🧠 Pro'}*\n"
        f"🧩 Системные промты: {sp}\n"
        f"💬 Чатов: `{chat_count}` · ⚡ Запросов всего: `{req_count}`\n"
        f"📅 Регистрация: {reg_date}"
    )

def render_shop_root() -> str:
    return (
        "🛒 *Магазин Evo Lumen*\n\n"
        "Все цены — в долларах США ($).\n\n"
        f"⚡ Flash: *{fmt_usd(PRICE_PER_REQUEST['flash'])}* за запрос\n"
        f"🧠 Pro: *{fmt_usd(PRICE_PER_REQUEST['pro'])}* за запрос\n"
        f"Пакеты запросов: от {MIN_REQUESTS_PACK} до {MAX_REQUESTS_PACK} шт.\n\n"
        "Подписки дают суточные лимиты дешевле поштучной цены, "
        "а с уровня *Pro* открываются системные промты.\n\n"
        "Оплата: ⭐ Telegram Stars, 🪙 CryptoBot или 💰 внутренний баланс."
    )

def render_plans_list() -> str:
    lines = ["🚀 *Подписки*\n"]
    for key in PLAN_ORDER:
        cfg = PLANS[key]
        if key == "free":
            lines.append(f"{cfg['emoji']} *{cfg['title']}* — бесплатно: {cfg['flash_day']} Flash/сутки")
            continue
        lines.append(
            f"{cfg['emoji']} *{cfg['title']}* — {fmt_usd(cfg['price_month'])}/мес · {fmt_usd(cfg['price_week'])}/нед\n"
            f"   ⚡ {cfg['flash_day']} Flash + 🧠 {cfg['pro_day']} Pro в сутки"
        )
    lines.append("\nВыберите тариф для подробностей:")
    return "\n".join(lines)

def render_plan_card(key: str) -> str:
    cfg = PLANS[key]
    perks = "\n".join(f" • {p}" for p in cfg["perks"])
    save = ""
    if cfg["flash_day"]:
        raw = pack_price("flash", cfg["flash_day"] * 30) + pack_price("pro", cfg["pro_day"] * 30)
        save = f"\n💡 Тот же объём запросами стоил бы ≈ *{fmt_usd(raw)}*."
    return (
        f"{cfg['emoji']} *Подписка {cfg['title']}*\n\n"
        f"{perks}\n\n"
        f"🗓 Месяц: *{fmt_usd(cfg['price_month'])}*\n"
        f"📅 Неделя: *{fmt_usd(cfg['price_week'])}*"
        f"{save}"
    )

# ─── Сброс FSM при нажатии кнопок меню ───

MENU_TEXTS = {
    "➕ Новый чат", "🖨️ История чатов", "👤 Мой профиль",
    "🛒 Магазин", "🧩 Системный промт", "🎁 Промокод", "⚡ Админ панель"
}

@dp.message(Command("cancel"))
async def cmd_cancel(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "✅ Действие отменено.",
        reply_markup=get_main_reply_keyboard(message.from_user.id == ADMIN_ID)
    )

@dp.message(~StateFilter(None), F.text.in_(MENU_TEXTS))
async def handle_menu_while_state(message: types.Message, state: FSMContext):
    """Кнопки нижнего меню всегда работают, даже если открыт диалог ввода."""
    await state.clear()
    text = message.text
    if text == "➕ Новый чат":
        await handle_new_chat(message)
    elif text == "🖨️ История чатов":
        await handle_history_menu(message)
    elif text == "👤 Мой профиль":
        await handle_user_profile(message)
    elif text == "🛒 Магазин":
        await handle_shop_btn(message)
    elif text == "🧩 Системный промт":
        await handle_sysprompt_btn(message)
    elif text == "🎁 Промокод":
        await handle_enter_promo_btn(message, state)
    elif text == "⚡ Админ панель":
        await cmd_admin(message)

# ─── Проверка подписки ───

async def check_subscription_status(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    except Exception:
        return True

# ─── Фоновый таймер статуса ───

class StatusUpdater:
    def __init__(self, message: types.Message):
        self.message = message
        self.start_time = time.time()
        self.stage = "⚡ Evo Lumen 1.0 формирует ответ..."
        self.is_running = True
        self.task = None

    async def _update_loop(self):
        while self.is_running:
            elapsed = int(time.time() - self.start_time)
            text = f"{self.stage}\n⏱ _Время размышления:_ *{elapsed} сек.*"
            try:
                await self.message.edit_text(text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
            await asyncio.sleep(1.5)

    def start(self):
        self.task = asyncio.create_task(self._update_loop())

    def set_stage(self, new_stage: str):
        self.stage = new_stage

    async def stop(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

# ─── Логика файлов и экспорта ───

def detect_file_request(user_prompt: str, ai_text: str) -> tuple[bool, str, str]:
    prompt_lower = user_prompt.lower()
    filename_match = re.search(r'\b([a-zA-Z0-9_\-]+\.([a-zA-Z0-9]+))\b', user_prompt)

    file_intent_patterns = [
        r'\b(файл|файла|файлом|файле|файлик|файликом)\b',
        r'\b(скинь|отправь|выдай|выгрузи|дай|сохрани|сделай|напиши|пришли|сгенерируй).*(файл|документ|\.[a-zA-Z0-9]+)\b',
        r'\b(файл|документ|\.[a-zA-Z0-9]+).*(скинь|отправь|выдай|выгрузи|дай|сохрани|пришли)\b',
        r'\b(в\s+(виде|формате)\s+[a-zA-Z0-9_\-\.]+)\b',
        r'\b(как\s+[a-zA-Z0-9_\-]+\.[a-zA-Z0-9]+)\b'
    ]
    has_file_intent = any(re.search(p, prompt_lower) for p in file_intent_patterns)

    if filename_match:
        full_name = filename_match.group(1)
        ext = filename_match.group(2).lower()
        if ext in EXT_MAP.values() or ext in EXT_MAP:
            return True, full_name, EXT_MAP.get(ext, ext)
        if has_file_intent:
            return True, full_name, ext

    if not has_file_intent:
        return False, "", ""

    detected_ext = None
    for key, ext_val in EXT_MAP.items():
        pattern = rf'(?:\.|\b(?:формат[еа]?|расширени[еием]|виде|язык[еа]?|как|код|файлом)\s+){re.escape(key)}\b'
        if re.search(pattern, prompt_lower):
            detected_ext = ext_val
            break
        if f".{key}" in prompt_lower:
            detected_ext = ext_val
            break

    code_blocks = re.findall(r'```([a-zA-Z0-9_+#\-\.]+)?\n([\s\S]*?)```', ai_text)
    if not detected_ext:
        if code_blocks and code_blocks[0][0]:
            raw_lang = code_blocks[0][0].strip().lower()
            detected_ext = EXT_MAP.get(raw_lang, "txt")
        elif code_blocks:
            detected_ext = "py" if "def " in ai_text or "import " in ai_text else "txt"
        else:
            detected_ext = "txt"

    filename = DEFAULT_FILENAMES.get(detected_ext, f"file.{detected_ext}")
    return True, filename, detected_ext

def extract_file_content(ai_text: str, ext: str, user_prompt: str) -> str:
    code_blocks = re.findall(r'```(?:[a-zA-Z0-9_+#\-\.]+)?\n([\s\S]*?)```', ai_text)
    if ext in CODE_EXTENSIONS:
        if code_blocks:
            return "\n\n".join(b.strip() for b in code_blocks)
        cleaned = ai_text.strip()
        cleaned = re.sub(r'^```[a-zA-Z0-9_\-\.]*\n?', '', cleaned)
        cleaned = re.sub(r'\n?```$', '', cleaned).strip()
        return cleaned

    if ext == 'md':
        return ai_text.strip()

    if ext == 'txt':
        prompt_lower = user_prompt.lower()
        if code_blocks and any(w in prompt_lower for w in ['код', 'code', 'скрипт', 'программ']):
            return "\n\n".join(b.strip() for b in code_blocks)
        return ai_text.strip()

    return "\n\n".join(b.strip() for b in code_blocks) if code_blocks else ai_text.strip()

async def send_response(message: types.Message, text: str, user_prompt: str = ""):
    wants_file, filename, ext = detect_file_request(user_prompt, text)
    if wants_file:
        file_content = extract_file_content(text, ext, user_prompt)
        if file_content:
            input_file = BufferedInputFile(file_content.encode("utf-8"), filename=filename)
            try:
                await message.answer_document(
                    document=input_file,
                    caption=f"📄 Файл `{filename}` сформирован по вашему запросу:"
                )
            except Exception as e:
                await message.answer(f"⚠️ Не удалось прикрепить файл `{filename}`: {str(e)}")

    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)] if len(text) > 4000 else [text]
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode=ParseMode.MARKDOWN)
        except TelegramBadRequest:
            await message.answer(chunk)

async def build_chat_export_file(chat_id: int, format_type: str) -> BufferedInputFile:
    title = await get_chat_title(chat_id)
    messages = await get_all_chat_messages(chat_id)

    if format_type == "md":
        content = f"# Экспорт диалога: {title}\n*ID диалога: {chat_id} | Экспортировано: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n---\n\n"
        for m in messages:
            sender = "👤 Пользователь" if m["role"] == "user" else "🤖 Evo Lumen"
            date_str = m["created_at"].strftime("%Y-%m-%d %H:%M:%S") if m["created_at"] else ""
            content += f"### {sender} ({date_str})\n\n{m['content']}\n\n---\n\n"
        filename = f"dialog_{chat_id}.md"
    else:
        content = f"=== ЭКСПОРТ ДИАЛОГА: {title} (ID: {chat_id}) ===\nДата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
        for m in messages:
            sender = "USER" if m["role"] == "user" else "EVO LUMEN"
            date_str = m["created_at"].strftime("%Y-%m-%d %H:%M:%S") if m["created_at"] else ""
            content += f"[{date_str}] {sender}:\n{m['content']}\n\n" + ("=" * 40) + "\n\n"
        filename = f"dialog_{chat_id}.txt"

    return BufferedInputFile(content.encode("utf-8"), filename=filename)

# ─── Экспорт и Меню Чатов (Callbacks) ───

@dp.callback_query(F.data.startswith("chat_exp_md:"))
async def handle_export_md(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    file = await build_chat_export_file(chat_id, "md")
    await call.message.answer_document(file, caption=f"📥 Экспорт чата #{chat_id} в формате Markdown (.md)")
    await call.answer()

@dp.callback_query(F.data.startswith("chat_exp_txt:"))
async def handle_export_txt(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    file = await build_chat_export_file(chat_id, "txt")
    await call.message.answer_document(file, caption=f"📥 Экспорт чата #{chat_id} в формате Text (.txt)")
    await call.answer()

@dp.message(F.text == "➕ Новый чат")
async def handle_new_chat(message: types.Message):
    if await is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы в системе.")
        return
    await track_user(message.from_user)
    async with db_pool.acquire() as conn:
        new_id = await conn.fetchval(
            "INSERT INTO chats (user_id, title) VALUES ($1, $2) RETURNING id",
            message.from_user.id, "Новый диалог"
        )
        await conn.execute(
            "INSERT INTO active_sessions (user_id, active_chat_id) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET active_chat_id = $2",
            message.from_user.id, new_id
        )
    await message.answer(f"🆕 Создан новый чат **№{new_id}**. Начните диалог!", parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "🖨️ История чатов")
async def handle_history_menu(message: types.Message):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    chats = await get_user_chats(message.from_user.id)
    if not chats:
        await message.answer("У вас пока нет созданных чатов. Напишите сообщение, чтобы создать диалог.")
        return

    keyboard_buttons = [
        [InlineKeyboardButton(text=f"💬 {title} (ID: {cid})", callback_data=f"chat_manage:{cid}")]
        for cid, title in chats
    ]
    await message.answer(
        "🖨️ **Ваша история диалогов:**\nВыберите чат для управления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data.startswith("chat_manage:"))
async def handle_chat_manage(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    title = await get_chat_title(chat_id)
    await call.message.edit_text(
        f"⚙️ **Управление чатом:**\n📌 *{title}* (ID: `{chat_id}`)",
        reply_markup=get_chat_actions_keyboard(chat_id),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data == "chat_list_back")
async def handle_chat_list_back(call: CallbackQuery):
    chats = await get_user_chats(call.from_user.id)
    keyboard_buttons = [
        [InlineKeyboardButton(text=f"💬 {title} (ID: {cid})", callback_data=f"chat_manage:{cid}")]
        for cid, title in chats
    ]
    await call.message.edit_text(
        "🖨️ Ваша история диалогов:\nВыберите чат для управления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data.startswith("chat_use:"))
async def handle_chat_select(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    await set_active_chat(call.from_user.id, chat_id)
    title = await get_chat_title(chat_id)
    await call.message.edit_text(
        f"✅ Активный диалог переключен на: *{title}*.\nВы можете продолжить общение!",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data.startswith("chat_delete:"))
async def handle_chat_delete(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    await delete_chat_db(chat_id, call.from_user.id)
    await call.answer("🗑️ Чат успешно удален!", show_alert=True)
    await handle_chat_list_back(call)

@dp.callback_query(F.data.startswith("chat_rename:"))
async def handle_chat_rename_prompt(call: CallbackQuery, state: FSMContext):
    chat_id = int(call.data.split(":")[1])
    await state.set_state(ChatStates.waiting_for_chat_rename)
    await state.update_data(rename_chat_id=chat_id)
    await call.message.edit_text("✏️ Отправьте новое название для этого чата следующим сообщением:")

@dp.message(StateFilter(ChatStates.waiting_for_chat_rename))
async def handle_chat_rename_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    chat_id = data.get("rename_chat_id")
    new_title = message.text.strip()[:60]
    if chat_id:
        await rename_chat_db(chat_id, new_title)
        await message.answer(f"✅ Название чата изменено на: **{new_title}**", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

# ─── Профиль ───

async def show_profile(target: types.Message | CallbackQuery, edit: bool = False):
    user = target.from_user
    st = await get_user_state(user.id)
    async with db_pool.acquire() as conn:
        chat_count = await conn.fetchval("SELECT count(*) FROM chats WHERE user_id = $1", user.id)
        req_count = await conn.fetchval("SELECT count(*) FROM activity_logs WHERE user_id = $1", user.id)

    text = render_profile(st, chat_count, req_count)
    kb = get_profile_keyboard(st)
    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except TelegramBadRequest:
            pass
    else:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "👤 Мой профиль")
async def handle_user_profile(message: types.Message):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    await show_profile(message)

@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    await handle_user_profile(message)

@dp.callback_query(F.data == "profile_refresh")
async def handle_profile_refresh(call: CallbackQuery):
    await show_profile(call, edit=True)
    await call.answer("Обновлено")

@dp.callback_query(F.data == "toggle_model")
async def handle_toggle_model(call: CallbackQuery):
    st = await get_user_state(call.from_user.id)
    new_model = "pro" if st["active_model"] == "flash" else "flash"
    await set_active_model(call.from_user.id, new_model)
    await call.answer(f"Активная модель: {'🧠 Pro' if new_model == 'pro' else '⚡ Flash'}")
    await show_profile(call, edit=True)

# ─── Магазин ───

@dp.message(F.text == "🛒 Магазин")
async def handle_shop_btn(message: types.Message):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    await message.answer(render_shop_root(), reply_markup=get_shop_root_keyboard(), parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("shop"))
async def cmd_shop(message: types.Message):
    await handle_shop_btn(message)

@dp.callback_query(F.data == "shop_root")
async def handle_shop_root(call: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await call.message.edit_text(render_shop_root(), reply_markup=get_shop_root_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except TelegramBadRequest:
        pass
    await call.answer()

@dp.callback_query(F.data == "shop_plans")
async def handle_shop_plans(call: CallbackQuery):
    try:
        await call.message.edit_text(render_plans_list(), reply_markup=get_plans_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except TelegramBadRequest:
        pass
    await call.answer()

@dp.callback_query(F.data.startswith("plan_view:"))
async def handle_plan_view(call: CallbackQuery):
    key = call.data.split(":")[1]
    if key not in PLANS:
        await call.answer("Тариф не найден", show_alert=True)
        return
    await call.message.edit_text(render_plan_card(key), reply_markup=get_plan_view_keyboard(key), parse_mode=ParseMode.MARKDOWN)
    await call.answer()

@dp.callback_query(F.data == "shop_packs")
async def handle_shop_packs(call: CallbackQuery):
    text = (
        "⚡ *Пакеты запросов*\n\n"
        f"Flash — *{fmt_usd(PRICE_PER_REQUEST['flash'])}* за запрос\n"
        f"Pro — *{fmt_usd(PRICE_PER_REQUEST['pro'])}* за запрос\n\n"
        f"Минимум {MIN_REQUESTS_PACK}, максимум {MAX_REQUESTS_PACK} запросов за покупку.\n"
        "Купленные запросы не сгорают и тратятся после суточной квоты тарифа.\n\n"
        "Выберите модель:"
    )
    try:
        await call.message.edit_text(text, reply_markup=get_packs_model_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except TelegramBadRequest:
        pass
    await call.answer()

@dp.callback_query(F.data.startswith("pack_model:"))
async def handle_pack_model(call: CallbackQuery):
    model = call.data.split(":")[1]
    if model not in PRICE_PER_REQUEST:
        await call.answer("Неизвестная модель", show_alert=True)
        return
    name = "⚡ Flash" if model == "flash" else "🧠 Pro"
    text = (
        f"{name} — *{fmt_usd(PRICE_PER_REQUEST[model])}* за запрос\n\n"
        "Выберите количество запросов:"
    )
    await call.message.edit_text(text, reply_markup=get_pack_counts_keyboard(model), parse_mode=ParseMode.MARKDOWN)
    await call.answer()

@dp.callback_query(F.data.startswith("pack_custom:"))
async def handle_pack_custom(call: CallbackQuery, state: FSMContext):
    model = call.data.split(":")[1]
    await state.set_state(UserStates.waiting_for_custom_pack)
    await state.update_data(pack_model=model)
    await call.message.edit_text(
        f"✏️ Введите количество запросов от *{MIN_REQUESTS_PACK}* до *{MAX_REQUESTS_PACK}*:",
        parse_mode=ParseMode.MARKDOWN
    )
    await call.answer()

@dp.message(StateFilter(UserStates.waiting_for_custom_pack))
async def handle_pack_custom_input(message: types.Message, state: FSMContext):
    data = await state.get_data()
    model = data.get("pack_model", "flash")
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("⚠️ Нужно целое число. Попробуйте снова:")
        return
    count = int(raw)
    if not (MIN_REQUESTS_PACK <= count <= MAX_REQUESTS_PACK):
        await message.answer(f"⚠️ Допустимый диапазон: {MIN_REQUESTS_PACK}–{MAX_REQUESTS_PACK}. Введите снова:")
        return
    await state.clear()
    await send_payment_options(message, f"pack:{model}:{count}")

# ─── Пополнение баланса ───

@dp.callback_query(F.data == "topup_menu")
async def handle_topup_menu(call: CallbackQuery):
    st = await get_user_state(call.from_user.id)
    text = (
        "💳 *Пополнение баланса*\n\n"
        f"Текущий баланс: *{fmt_usd(st['balance'])}*\n\n"
        "С баланса можно покупать подписки и пакеты запросов.\n"
        "Выберите сумму пополнения:"
    )
    try:
        await call.message.edit_text(text, reply_markup=get_topup_keyboard(), parse_mode=ParseMode.MARKDOWN)
    except TelegramBadRequest:
        pass
    await call.answer()

@dp.callback_query(F.data == "topup_custom")
async def handle_topup_custom(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_topup_amount)
    await call.message.edit_text(
        "✏️ Введите сумму пополнения в долларах (от *1* до *1000*), например `15` или `7.5`:",
        parse_mode=ParseMode.MARKDOWN
    )
    await call.answer()

@dp.message(StateFilter(UserStates.waiting_for_topup_amount))
async def handle_topup_custom_input(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".").lstrip("$")
    try:
        amount = usd(raw)
    except Exception:
        await message.answer("⚠️ Не похоже на сумму. Введите число, например `12.50`:", parse_mode=ParseMode.MARKDOWN)
        return
    if amount < Decimal("1") or amount > Decimal("1000"):
        await message.answer("⚠️ Сумма должна быть от $1 до $1000. Введите снова:")
        return
    await state.clear()
    await send_payment_options(message, f"topup:{amount:.2f}", allow_balance=False)

@dp.callback_query(F.data.startswith("topup:"))
async def handle_topup_preset(call: CallbackQuery):
    amount = usd(call.data.split(":")[1])
    await send_payment_options(call, f"topup:{amount:.2f}", allow_balance=False)
    await call.answer()

# ─── Выбор способа оплаты ───

async def send_payment_options(target: types.Message | CallbackQuery, purpose: str, allow_balance: bool = True):
    user_id = target.from_user.id
    amount = purpose_amount(purpose)
    st = await get_user_state(user_id)
    balance_enough = allow_balance and st["balance"] >= amount

    text = (
        f"🧾 *{purpose_title(purpose)}*\n\n"
        f"💵 К оплате: *{fmt_usd(amount)}*\n"
        f"⭐ В звёздах: *{usd_to_stars(amount)}* Stars\n"
        f"💰 Ваш баланс: *{fmt_usd(st['balance'])}*\n\n"
    )
    if allow_balance and not balance_enough:
        text += "_Баланса не хватает — оплатите звёздами или криптой либо пополните баланс._\n\n"
    text += "Выберите способ оплаты:"

    kb = get_payment_methods_keyboard(purpose, balance_enough)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    else:
        await target.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("buy:"))
async def handle_buy(call: CallbackQuery):
    purpose = call.data[len("buy:"):]
    await send_payment_options(call, purpose)
    await call.answer()

# ─── Оплата с баланса ───

@dp.callback_query(F.data.startswith("pay:balance:"))
async def handle_pay_balance(call: CallbackQuery):
    purpose = call.data[len("pay:balance:"):]
    amount = purpose_amount(purpose)
    if parse_purpose(purpose)["kind"] == "topup":
        await call.answer("Пополнение нельзя оплатить балансом", show_alert=True)
        return

    ok = await try_charge_balance(call.from_user.id, amount)
    if not ok:
        await call.answer("❌ Недостаточно средств на балансе", show_alert=True)
        return

    result = await deliver_purchase(call.from_user.id, purpose, "balance")
    st = await get_user_state(call.from_user.id)
    await call.message.edit_text(
        f"{result}\n\n💰 Остаток баланса: *{fmt_usd(st['balance'])}*",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Профиль", callback_data="profile_refresh")],
            [InlineKeyboardButton(text="🛒 В магазин", callback_data="shop_root")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await call.answer("Оплачено с баланса")

# ─── Оплата Telegram Stars ───

@dp.callback_query(F.data.startswith("pay:stars:"))
async def handle_pay_stars(call: CallbackQuery):
    purpose = call.data[len("pay:stars:"):]
    amount = purpose_amount(purpose)
    stars = usd_to_stars(amount)
    title = purpose_title(purpose)[:32]
    description = f"{purpose_title(purpose)} — {fmt_usd(amount)}"[:255]

    try:
        await bot.send_invoice(
            chat_id=call.from_user.id,
            title=title,
            description=description,
            payload=f"evo|{purpose}",
            currency="XTR",
            prices=[LabeledPrice(label=title, amount=stars)],
            provider_token="",
            start_parameter="evo-stars"
        )
        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO payments (user_id, provider, amount_usd, stars, purpose, status)
                VALUES ($1, 'stars', $2, $3, $4, 'pending')
            """, call.from_user.id, amount, stars, purpose)
        await call.answer("⭐ Счёт на оплату отправлен")
    except Exception as e:
        await call.answer(f"Ошибка счёта: {e}", show_alert=True)

@dp.pre_checkout_query()
async def handle_pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def handle_successful_payment(message: types.Message):
    sp = message.successful_payment
    payload = sp.invoice_payload or ""
    if not payload.startswith("evo|"):
        await message.answer("✅ Платёж получен, но заказ не распознан. Напишите в поддержку.")
        return
    purpose = payload[4:]
    user_id = message.from_user.id
    charge_id = sp.telegram_payment_charge_id

    async with db_pool.acquire() as conn:
        already_paid = await conn.fetchval(
            "SELECT 1 FROM payments WHERE external_id = $1 AND status = 'paid'", charge_id
        )
        if already_paid:
            await message.answer("✅ Этот платёж уже был успешно обработан.")
            return

        updated = await conn.fetchval("""
            UPDATE payments SET status = 'paid', paid_at = CURRENT_TIMESTAMP,
                external_id = $1
            WHERE id = (
                SELECT id FROM payments 
                WHERE user_id = $2 AND provider = 'stars' AND purpose = $3 AND status = 'pending'
                ORDER BY id DESC LIMIT 1
            )
            RETURNING id
        """, charge_id, user_id, purpose)

        if not updated:
            await conn.execute("""
                INSERT INTO payments (user_id, provider, external_id, amount_usd, stars, purpose, status, paid_at)
                VALUES ($1, 'stars', $2, $3, $4, $5, 'paid', CURRENT_TIMESTAMP)
            """, user_id, charge_id, purpose_amount(purpose), sp.total_amount, purpose)

    result = await deliver_purchase(user_id, purpose, "stars")
    await message.answer(
        f"⭐ *Оплата звёздами получена!*\n\n{result}",
        reply_markup=get_main_reply_keyboard(user_id == ADMIN_ID),
        parse_mode=ParseMode.MARKDOWN
    )

# ─── Оплата CryptoBot ───

@dp.callback_query(F.data.startswith("pay:crypto:"))
async def handle_pay_crypto(call: CallbackQuery):
    purpose = call.data[len("pay:crypto:"):]
    amount = purpose_amount(purpose)
    try:
        invoice_id, pay_url = await crypto_create_invoice(
            call.from_user.id, amount, purpose, purpose_title(purpose)
        )
    except CryptoPayError as e:
        await call.answer(f"CryptoBot недоступен: {e}", show_alert=True)
        return
    except Exception as e:
        await call.answer(f"Ошибка создания чека: {e}", show_alert=True)
        return

    text = (
        f"🪙 *Оплата через CryptoBot*\n\n"
        f"🧾 {purpose_title(purpose)}\n"
        f"💵 Сумма: *{amount:.2f} {CRYPTO_ASSET}*\n"
        f"🆔 Чек: `{invoice_id}`\n\n"
        "1. Нажмите «Оплатить чек»\n"
        "2. Оплатите в CryptoBot\n"
        "3. Вернитесь и нажмите «Я оплатил»\n\n"
        "_Чек действует 60 минут._"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Оплатить чек", url=pay_url)],
        [InlineKeyboardButton(text="🔄 Я оплатил — проверить", callback_data=f"crypto_check:{invoice_id}")],
        [InlineKeyboardButton(text="◀️ В магазин", callback_data="shop_root")]
    ])
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    await call.answer()

@dp.callback_query(F.data.startswith("crypto_check:"))
async def handle_crypto_check(call: CallbackQuery):
    invoice_id = call.data.split(":")[1]
    async with db_pool.acquire() as conn:
        pay = await conn.fetchrow("""
            SELECT user_id, purpose, status, amount_usd FROM payments
            WHERE provider = 'cryptobot' AND external_id = $1
        """, invoice_id)

    if not pay:
        await call.answer("Чек не найден в базе", show_alert=True)
        return
    if pay["user_id"] != call.from_user.id:
        await call.answer("Это чек другого пользователя", show_alert=True)
        return
    if pay["status"] == "paid":
        await call.answer("Этот чек уже зачислен", show_alert=True)
        return

    try:
        status = await crypto_check_invoice(invoice_id)
    except Exception as e:
        await call.answer(f"Не удалось проверить: {e}", show_alert=True)
        return

    if status != "paid":
        await call.answer(f"Оплата не найдена (статус: {status}). Попробуйте через минуту.", show_alert=True)
        return

    async with db_pool.acquire() as conn:
        updated = await conn.fetchval("""
            UPDATE payments SET status = 'paid', paid_at = CURRENT_TIMESTAMP
            WHERE provider = 'cryptobot' AND external_id = $1 AND status <> 'paid'
            RETURNING id
        """, invoice_id)
    if not updated:
        await call.answer("Этот чек уже зачислен", show_alert=True)
        return

    result = await deliver_purchase(call.from_user.id, pay["purpose"], "cryptobot")
    await call.message.edit_text(
        f"🪙 *Оплата подтверждена!*\n\n{result}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Профиль", callback_data="profile_refresh")],
            [InlineKeyboardButton(text="🛒 В магазин", callback_data="shop_root")]
        ]),
        parse_mode=ParseMode.MARKDOWN
    )
    await call.answer("Зачислено")

# ─── Системные промты (Pro и выше) ───

async def show_sysprompt_menu(target: types.Message | CallbackQuery, edit: bool = False):
    user_id = target.from_user.id
    st = await get_user_state(user_id)
    if not plan_allows_system_prompt(st["plan"]):
        text = (
            "🔒 *Системные промты доступны с тарифа Pro*\n\n"
            "На тарифах 🚀 Pro, 🔥 Max 5x и 👑 Max 20x можно задать постоянную инструкцию "
            "для модели текстом или файлом (.txt/.md).\n\n"
            f"Ваш тариф: *{st['cfg']['emoji']} {st['cfg']['title']}*"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Оформить Pro", callback_data="plan_view:pro")],
            [InlineKeyboardButton(text="👤 Профиль", callback_data="profile_refresh")]
        ])
    else:
        current = st["system_prompt"]
        preview = (current[:300] + "…") if current and len(current) > 300 else (current or "не задан")
        text = (
            "🧩 *Системный промт*\n\n"
            "Постоянная инструкция, которая добавляется к каждому запросу.\n"
            f"Лимит: {SYSTEM_PROMPT_LIMIT} символов. Можно отправить текст или файл `.txt`/`.md`.\n\n"
            f"*Текущий:*\n```\n{preview}\n```"
        )
        kb = get_sysprompt_keyboard(bool(current))

    if edit and isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        except TelegramBadRequest:
            pass
    else:
        msg = target.message if isinstance(target, CallbackQuery) else target
        await msg.answer(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "🧩 Системный промт")
async def handle_sysprompt_btn(message: types.Message):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    await show_sysprompt_menu(message)

@dp.callback_query(F.data == "sysprompt_menu")
async def handle_sysprompt_menu_cb(call: CallbackQuery):
    await show_sysprompt_menu(call, edit=True)
    await call.answer()

@dp.callback_query(F.data == "sysprompt_set")
async def handle_sysprompt_set(call: CallbackQuery, state: FSMContext):
    st = await get_user_state(call.from_user.id)
    if not plan_allows_system_prompt(st["plan"]):
        await call.answer("Нужен тариф Pro или выше", show_alert=True)
        return
    await state.set_state(UserStates.waiting_for_system_prompt)
    await call.message.edit_text(
        "✏️ Отправьте текст системного промта *или* прикрепите файл `.txt` / `.md`.\n\n"
        "Отправьте `отмена`, чтобы выйти.",
        parse_mode=ParseMode.MARKDOWN
    )
    await call.answer()

@dp.message(StateFilter(UserStates.waiting_for_system_prompt))
async def handle_sysprompt_input(message: types.Message, state: FSMContext):
    st = await get_user_state(message.from_user.id)
    if not plan_allows_system_prompt(st["plan"]):
        await state.clear()
        await message.answer("🔒 Системные промты доступны с тарифа Pro.")
        return

    raw_text = (message.text or "").strip()
    if raw_text.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("Отменено.")
        return

    prompt_text = None
    if message.document:
        name = (message.document.file_name or "").lower()
        if not name.endswith((".txt", ".md", ".markdown", ".prompt")):
            await message.answer("⚠️ Поддерживаются только файлы `.txt` и `.md`.", parse_mode=ParseMode.MARKDOWN)
            return
        if message.document.file_size and message.document.file_size > 200_000:
            await message.answer("⚠️ Файл слишком большой (максимум ~200 КБ).")
            return
        buf = io.BytesIO()
        await bot.download(message.document.file_id, destination=buf)
        try:
            prompt_text = buf.getvalue().decode("utf-8").strip()
        except UnicodeDecodeError:
            await message.answer("⚠️ Файл не в кодировке UTF-8.")
            return
    elif raw_text:
        prompt_text = raw_text

    if not prompt_text:
        await message.answer("⚠️ Пришлите текст или текстовый файл.")
        return

    if len(prompt_text) > SYSTEM_PROMPT_LIMIT:
        prompt_text = prompt_text[:SYSTEM_PROMPT_LIMIT]
        note = f"\n\n_Промт обрезан до {SYSTEM_PROMPT_LIMIT} символов._"
    else:
        note = ""

    await set_system_prompt(message.from_user.id, prompt_text)
    await state.clear()
    await message.answer(
        f"✅ Системный промт сохранён ({len(prompt_text)} символов).{note}",
        reply_markup=get_sysprompt_keyboard(True),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data == "sysprompt_show")
async def handle_sysprompt_show(call: CallbackQuery):
    st = await get_user_state(call.from_user.id)
    current = st["system_prompt"]
    if not current:
        await call.answer("Промт не задан", show_alert=True)
        return
    if len(current) > 3500:
        file = BufferedInputFile(current.encode("utf-8"), filename="system_prompt.md")
        await call.message.answer_document(file, caption="🧩 Ваш системный промт")
    else:
        await call.message.answer(f"🧩 *Ваш системный промт:*\n```\n{current}\n```", parse_mode=ParseMode.MARKDOWN)
    await call.answer()

@dp.callback_query(F.data == "sysprompt_clear")
async def handle_sysprompt_clear(call: CallbackQuery):
    await set_system_prompt(call.from_user.id, None)
    await call.answer("Системный промт удалён", show_alert=True)
    await show_sysprompt_menu(call, edit=True)

# ─── Промокоды (Пользователь) ───

@dp.message(F.text == "🎁 Промокод")
async def handle_enter_promo_btn(message: types.Message, state: FSMContext):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    await state.set_state(UserStates.waiting_for_promo_code)
    await message.answer("🎁 Введите промокод для активации бонуса:")

@dp.callback_query(F.data == "profile_promo")
async def handle_profile_promo(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_promo_code)
    await call.message.answer("🎁 Введите промокод для активации бонуса:")
    await call.answer()

@dp.message(StateFilter(UserStates.waiting_for_promo_code))
async def handle_promo_activation(message: types.Message, state: FSMContext):
    code_text = (message.text or "").strip().upper()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        promo = await conn.fetchrow("""
            SELECT code, reward, max_activations, used_count, expires_at,
                   grant_plan, grant_days, grant_flash, grant_pro
            FROM promocodes
            WHERE code = $1
        """, code_text)

        if not promo:
            await message.answer("❌ Промокод не найден или был удален.")
            await state.clear()
            return

        if promo["expires_at"] and promo["expires_at"] < datetime.now():
            await conn.execute("DELETE FROM promocodes WHERE code = $1", code_text)
            await message.answer("⌛ Срок действия данного промокода истек!")
            await state.clear()
            return

        if promo["used_count"] >= promo["max_activations"]:
            await message.answer("🚫 Этот промокод уже исчерпал лимит активаций.")
            await state.clear()
            return

        already_used = await conn.fetchval(
            "SELECT 1 FROM promocode_activations WHERE code = $1 AND user_id = $2",
            code_text, user_id
        )
        if already_used:
            await message.answer("⚠️ Вы уже активировали этот промокод ранее!")
            await state.clear()
            return

        async with conn.transaction():
            await conn.execute("INSERT INTO promocode_activations (code, user_id) VALUES ($1, $2)", code_text, user_id)
            await conn.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code = $1", code_text)
            if promo["reward"]:
                await conn.execute("UPDATE users SET balance = COALESCE(balance, 0) + $1 WHERE user_id = $2", usd(promo["reward"]), user_id)

    rewards = []
    if promo["reward"]:
        rewards.append(f"💰 +{fmt_usd(promo['reward'])} на баланс")
    if promo["grant_flash"]:
        await grant_requests(user_id, "flash", promo["grant_flash"])
        rewards.append(f"⚡ +{promo['grant_flash']} запросов Flash")
    if promo["grant_pro"]:
        await grant_requests(user_id, "pro", promo["grant_pro"])
        rewards.append(f"🧠 +{promo['grant_pro']} запросов Pro")
    if promo["grant_plan"] and promo["grant_plan"] in PLANS and (promo["grant_days"] or 0) > 0:
        exp = await grant_plan(user_id, promo["grant_plan"], promo["grant_days"])
        cfg = PLANS[promo["grant_plan"]]
        rewards.append(f"{cfg['emoji']} Подписка {cfg['title']} до {exp.strftime('%Y-%m-%d')}")

    body = "\n".join(rewards) if rewards else "Бонус применён."
    await message.answer(f"🎉 *Промокод активирован!*\n\n{body}", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

# ─── Админ-панель ───

@dp.message(Command("admin"))
@dp.message(F.text == "⚡ Админ панель")
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🛠 **Панель управления администратора**\nВыберите нужный раздел:",
        reply_markup=get_admin_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data == "admin_close")
async def handle_admin_close(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    await call.message.delete()

@dp.callback_query(F.data == "admin_metrics_audience")
async def handle_admin_metrics_audience(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM users")
        new_24h = await conn.fetchval("SELECT count(*) FROM users WHERE created_at >= NOW() - INTERVAL '24 hours'")
        new_7d = await conn.fetchval("SELECT count(*) FROM users WHERE created_at >= NOW() - INTERVAL '7 days'")
        new_30d = await conn.fetchval("SELECT count(*) FROM users WHERE created_at >= NOW() - INTERVAL '30 days'")
        dau = await conn.fetchval("SELECT count(*) FROM users WHERE last_activity >= NOW() - INTERVAL '24 hours'")
        banned = await conn.fetchval("SELECT count(*) FROM users WHERE is_banned = TRUE")
        blocked = await conn.fetchval("SELECT count(*) FROM users WHERE is_blocked = TRUE")
        plan_rows = await conn.fetch("SELECT COALESCE(plan, 'free') AS plan, count(*) AS cnt FROM users GROUP BY 1")

    plans_map = {r["plan"]: r["cnt"] for r in plan_rows}
    plans_str = "\n".join(
        f" ├ {PLANS[k]['emoji']} {PLANS[k]['title']}: `{plans_map.get(k, 0)}`" for k in PLAN_ORDER
    )

    text = (
        "📊 **Метрики аудитории:**\n\n"
        f"👥 **Всего пользователей:** `{total}`\n"
        f"🟢 **DAU (активные за 24ч):** `{dau}`\n\n"
        f"📈 **Новые пользователи:**\n"
        f" ├ За 24 часа: `+{new_24h}`\n"
        f" ├ За 7 дней: `+{new_7d}`\n"
        f" └ За 30 дней: `+{new_30d}`\n\n"
        f"🎫 **По тарифам:**\n{plans_str}\n\n"
        f"🚫 **Заблокированы администратором:** `{banned}`\n"
        f"🔕 **Заблокировали бота:** `{blocked}`"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]])
    await call.message.edit_text(text, reply_markup=back_kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "admin_metrics_activity")
async def handle_admin_metrics_activity(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        total_reqs = await conn.fetchval("SELECT count(*) FROM activity_logs")
        rows = await conn.fetch("SELECT content_type, count(*) as cnt FROM activity_logs GROUP BY content_type")
        types_map = {r["content_type"]: r["cnt"] for r in rows}

    text = (
        "📈 **Метрики активности бота:**\n\n"
        f"⚡ **Всего обработано запросов:** `{total_reqs}`\n\n"
        f"📦 **Распределение по типам:**\n"
        f" ├ 📝 Текст: `{types_map.get('text', 0)}`\n"
        f" ├ 🖼️ Фото / Изображения: `{types_map.get('photo', 0)}`\n"
        f" ├ 📄 Документы и Архивы: `{types_map.get('document', 0)}`\n"
        f" └ 🎙️ Аудио / Голосовые: `{types_map.get('audio', 0)}`"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]])
    await call.message.edit_text(text, reply_markup=back_kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "admin_metrics_sales")
async def handle_admin_metrics_sales(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        paid_total = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM payments WHERE status = 'paid'"
        )
        paid_30 = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM payments "
            "WHERE status = 'paid' AND paid_at >= NOW() - INTERVAL '30 days'"
        )
        by_provider = await conn.fetch(
            "SELECT provider, count(*) AS cnt, COALESCE(SUM(amount_usd), 0) AS sum "
            "FROM payments WHERE status = 'paid' GROUP BY provider"
        )
        pending = await conn.fetchval("SELECT count(*) FROM payments WHERE status = 'pending'")
        by_kind = await conn.fetch(
            "SELECT kind, count(*) AS cnt, COALESCE(SUM(amount_usd), 0) AS sum FROM purchases GROUP BY kind"
        )
        balances = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users")

    prov_str = "\n".join(
        f" ├ {r['provider']}: `{r['cnt']}` шт · {fmt_usd(r['sum'])}" for r in by_provider
    ) or " └ пока нет оплат"
    kind_str = "\n".join(
        f" ├ {r['kind']}: `{r['cnt']}` шт · {fmt_usd(r['sum'])}" for r in by_kind
    ) or " └ пока нет покупок"

    text = (
        "💵 **Метрики продаж:**\n\n"
        f"💰 Оплачено всего: *{fmt_usd(paid_total)}*\n"
        f"📆 За 30 дней: *{fmt_usd(paid_30)}*\n"
        f"⏳ Ожидают оплаты: `{pending}`\n\n"
        f"🏦 **По провайдерам:**\n{prov_str}\n\n"
        f"🛒 **По типам покупок:**\n{kind_str}\n\n"
        f"👛 Суммарный баланс пользователей: *{fmt_usd(balances)}*"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]])
    await call.message.edit_text(text, reply_markup=back_kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "admin_back_main")
async def handle_admin_back_main(call: CallbackQuery, state: FSMContext = None):
    if call.from_user.id != ADMIN_ID:
        return
    if state:
        await state.clear()
    await call.message.edit_text(
        "🛠 **Панель управления администратора**\nВыберите нужный раздел:",
        reply_markup=get_admin_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data == "admin_user_search")
async def handle_admin_user_search_prompt(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_for_user_query)
    await call.message.edit_text(
        "🔍 Введите **user_id** или **@username** для поиска:",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(StateFilter(AdminStates.waiting_for_user_query))
async def handle_admin_user_search_exec(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    q = (message.text or "").strip().replace("@", "")
    async with db_pool.acquire() as conn:
        if q.isdigit():
            user = await conn.fetchrow("SELECT * FROM users WHERE user_id = $1", int(q))
        else:
            user = await conn.fetchrow("SELECT * FROM users WHERE LOWER(username) = LOWER($1)", q)

    if not user:
        await message.answer("❌ Пользователь не найден в базе данных.")
        await state.clear()
        return

    uid = user["user_id"]
    st = await get_user_state(uid)
    async with db_pool.acquire() as conn:
        chats = await conn.fetch("SELECT id, title FROM chats WHERE user_id = $1 ORDER BY id DESC LIMIT 5", uid)
        req_count = await conn.fetchval("SELECT count(*) FROM activity_logs WHERE user_id = $1", uid)
        spent = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM payments WHERE user_id = $1 AND status = 'paid'", uid
        )

    chats_list_str = "\n".join([f" • ID {c['id']}: {c['title']}" for c in chats]) if chats else "Нет чатов"
    ban_status = "🔴 Заблокирован" if user["is_banned"] else "🟢 Активен"
    plan_cfg = st["cfg"]
    plan_line = f"{plan_cfg['emoji']} {plan_cfg['title']}"
    if st["plan_expires"]:
        plan_line += f" (до {st['plan_expires'].strftime('%Y-%m-%d')})"

    card = (
        f"👤 **Карточка пользователя:**\n\n"
        f"🆔 **ID:** `{uid}`\n"
        f"👤 **Username:** @{user['username'] or 'нет'}\n"
        f"📛 **Имя:** {user['first_name']}\n"
        f"💰 **Баланс:** *{fmt_usd(user['balance'])}*\n"
        f"🎫 **Тариф:** {plan_line}\n"
        f"🎟 **Запросы:** ⚡ `{st['flash_credits']}` · 🧠 `{st['pro_credits']}`\n"
        f"💳 **Оплачено всего:** *{fmt_usd(spent)}*\n"
        f"Статус: **{ban_status}**\n"
        f"📅 **Регистрация:** {user['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
        f"⚡ **Последняя активность:** {user['last_activity'].strftime('%Y-%m-%d %H:%M')}\n"
        f"📊 **Всего запросов:** `{req_count}`\n\n"
        f"💬 **Последние диалоги:**\n{chats_list_str}"
    )

    ban_btn_text = "✅ Разбанить" if user["is_banned"] else "🚫 Забанить"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=ban_btn_text, callback_data=f"admin_toggle_ban:{uid}")],
            [InlineKeyboardButton(text="💵 Изменить баланс", callback_data=f"admin_balance:{uid}")],
            [InlineKeyboardButton(text="🧹 Сбросить контекст/сессию", callback_data=f"admin_reset_session:{uid}")],
            [InlineKeyboardButton(text="🗑️ Удалить все диалоги", callback_data=f"admin_clear_chats:{uid}")],
            [InlineKeyboardButton(text="◀️ Меню админки", callback_data="admin_back_main")]
        ]
    )
    await message.answer(card, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    await state.clear()

@dp.callback_query(F.data.startswith("admin_balance:"))
async def handle_admin_balance_prompt(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data.split(":")[1])
    await state.set_state(AdminStates.waiting_for_grant_balance)
    await state.update_data(target_uid=uid)
    await call.message.answer(
        f"💵 Введите сумму в долларах для начисления пользователю `{uid}`.\n"
        "Отрицательное число — списание. Например: `10` или `-5.5`",
        parse_mode=ParseMode.MARKDOWN
    )
    await call.answer()

@dp.message(StateFilter(AdminStates.waiting_for_grant_balance))
async def handle_admin_balance_exec(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    uid = data.get("target_uid")
    try:
        amount = usd((message.text or "").strip().replace(",", ".").lstrip("$"))
    except Exception:
        await message.answer("⚠️ Введите число, например `25` или `-10`:", parse_mode=ParseMode.MARKDOWN)
        return
    new_bal = await add_balance(uid, amount)
    await state.clear()
    await message.answer(
        f"✅ Баланс пользователя `{uid}` изменён на *{fmt_usd(amount)}*.\n"
        f"💰 Новый баланс: *{fmt_usd(new_bal)}*",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data.startswith("admin_toggle_ban:"))
async def handle_admin_toggle_ban(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data.split(":")[1])
    async with db_pool.acquire() as conn:
        current = await conn.fetchval("SELECT is_banned FROM users WHERE user_id = $1", uid)
        new_val = not current
        await conn.execute("UPDATE users SET is_banned = $1 WHERE user_id = $2", new_val, uid)

    await call.answer(f"Статус бана изменен на: {'Забанен' if new_val else 'Разбанен'}", show_alert=True)
    await handle_admin_back_main(call)

@dp.callback_query(F.data.startswith("admin_reset_session:"))
async def handle_admin_reset_session(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data.split(":")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM active_sessions WHERE user_id = $1", uid)
    await call.answer("Сессия и активный контекст пользователя сброшены!", show_alert=True)

@dp.callback_query(F.data.startswith("admin_clear_chats:"))
async def handle_admin_clear_chats(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    uid = int(call.data.split(":")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM chats WHERE user_id = $1", uid)
        await conn.execute("DELETE FROM active_sessions WHERE user_id = $1", uid)
    await call.answer("Все чаты пользователя полностью удалены!", show_alert=True)

# ─── Генератор промокодов ───

@dp.callback_query(F.data == "admin_create_promo")
async def handle_admin_create_promo_step1(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_for_promo_reward)
    await call.message.edit_text(
        "🎁 **Шаг 1/3:** Введите содержимое промокода.\n\n"
        "Форматы:\n"
        "• `5` или `$5` — начислить $5 на баланс\n"
        "• `flash:50` — 50 запросов Flash\n"
        "• `pro:20` — 20 запросов Pro\n"
        "• `plan:pro:30` — подписка Pro на 30 дней\n"
        "Можно комбинировать через `+`: `5+flash:20+plan:plus:7`",
        parse_mode=ParseMode.MARKDOWN
    )

def parse_promo_spec(raw: str) -> dict | None:
    """Разбирает строку награды промокода в структуру грантов."""
    result = {"reward": Decimal("0"), "grant_plan": None, "grant_days": 0, "grant_flash": 0, "grant_pro": 0}
    ok = False
    for part in raw.replace(" ", "").split("+"):
        if not part:
            continue
        chunks = part.split(":")
        head = chunks[0].lower()
        if head == "plan" and len(chunks) == 3 and chunks[1] in PLANS and chunks[2].isdigit():
            result["grant_plan"] = chunks[1]
            result["grant_days"] = int(chunks[2])
            ok = True
        elif head in ("flash", "req", "requests") and len(chunks) == 2 and chunks[1].isdigit():
            result["grant_flash"] = int(chunks[1])
            ok = True
        elif head == "pro" and len(chunks) == 2 and chunks[1].isdigit():
            result["grant_pro"] = int(chunks[1])
            ok = True
        elif head in ("usd", "balance", "bal") and len(chunks) == 2:
            try:
                result["reward"] += usd(chunks[1].replace(",", ".").lstrip("$"))
                ok = True
            except Exception:
                return None
        else:
            try:
                result["reward"] += usd(part.replace(",", ".").lstrip("$"))
                ok = True
            except Exception:
                return None
    return result if ok else None

@dp.message(StateFilter(AdminStates.waiting_for_promo_reward))
async def handle_admin_create_promo_step2(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    spec = parse_promo_spec((message.text or "").strip())
    if not spec:
        await message.answer("⚠️ Не разобрал формат. Пример: `10` или `flash:50+plan:pro:30`", parse_mode=ParseMode.MARKDOWN)
        return
    await state.update_data(promo_spec=spec)
    await state.set_state(AdminStates.waiting_for_promo_activations)
    await message.answer("👥 **Шаг 2/3:** Введите максимальное число активаций (например: 10):", parse_mode=ParseMode.MARKDOWN)

@dp.message(StateFilter(AdminStates.waiting_for_promo_activations))
async def handle_admin_create_promo_step3(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        activations = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Введите целое число активаций:")
        return
    await state.update_data(promo_activations=activations)
    await state.set_state(AdminStates.waiting_for_promo_duration)
    await message.answer("⏱ **Шаг 3/3:** Введите время жизни промокода в минутах (0 — бессрочно):", parse_mode=ParseMode.MARKDOWN)

@dp.message(StateFilter(AdminStates.waiting_for_promo_duration))
async def handle_admin_create_promo_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        duration_minutes = int((message.text or "").strip())
    except ValueError:
        await message.answer("⚠️ Введите число минут (0 — бессрочно):")
        return

    data = await state.get_data()
    spec = data["promo_spec"]
    activations = data["promo_activations"]
    code = f"EVO-{secrets.token_hex(3).upper()}"
    expires_at = datetime.now() + timedelta(minutes=duration_minutes) if duration_minutes > 0 else None

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO promocodes (code, reward, max_activations, expires_at,
                                    grant_plan, grant_days, grant_flash, grant_pro)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """, code, usd(spec["reward"]), activations, expires_at,
             spec["grant_plan"], spec["grant_days"], spec["grant_flash"], spec["grant_pro"])

    bonus_lines = []
    if spec["reward"]:
        bonus_lines.append(f"💰 {fmt_usd(spec['reward'])} на баланс")
    if spec["grant_flash"]:
        bonus_lines.append(f"⚡ {spec['grant_flash']} запросов Flash")
    if spec["grant_pro"]:
        bonus_lines.append(f"🧠 {spec['grant_pro']} запросов Pro")
    if spec["grant_plan"]:
        cfg = PLANS[spec["grant_plan"]]
        bonus_lines.append(f"{cfg['emoji']} {cfg['title']} на {spec['grant_days']} дн.")

    exp_str = expires_at.strftime('%Y-%m-%d %H:%M') if expires_at else "Бессрочно"
    res_text = (
        f"✅ **Промокод успешно создан!**\n\n"
        f"🔑 **Код:** `{code}`\n"
        f"🎁 **Бонус:**\n" + "\n".join(f" • {b}" for b in bonus_lines) + "\n"
        f"👥 **Лимит активаций:** `{activations}`\n"
        f"⌛ **Действителен до:** `{exp_str}`"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В панель", callback_data="admin_back_main")]])
    await message.answer(res_text, reply_markup=back_kb, parse_mode=ParseMode.MARKDOWN)
    await state.clear()

# ─── Система Рассылки сообщений ───

@dp.callback_query(F.data == "admin_broadcast_start")
async def handle_broadcast_step1(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Всем пользователям", callback_data="broad_target:all")],
            [InlineKeyboardButton(text="⏱ Активным за 1 день", callback_data="broad_target:1"),
             InlineKeyboardButton(text="⏱ Активным за 7 дней", callback_data="broad_target:7")],
            [InlineKeyboardButton(text="⏱ Активным за 30 дней", callback_data="broad_target:30")],
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="admin_back_main")]
        ]
    )
    await call.message.edit_text("🎯 **Выберите аудиторию для рассылки:**", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("broad_target:"))
async def handle_broadcast_step2(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    target = call.data.split(":")[1]
    await state.update_data(broadcast_target=target)
    await state.set_state(AdminStates.waiting_for_broadcast_content)
    await call.message.edit_text(
        "📝 **Отправьте сообщение для рассылки.**\n"
        "Поддерживается: *текст, фото с описанием или видео с описанием.*",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(StateFilter(AdminStates.waiting_for_broadcast_content))
async def handle_broadcast_step3(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    content_data = {
        "text": message.html_text if message.text else message.caption,
        "photo": message.photo[-1].file_id if message.photo else None,
        "video": message.video.file_id if message.video else None,
    }
    await state.update_data(broadcast_payload=content_data)
    await state.set_state(AdminStates.waiting_for_broadcast_buttons)

    await message.answer(
        "🔘 **Добавить Inline-кнопку?**\n"
        "Отправьте в формате: `Текст кнопки | https://ссылка`\n"
        "Или отправьте `0` / `пропустить`, если кнопка не нужна.",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(StateFilter(AdminStates.waiting_for_broadcast_buttons))
async def handle_broadcast_execute(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    payload = data["broadcast_payload"]
    target = data["broadcast_target"]

    reply_markup = None
    btn_text = (message.text or "").strip()
    if btn_text not in ["0", "пропустить", "нет", "none"]:
        if "|" in btn_text:
            title, url = btn_text.split("|", 1)
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=title.strip(), url=url.strip())]])

    async with db_pool.acquire() as conn:
        if target == "all":
            rows = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE AND is_blocked = FALSE")
        else:
            days = int(target)
            rows = await conn.fetch("""
                SELECT user_id FROM users
                WHERE is_banned = FALSE AND is_blocked = FALSE
                AND last_activity >= NOW() - make_interval(days => $1::int)
            """, days)

    user_ids = [r["user_id"] for r in rows]
    total = len(user_ids)

    status_msg = await message.answer(f"🚀 Запуск рассылки на {total} получателей...")

    success, blocked, failed = 0, 0, 0
    for i, uid in enumerate(user_ids):
        try:
            if payload["photo"]:
                await bot.send_photo(chat_id=uid, photo=payload["photo"], caption=payload["text"], parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            elif payload["video"]:
                await bot.send_video(chat_id=uid, video=payload["video"], caption=payload["text"], parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=uid, text=payload["text"], parse_mode=ParseMode.HTML, reply_markup=reply_markup)
            success += 1
        except TelegramForbiddenError:
            blocked += 1
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE users SET is_blocked = TRUE WHERE user_id = $1", uid)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            failed += 1
        except Exception:
            failed += 1

        if (i + 1) % 25 == 0:
            await asyncio.sleep(1.0)
        else:
            await asyncio.sleep(0.04)

    report = (
        f"📊 **Отчет о рассылке:**\n\n"
        f"👥 Получателей в выборке: `{total}`\n"
        f"✅ Успешно доставлено: `{success}`\n"
        f"🔕 Заблокировали бота: `{blocked}`\n"
        f"❌ Ошибок отправки: `{failed}`"
    )
    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)
    await state.clear()

# ─── Обработка подписки и /start ───

@dp.callback_query(F.data == "verify_subscription")
async def verify_sub_callback(call: CallbackQuery):
    is_sub = await check_subscription_status(call.from_user.id)
    if is_sub:
        await track_user(call.from_user)
        await call.message.delete()
        await call.message.answer(
            "🎉 Спасибо за подписку! Доступ к Evo Lumen 1.0 разблокирован.",
            reply_markup=get_main_reply_keyboard(call.from_user.id == ADMIN_ID),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await call.answer("❌ Вы еще не подписались на канал!", show_alert=True)

@dp.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    if await is_user_banned(message.from_user.id):
        await message.answer("⛔ Вы заблокированы в системе.")
        return

    await track_user(message.from_user)
    is_sub = await check_subscription_status(message.from_user.id)
    if not is_sub:
        await message.answer(
            "🔒 Для использования бота необходимо подписаться на наш официальный канал!\n\n"
            "Подпишитесь и нажмите кнопку «Подтвердить подписку» ниже.",
            reply_markup=get_sub_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    await get_or_create_active_chat(message.from_user.id)
    st = await get_user_state(message.from_user.id)
    free = PLANS["free"]
    greeting = (
        "👋 Здравствуйте! Я **Evo Lumen 1.0** — искусственный интеллект, "
        "разработанный компанией **Quantum**.\n\n"
        "✨ **Возможности:**\n"
        "• Мгновенные ответы и решение задач\n"
        "• Проектирование и аудит сложного программного кода\n"
        "• Анализ голосовых сообщений и аудио\n"
        "• Чтение документов (PDF, DOCX, EPUB, FB2, TXT, ZIP)\n"
        "• Экспорт истории чата в `.md` / `.txt`\n"
        "• Анализ веб-страниц по ссылкам\n"
        "• Системные промты на тарифах Pro и выше\n\n"
        f"🎁 **Бесплатный тариф:** {free['flash_day']} запросов ⚡ Flash в сутки.\n"
        f"🚀 Подписки от *{fmt_usd(PLANS['plus']['price_week'])}* в неделю, "
        f"пакеты запросов от *{MIN_REQUESTS_PACK}* шт.\n"
        f"💵 Все цены в долларах. Оплата: ⭐ Telegram Stars и 🪙 CryptoBot.\n\n"
        f"🎫 Ваш тариф: *{st['cfg']['emoji']} {st['cfg']['title']}* · "
        f"💰 Баланс: *{fmt_usd(st['balance'])}*"
    )
    await message.answer(
        greeting,
        reply_markup=get_main_reply_keyboard(message.from_user.id == ADMIN_ID),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(Command("plans"))
async def cmd_plans(message: types.Message):
    await message.answer(render_plans_list(), reply_markup=get_plans_keyboard(), parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("model"))
async def cmd_model(message: types.Message):
    st = await get_user_state(message.from_user.id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"🔀 Переключить (сейчас: {'⚡ Flash' if st['active_model'] == 'flash' else '🧠 Pro'})",
        callback_data="toggle_model"
    )]])
    await message.answer(
        f"🔀 Активная модель: *{'⚡ Flash' if st['active_model'] == 'flash' else '🧠 Pro'}*\n\n"
        f"⚡ Flash — быстрые ответы, {fmt_usd(PRICE_PER_REQUEST['flash'])}/запрос\n"
        f"🧠 Pro — глубокие рассуждения и аудит кода, {fmt_usd(PRICE_PER_REQUEST['pro'])}/запрос",
        reply_markup=kb, parse_mode=ParseMode.MARKDOWN
    )

# ─── Вспомогательные функции: URL и Аудио ───

async def extract_urls_content(text: str) -> str:
    urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', text)
    if not urls:
        return ""
    extracted_pages = []
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as session:
        for url in urls[:3]:
            target_url = url if url.startswith("http") else f"http://{url}"
            try:
                async with session.get(target_url) as resp:
                    if resp.status == 200:
                        html = await resp.text(errors="ignore")
                        soup = BeautifulSoup(html, "html.parser")
                        for elem in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                            elem.extract()
                        text_data = " ".join(soup.get_text().split())[:8000]
                        extracted_pages.append(f"🌐 Содержимое страницы ({url}):\n{text_data}")
            except Exception as e:
                extracted_pages.append(f"🌐 [Не удалось загрузить {url}: {str(e)}]")
    return "\n\n" + "\n\n".join(extracted_pages) if extracted_pages else ""

async def transcribe_audio_file(file_id: str) -> str:
    try:
        file_io = io.BytesIO()
        await bot.download(file_id, destination=file_io)
        audio_bytes = file_io.getvalue()
        transcription = await client_flash.audio.transcriptions.create(
            model="whisper-1",
            file=("audio.ogg", audio_bytes, "audio/ogg")
        )
        return transcription.text
    except Exception as e:
        return f"[Ошибка распознавания аудио: {str(e)}]"

# ─── Извлечение контента из сообщений ───

async def extract_content_from_message(message: types.Message) -> tuple[str, list, str]:
    text_content = message.caption or message.text or ""
    image_payloads = []
    content_type = "text"

    if message.voice:
        content_type = "audio"
        transcription = await transcribe_audio_file(message.voice.file_id)
        text_content = f"{text_content}\n\n🎙 Голосовое сообщение: {transcription}" if text_content else transcription
    elif message.audio:
        content_type = "audio"
        transcription = await transcribe_audio_file(message.audio.file_id)
        text_content = f"{text_content}\n\n🎙 Аудиозапись: {transcription}" if text_content else transcription
    elif message.photo:
        content_type = "photo"
        photo = message.photo[-1]
        file_io = io.BytesIO()
        await bot.download(photo.file_id, destination=file_io)
        b64_img = base64.b64encode(file_io.getvalue()).decode("utf-8")
        image_payloads.append(b64_img)
        if not text_content:
            text_content = "Проанализируй прикрепленное изображение."
    elif message.document:
        content_type = "document"
        doc = message.document
        file_io = io.BytesIO()
        await bot.download(doc.file_id, destination=file_io)
        file_bytes = file_io.getvalue()
        file_name = doc.file_name or "file"
        lower_name = file_name.lower()

        if lower_name.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                    file_list = z.namelist()
                    extracted_texts = []
                    for name in file_list[:15]:
                        if not name.endswith("/"):
                            try:
                                content = z.read(name).decode("utf-8", errors="ignore")[:3000]
                                extracted_texts.append(f"--- Файл: {name} ---\n{content}")
                            except Exception:
                                pass
                    archive_info = f"📦 Содержимое ZIP архива ({file_name}):\nСписок файлов: {', '.join(file_list[:30])}\n\n" + "\n\n".join(extracted_texts)
                    text_content = f"{text_content}\n\n{archive_info}" if text_content else archive_info
            except Exception as e:
                text_content = f"{text_content}\n\n[Ошибка распаковки архива {file_name}: {str(e)}]"
        elif lower_name.endswith(".pdf"):
            try:
                reader = PdfReader(io.BytesIO(file_bytes))
                pdf_texts = [p.extract_text() for p in reader.pages[:40] if p.extract_text()]
                pdf_combined = "\n".join(pdf_texts)[:15000]
                info = f"📄 Документ PDF ({file_name}):\n{pdf_combined}"
                text_content = f"{text_content}\n\n{info}" if text_content else info
            except Exception as e:
                text_content = f"{text_content}\n\n[Ошибка чтения PDF {file_name}: {str(e)}]"
        elif lower_name.endswith(".docx"):
            try:
                doc_obj = docx.Document(io.BytesIO(file_bytes))
                doc_text = "\n".join([p.text for p in doc_obj.paragraphs if p.text])[:15000]
                info = f"📄 Документ DOCX ({file_name}):\n{doc_text}"
                text_content = f"{text_content}\n\n{info}" if text_content else info
            except Exception as e:
                text_content = f"{text_content}\n\n[Ошибка чтения DOCX {file_name}: {str(e)}]"
        elif lower_name.endswith(".fb2"):
            try:
                root = ET.fromstring(file_bytes)
                fb2_text = "".join(root.itertext())[:15000]
                info = f"📚 Книга FB2 ({file_name}):\n{fb2_text}"
                text_content = f"{text_content}\n\n{info}" if text_content else info
            except Exception as e:
                text_content = f"{text_content}\n\n[Ошибка чтения FB2 {file_name}: {str(e)}]"
        elif lower_name.endswith(".epub"):
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                    epub_texts = []
                    for item in z.namelist():
                        if item.lower().endswith(('.html', '.xhtml', '.htm')):
                            try:
                                raw_html = z.read(item).decode('utf-8', errors='ignore')
                                soup = BeautifulSoup(raw_html, 'html.parser')
                                if soup.get_text().strip():
                                    epub_texts.append(soup.get_text())
                            except Exception:
                                pass
                    epub_combined = "\n".join(epub_texts)[:15000]
                    info = f"📚 Книга EPUB ({file_name}):\n{epub_combined}"
                    text_content = f"{text_content}\n\n{info}" if text_content else info
            except Exception as e:
                text_content = f"{text_content}\n\n[Ошибка чтения EPUB {file_name}: {str(e)}]"
        else:
            try:
                decoded = file_bytes.decode("utf-8")
                text_content = f"{text_content}\n\n📄 Файл `{file_name}`:\n```\n{decoded[:12000]}\n```"
            except UnicodeDecodeError:
                text_content = f"{text_content}\n\n📎 Получен бинарный файл `{file_name}` размером {len(file_bytes)} байт."

    if text_content:
        urls_info = await extract_urls_content(text_content)
        if urls_info:
            text_content += urls_info

    return text_content, image_payloads, content_type

# ─── Клавиатура при исчерпании лимита ───

def get_limit_keyboard(model: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Оформить подписку", callback_data="shop_plans")],
        [InlineKeyboardButton(text=f"⚡ Купить запросы {'Flash' if model == 'flash' else 'Pro'}",
                              callback_data=f"pack_model:{model}")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile_refresh")]
    ])

# ─── Основной обработчик запросов к ИИ ───

@dp.message()
async def handle_all_prompts(message: types.Message):
    user_id = message.from_user.id

    if await is_user_banned(user_id):
        await message.answer("⛔ Вы заблокированы в системе.")
        return

    await track_user(message.from_user)

    is_sub = await check_subscription_status(user_id)
    if not is_sub:
        await message.answer(
            "🔒 Доступ ограничен! Для продолжения диалога подпишитесь на наш канал.",
            reply_markup=get_sub_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    st = await get_user_state(user_id)
    model = st["active_model"] if st["active_model"] in PRICE_PER_REQUEST else "flash"

    # Антифлуд зависит от тарифа
    rate_limit = st["cfg"]["rate_limit"]
    now = time.time()
    last_time = user_last_request_time.get(user_id, 0)
    if user_id != ADMIN_ID and (now - last_time) < rate_limit:
        remaining = int(rate_limit - (now - last_time)) + 1
        await message.answer(
            f"⏳ *Антифлуд:* подождите `{remaining}` сек.\n"
            f"На вашем тарифе {st['cfg']['emoji']} {st['cfg']['title']} — 1 запрос в {rate_limit} сек.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Проверка доступности запросов
    quota_left = st["left_flash"] if model == "flash" else st["left_pro"]
    credits = st["flash_credits"] if model == "flash" else st["pro_credits"]
    if user_id != ADMIN_ID and quota_left <= 0 and credits <= 0:
        model_name = "⚡ Flash" if model == "flash" else "🧠 Pro"
        other = "pro" if model == "flash" else "flash"
        other_ok = (st["left_pro"] + st["pro_credits"]) if model == "flash" else (st["left_flash"] + st["flash_credits"])
        hint = f"\n\n_Доступно на модели {'🧠 Pro' if other == 'pro' else '⚡ Flash'}: {other_ok} запросов — переключитесь в профиле._" if other_ok > 0 else ""
        await message.answer(
            f"🚫 *Лимит исчерпан* по модели {model_name}.\n\n"
            f"🎫 Тариф: {st['cfg']['emoji']} {st['cfg']['title']}\n"
            f"Суточная квота обнулится в 00:00.\n\n"
            f"Купите подписку или пакет запросов "
            f"({fmt_usd(PRICE_PER_REQUEST[model])} за запрос, от {MIN_REQUESTS_PACK} шт.).{hint}",
            reply_markup=get_limit_keyboard(model),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    prompt_text, images, content_type = await extract_content_from_message(message)
    if not prompt_text and not images:
        return

    allowed, source = await consume_request(user_id, model)
    if not allowed:
        await message.answer(
            "🚫 Запросы закончились. Загляните в магазин.",
            reply_markup=get_limit_keyboard(model),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    user_last_request_time[user_id] = now
    await log_activity(user_id, content_type)

    active_chat_id = await get_or_create_active_chat(user_id)
    history = await get_chat_messages(active_chat_id, limit=st["cfg"]["history"])
    await save_message(active_chat_id, "user", prompt_text)

    model_label = "⚡ Flash" if model == "flash" else "🧠 Pro"
    status_msg = await message.answer(
        f"{model_label} *Evo Lumen 1.0* анализирует задачу...\n⏱ _Время размышления:_ *0 сек.*",
        parse_mode=ParseMode.MARKDOWN
    )
    updater = StatusUpdater(status_msg)
    updater.start()

    ok = False
    try:
        messages_payload = []

        if st["system_prompt"] and plan_allows_system_prompt(st["plan"]):
            messages_payload.append({"role": "system", "content": st["system_prompt"]})

        for hist in history:
            messages_payload.append({"role": hist["role"], "content": hist["content"]})

        if images:
            user_content = [{"type": "text", "text": prompt_text}]
            for img in images:
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img}"}
                })
            messages_payload.append({"role": "user", "content": user_content})
        else:
            messages_payload.append({"role": "user", "content": prompt_text})

        if model == "pro":
            updater.set_stage("🧠 *Evo Lumen 1.0 Pro* строит глубокое решение...")
            pro_res = await client_pro.chat.completions.create(
                model=MODEL_PRO,
                messages=messages_payload,
                temperature=0.1
            )
            final_answer = pro_res.choices[0].message.content.strip()
        else:
            flash_res = await client_flash.chat.completions.create(
                model=MODEL_FLASH,
                messages=messages_payload,
                temperature=0.2
            )
            final_answer = flash_res.choices[0].message.content.strip()
            final_answer = final_answer.replace("[MODE: DIRECT]", "").replace("[MODE: CODE_DRAFT]", "").strip()

        await save_message(active_chat_id, "assistant", final_answer)
        ok = True

    except Exception as e:
        final_answer = f"⚠️ **Ошибка при обработке запроса:** `{str(e)}`\n\n_Запрос возвращён на ваш счёт._"
    finally:
        await updater.stop()
        try:
            await status_msg.delete()
        except Exception:
            pass

    if not ok:
        await refund_request(user_id, model, source)

    await send_response(message, final_answer, user_prompt=prompt_text)

# ─── Запуск приложения ───

async def main():
    await init_db()
    print("🚀 База данных PostgreSQL инициализирована.")
    print(f"👑 Админ панель активирована для ID: {ADMIN_ID}")
    print(f"💵 Валюта: USD · Flash {PRICE_PER_REQUEST['flash']}$ · Pro {PRICE_PER_REQUEST['pro']}$")
    print(f"⭐ Курс звёзд: {STARS_PER_USD} Stars = 1$")
    print(f"🪙 CryptoBot: {'подключен' if CRYPTO_PAY_TOKEN else 'ТОКЕН НЕ ЗАДАН'}")

    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
            me = await bot.get_me()
            print(f"🤖 Бот @{me.username} успешно авторизован в Telegram и готов к работе!")
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
            break
        except (TelegramNetworkError, asyncio.TimeoutError, aiohttp.ClientError) as e:
            print(f"⚠️ Сетевая ошибка при подключении к Telegram ({e}). Повторное подключение через 5 сек...")
            await asyncio.sleep(5)
        except Exception as e:
            print(f"❌ Критическая ошибка при работе бота: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
