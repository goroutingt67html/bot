import os
import io
import re
import time
import json
import base64
import zipfile
import asyncio
import secrets
from datetime import datetime, timedelta
import aiohttp
import asyncpg
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from pypdf import PdfReader
import docx
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
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

# ─── Конфигурация токенов и окружения ───

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
FLASH_API_KEY = os.getenv("FLASH_API_KEY", "")
PRO_API_KEY = os.getenv("PRO_API_KEY", "")
CRYPTO_BOT_TOKEN = os.getenv("CRYPTO_BOT_TOKEN", "")

RAW_DB_URL = os.getenv("DATABASE_URL", "")
DATABASE_URL = RAW_DB_URL.replace("postgres://", "postgresql://") if RAW_DB_URL else ""

ADMIN_ID = 5480751648
RATE_LIMIT_SECONDS = 15

# Цены в USD ($) и курс Stars
PRICE_FLASH_USD = 0.050
PRICE_PRO_USD = 0.120
STARS_RATE_PER_USD = 50  # 1 USD = 50 Stars

REQUEST_PRICES = {
    "flash": PRICE_FLASH_USD,
    "pro": PRICE_PRO_USD
}

TIERS_PRICING = {
    "standard": {"name": "Standard Plan", "price_usd": 4.99, "stars": 250, "days": 30, "limit_5h": 40, "cooldown": 7},
    "pro": {"name": "Pro Plan (Thinking)", "price_usd": 14.99, "stars": 750, "days": 30, "limit_5h": 100, "cooldown": 2},
    "ultra": {"name": "Ultra Plan (Dev)", "price_usd": 29.99, "stars": 1500, "days": 30, "limit_5h": 250, "cooldown": 0}
}

CHANNEL_USERNAME = "@Quantum_Evo"
CHANNEL_URL = "https://t.me/Quantum_Evo"

FLASH_BASE_URL = "https://gorouter.app/v1"
MODEL_FLASH = "claude-opus-4-8"

PRO_BASE_URL = "https://gorouter.app/v1"
MODEL_PRO = "claude-opus-5-thinking"

# ─── Инициализация ───

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

client_flash = AsyncOpenAI(api_key=FLASH_API_KEY, base_url=FLASH_BASE_URL)
client_pro = AsyncOpenAI(api_key=PRO_API_KEY, base_url=PRO_BASE_URL)

db_pool: asyncpg.Pool = None
user_last_request_time: dict[int, float] = {}

# ─── Словари расширений файлов ───

EXT_MAP = {
    'python': 'py', 'py': 'py', 'javascript': 'js', 'js': 'js', 'typescript': 'ts', 'ts': 'ts',
    'html': 'html', 'css': 'css', 'json': 'json', 'csv': 'csv', 'markdown': 'md', 'md': 'md',
    'txt': 'txt', 'cpp': 'cpp', 'c': 'c', 'cs': 'cs', 'java': 'java', 'go': 'go', 'rs': 'rs',
    'php': 'php', 'sql': 'sql', 'sh': 'sh', 'yaml': 'yaml', 'yml': 'yml'
}
CODE_EXTENSIONS = {'py', 'js', 'ts', 'html', 'css', 'json', 'cpp', 'c', 'cs', 'java', 'go', 'rs', 'php', 'sql', 'sh', 'yaml', 'yml'}
DEFAULT_FILENAMES = {'py': 'main.py', 'js': 'index.js', 'ts': 'index.ts', 'html': 'index.html', 'css': 'style.css', 'md': 'README.md', 'txt': 'doc.txt'}

# ─── База Данных ───

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
            balance_usd NUMERIC(10, 4) DEFAULT 0.0000,
            flash_requests INT DEFAULT 10,
            pro_requests INT DEFAULT 0,
            tier_id TEXT DEFAULT 'free',
            subscription_expires_at TIMESTAMP,
            is_banned BOOLEAN DEFAULT FALSE,
            is_blocked BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_activity TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            invoice_id TEXT,
            method TEXT,
            item_type TEXT,
            item_data JSONB,
            amount_usd NUMERIC(10, 4),
            amount_stars INT DEFAULT 0,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS chats (
            id SERIAL PRIMARY KEY,
            user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            chat_id INTEGER REFERENCES chats(id) ON DELETE CASCADE,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS active_sessions (
            user_id BIGINT PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            active_chat_id INTEGER
        );
        CREATE TABLE IF NOT EXISTS activity_logs (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
            content_type TEXT,
            model_used TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY,
            reward_usd NUMERIC(10, 4) DEFAULT 0.0000,
            max_activations INT DEFAULT 1,
            used_count INT DEFAULT 0,
            expires_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS promocode_activations (
            id SERIAL PRIMARY KEY,
            code TEXT REFERENCES promocodes(code) ON DELETE CASCADE,
            user_id BIGINT REFERENCES users(user_id) ON DELETE CASCADE,
            activated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(code, user_id)
        );
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

async def get_user_balance(user_id: int) -> float:
    async with db_pool.acquire() as conn:
        val = await conn.fetchval("SELECT balance_usd FROM users WHERE user_id = $1", user_id)
        return float(val or 0.0)

async def log_activity(user_id: int, content_type: str, model_used: str):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO activity_logs (user_id, content_type, model_used) VALUES ($1, $2, $3)", 
            user_id, content_type, model_used
        )

# ─── Криптобот API Клиент ───

async def create_cryptobot_invoice(amount_usd: float, description: str, payload_data: dict) -> dict:
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    payload = {
        "amount": f"{amount_usd:.2f}",
        "currency_type": "fiat",
        "fiat": "USD",
        "description": description,
        "payload": json.dumps(payload_data)
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            data = await resp.json()
            if data.get("ok"):
                return data["result"]
            raise Exception(f"CryptoBot Error: {data.get('error')}")

async def get_cryptobot_invoice_status(invoice_id: int) -> str:
    url = f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}"
    headers = {"Crypto-Pay-API-Token": CRYPTO_BOT_TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            if data.get("ok") and data["result"]["items"]:
                return data["result"]["items"][0]["status"]
            return "expired"

# ─── Начисление покупок и пополнений ───

async def deliver_purchase(user_id: int, item_type: str, item_data: dict):
    async with db_pool.acquire() as conn:
        if item_type == "deposit":
            amount = float(item_data.get("amount_usd", 0.0))
            await conn.execute("UPDATE users SET balance_usd = balance_usd + $1 WHERE user_id = $2", amount, user_id)
        elif item_type == "flash_requests":
            qty = item_data.get("quantity", 0)
            await conn.execute("UPDATE users SET flash_requests = flash_requests + $1 WHERE user_id = $2", qty, user_id)
        elif item_type == "pro_requests":
            qty = item_data.get("quantity", 0)
            await conn.execute("UPDATE users SET pro_requests = pro_requests + $1 WHERE user_id = $2", qty, user_id)
        elif item_type == "subscription":
            tier = item_data.get("tier_id")
            days = TIERS_PRICING[tier]["days"]
            exp = datetime.now() + timedelta(days=days)
            await conn.execute("UPDATE users SET tier_id = $1, subscription_expires_at = $2 WHERE user_id = $3", tier, exp, user_id)

# ─── Проверка лимитов и списание ───

async def check_and_deduct_access(user_id: int, requires_pro: bool) -> tuple[bool, str]:
    if user_id == ADMIN_ID:
        return True, ""

    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("""
            SELECT tier_id, subscription_expires_at, flash_requests, pro_requests 
            FROM users WHERE user_id = $1
        """, user_id)

        if not user:
            return False, "Пользователь не найден."

        tier_id = user["tier_id"]
        sub_exp = user["subscription_expires_at"]
        has_active_sub = sub_exp and sub_exp > datetime.now() and tier_id in TIERS_PRICING

        if has_active_sub:
            tier_info = TIERS_PRICING[tier_id]
            recent_reqs = await conn.fetchval("""
                SELECT count(*) FROM activity_logs 
                WHERE user_id = $1 AND created_at >= NOW() - INTERVAL '5 hours'
            """, user_id)

            if recent_reqs >= tier_info["limit_5h"]:
                return False, f"⏳ Достигнут лимит тарифа **{tier_info['name']}** ({recent_reqs}/{tier_info['limit_5h']} за 5ч). Подождите сброса окна."
            
            if requires_pro and tier_id == "standard":
                if user["pro_requests"] > 0:
                    await conn.execute("UPDATE users SET pro_requests = pro_requests - 1 WHERE user_id = $1", user_id)
                    return True, ""
                return False, "⚠️ Для Pro Thinking требуется подписка Pro / Ultra или пакет Pro-запросов ($0.120/шт)."
            return True, ""

        if requires_pro:
            if user["pro_requests"] > 0:
                await conn.execute("UPDATE users SET pro_requests = pro_requests - 1 WHERE user_id = $1", user_id)
                return True, ""
            return False, "❌ У вас закончились **Pro-запросы** ($0.120/шт). Пополните баланс в профиле или оформите подписку."
        else:
            if user["flash_requests"] > 0:
                await conn.execute("UPDATE users SET flash_requests = flash_requests - 1 WHERE user_id = $1", user_id)
                return True, ""
            return False, "❌ У вас закончились запросы ($0.050/шт). Пополните баланс в профиле или оформите подписку."

# ─── FSM Состояния ───

class ShopStates(StatesGroup):
    waiting_for_custom_requests = State()
    waiting_for_custom_deposit = State()

class ChatStates(StatesGroup):
    waiting_for_chat_rename = State()

class UserStates(StatesGroup):
    waiting_for_promo_code = State()

class AdminStates(StatesGroup):
    waiting_for_user_query = State()
    waiting_for_promo_reward = State()
    waiting_for_promo_activations = State()
    waiting_for_promo_duration = State()
    waiting_for_broadcast_target = State()
    waiting_for_broadcast_content = State()
    waiting_for_broadcast_buttons = State()

# ─── Клавиатуры ───

def get_main_reply_keyboard(is_admin: bool = False):
    keyboard = [
        [KeyboardButton(text="➕ Новый чат"), KeyboardButton(text="🖨️ История чатов")],
        [KeyboardButton(text="👤 Профиль"), KeyboardButton(text="💳 Магазин и Тарифы")]
    ]
    if is_admin:
        keyboard.append([KeyboardButton(text="⚡ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)

def get_profile_inline_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Пополнить баланс ($)", callback_data="profile_topup_menu")],
            [InlineKeyboardButton(text="🛍 Магазин тарифов и запросов", callback_data="shop_open_from_prof")],
            [InlineKeyboardButton(text="🎁 Ввести промокод", callback_data="profile_enter_promo")]
        ]
    )

def get_topup_amounts_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="+$1.00", callback_data="topup_amt:1.0"),
             InlineKeyboardButton(text="+$5.00", callback_data="topup_amt:5.0")],
            [InlineKeyboardButton(text="+$10.00", callback_data="topup_amt:10.0"),
             InlineKeyboardButton(text="+$25.00", callback_data="topup_amt:25.0")],
            [InlineKeyboardButton(text="✏️ Ввести другую сумму", callback_data="topup_amt_custom")],
            [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")]
        ]
    )

def get_topup_method_keyboard(amount_usd: float):
    stars_amount = max(1, int(round(amount_usd * STARS_RATE_PER_USD)))
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐️ Telegram Stars ({stars_amount} XTR)", callback_data=f"pay_topup_stars:{amount_usd}")],
            [InlineKeyboardButton(text=f"💎 CryptoBot (${amount_usd:.2f})", callback_data=f"pay_topup_crypto:{amount_usd}")],
            [InlineKeyboardButton(text="◀️ Назад к суммам", callback_data="profile_topup_menu")]
        ]
    )

def get_shop_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💎 Подписки (Standard / Pro / Ultra)", callback_data="shop_subs")],
            [InlineKeyboardButton(text="⚡ Купить Flash-запросы ($0.050/шт)", callback_data="shop_buy_req:flash")],
            [InlineKeyboardButton(text="🧠 Купить Pro-запросы ($0.120/шт)", callback_data="shop_buy_req:pro")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="shop_close")]
        ]
    )

def get_req_pack_keyboard(req_type: str):
    price = REQUEST_PRICES.get(req_type, PRICE_FLASH_USD)
    p8 = 8 * price
    p25 = 25 * price
    p100 = 100 * price
    p500 = 500 * price
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"8 запр. (${p8:.2f})", callback_data=f"buy_pack:{req_type}:8"),
             InlineKeyboardButton(text=f"25 запр. (${p25:.2f})", callback_data=f"buy_pack:{req_type}:25")],
            [InlineKeyboardButton(text=f"100 запр. (${p100:.2f})", callback_data=f"buy_pack:{req_type}:100"),
             InlineKeyboardButton(text=f"500 запр. (${p500:.2f})", callback_data=f"buy_pack:{req_type}:500")],
            [InlineKeyboardButton(text="✏️ Ввести другое количество (8-500)", callback_data=f"buy_custom:{req_type}")],
            [InlineKeyboardButton(text="◀️ Назад в магазин", callback_data="shop_back_main")]
        ]
    )

def get_payment_method_keyboard(item_type: str, raw_data: str, amount_usd: float, user_balance: float):
    stars_amount = max(1, int(round(amount_usd * STARS_RATE_PER_USD)))
    buttons = []
    
    # Кнопка оплаты с баланса
    if user_balance >= amount_usd:
        buttons.append([InlineKeyboardButton(text=f"💰 Оплатить с баланса (${amount_usd:.2f})", callback_data=f"pay_balance:{item_type}:{raw_data}")])
    else:
        buttons.append([InlineKeyboardButton(text=f"💰 С баланса (не хватает ${amount_usd - user_balance:.2f})", callback_data=f"pay_balance_fail:{amount_usd}")])

    buttons.append([InlineKeyboardButton(text=f"⭐️ Telegram Stars ({stars_amount} XTR)", callback_data=f"pay_stars:{item_type}:{raw_data}")])
    buttons.append([InlineKeyboardButton(text=f"💎 CryptoBot (${amount_usd:.2f})", callback_data=f"pay_crypto:{item_type}:{raw_data}")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад в магазин", callback_data="shop_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ─── Профиль Пользователя ───

async def generate_profile_text(user_id: int) -> str:
    async with db_pool.acquire() as conn:
        u = await conn.fetchrow("""
            SELECT balance_usd, flash_requests, pro_requests, tier_id, subscription_expires_at, created_at 
            FROM users WHERE user_id = $1
        """, user_id)
        chat_count = await conn.fetchval("SELECT count(*) FROM chats WHERE user_id = $1", user_id)
        req_count = await conn.fetchval("SELECT count(*) FROM activity_logs WHERE user_id = $1", user_id)
    
    tier_str = u["tier_id"].upper()
    if u["subscription_expires_at"] and u["subscription_expires_at"] > datetime.now():
        tier_str += f" (активна до {u['subscription_expires_at'].strftime('%d.%m.%Y')})"
    else:
        tier_str = "FREE"

    return (
        f"👤 **Ваш профиль в Evo Lumen 1.0**\n\n"
        f"🆔 **ID:** `{user_id}`\n"
        f"💰 **Баланс:** `${u['balance_usd']:.2f}` USD\n"
        f"💎 **Тариф:** `{tier_str}`\n\n"
        f"📦 **Доступные пакетные запросы:**\n"
        f" ├ ⚡ Flash ($0.050/шт): `{u['flash_requests']}` шт.\n"
        f" └ 🧠 Pro Thinking ($0.120/шт): `{u['pro_requests']}` шт.\n\n"
        f"💬 **Диалогов в базе:** {chat_count}\n"
        f"⚡ **Всего запросов:** {req_count}\n"
        f"📅 **Дата регистрации:** {u['created_at'].strftime('%Y-%m-%d')}"
    )

@dp.message(F.text == "👤 Профиль")
async def handle_user_profile(message: types.Message):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    text = await generate_profile_text(message.from_user.id)
    await message.answer(text, reply_markup=get_profile_inline_keyboard(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "back_to_profile")
async def handle_back_to_profile(call: CallbackQuery, state: FSMContext):
    await state.clear()
    text = await generate_profile_text(call.from_user.id)
    await call.message.edit_text(text, reply_markup=get_profile_inline_keyboard(), parse_mode=ParseMode.MARKDOWN)

# ─── Пополнение Баланса ───

@dp.callback_query(F.data == "profile_topup_menu")
async def handle_profile_topup_menu(call: CallbackQuery):
    await call.message.edit_text(
        "💵 **Пополнение баланса аккаунта**\n\n"
        "Выберите сумму пополнения в долларах ($) или укажите свое значение:",
        reply_markup=get_topup_amounts_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data.startswith("topup_amt:"))
async def handle_topup_amt_preset(call: CallbackQuery):
    amount_usd = float(call.data.split(":")[1])
    await call.message.edit_text(
        f"💳 **Пополнение баланса на сумму: ${amount_usd:.2f} USD**\n\n"
        f"Выберите удобный способ оплаты:",
        reply_markup=get_topup_method_keyboard(amount_usd),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data == "topup_amt_custom")
async def handle_topup_amt_custom_prompt(call: CallbackQuery, state: FSMContext):
    await state.set_state(ShopStates.waiting_for_custom_deposit)
    await call.message.edit_text(
        "✏️ Введите желаемую сумму пополнения в $ (от `0.50` до `500.00`):\n*Например:* `7.50`",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(StateFilter(ShopStates.waiting_for_custom_deposit))
async def handle_topup_amt_custom_input(message: types.Message, state: FSMContext):
    try:
        amount_usd = float(message.text.strip().replace(",", "."))
        if amount_usd < 0.50 or amount_usd > 500.00:
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Введите корректное число от 0.50 до 500.00:")
        return

    await state.clear()
    await message.answer(
        f"💳 **Пополнение баланса на сумму: ${amount_usd:.2f} USD**\n\n"
        f"Выберите метод оплаты:",
        reply_markup=get_topup_method_keyboard(amount_usd),
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data.startswith("pay_topup_stars:"))
async def handle_pay_topup_stars(call: CallbackQuery):
    amount_usd = float(call.data.split(":")[1])
    stars = max(1, int(round(amount_usd * STARS_RATE_PER_USD)))
    title = f"Пополнение баланса (${amount_usd:.2f})"
    desc = f"Зачисление ${amount_usd:.2f} USD на внутренний баланс бота"
    payload_data = {"item_type": "deposit", "amount_usd": amount_usd}

    prices = [LabeledPrice(label=title, amount=stars)]
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description=desc,
        payload=json.dumps(payload_data),
        currency="XTR",
        prices=prices,
        provider_token=""
    )
    await call.answer()

@dp.callback_query(F.data.startswith("pay_topup_crypto:"))
async def handle_pay_topup_crypto(call: CallbackQuery):
    amount_usd = float(call.data.split(":")[1])
    title = f"Пополнение баланса (${amount_usd:.2f})"
    payload_data = {"user_id": call.from_user.id, "item_type": "deposit", "amount_usd": amount_usd}

    try:
        invoice = await create_cryptobot_invoice(amount_usd, title, payload_data)
        invoice_id = invoice["invoice_id"]
        pay_url = invoice["bot_invoice_url"]

        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO payments (user_id, invoice_id, method, item_type, item_data, amount_usd, status)
                VALUES ($1, $2, 'crypto_bot', 'deposit', $3, $4, 'pending')
            """, call.from_user.id, str(invoice_id), json.dumps(payload_data), amount_usd)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💎 Оплатить в CryptoBot", url=pay_url)],
                [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_crypto:{invoice_id}")],
                [InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="back_to_profile")]
            ]
        )
        await call.message.edit_text(
            f"🧾 **Счет на пополнение баланса сформирован!**\n\n"
            f"Сумма к оплате: **${amount_usd:.2f} USD**\n\n"
            f"Оплатите счет в CryptoBot и нажмите **«Проверить оплату»**.",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await call.message.answer(f"⚠️ Ошибка создания чека: {str(e)}")

# ─── Промокоды ───

@dp.callback_query(F.data == "profile_enter_promo")
async def handle_profile_promo_click(call: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_promo_code)
    await call.message.answer("🎁 Введите промокод для начисления бонуса на ваш $ баланс:")
    await call.answer()

@dp.message(F.text == "🎁 Промокод")
async def handle_enter_promo_btn(message: types.Message, state: FSMContext):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    await state.set_state(UserStates.waiting_for_promo_code)
    await message.answer("🎁 Введите промокод для начисления бонуса на ваш $ баланс:")

@dp.message(StateFilter(UserStates.waiting_for_promo_code))
async def handle_promo_activation(message: types.Message, state: FSMContext):
    code_text = message.text.strip().upper()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        promo = await conn.fetchrow("SELECT * FROM promocodes WHERE code = $1", code_text)
        if not promo:
            await message.answer("❌ Промокод не существует.")
            await state.clear()
            return

        if promo["expires_at"] and promo["expires_at"] < datetime.now():
            await message.answer("⌛ Срок действия промокода истек.")
            await state.clear()
            return

        if promo["used_count"] >= promo["max_activations"]:
            await message.answer("🚫 Лимит активаций промокода исчерпан.")
            await state.clear()
            return

        used = await conn.fetchval("SELECT 1 FROM promocode_activations WHERE code = $1 AND user_id = $2", code_text, user_id)
        if used:
            await message.answer("⚠️ Вы уже активировали этот промокод.")
            await state.clear()
            return

        async with conn.transaction():
            await conn.execute("INSERT INTO promocode_activations (code, user_id) VALUES ($1, $2)", code_text, user_id)
            await conn.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code = $1", code_text)
            await conn.execute("UPDATE users SET balance_usd = balance_usd + $1 WHERE user_id = $2", promo["reward_usd"], user_id)

    await message.answer(
        f"🎉 Промокод активирован! Начислено: **+${promo['reward_usd']:.2f} USD** на ваш баланс.",
        reply_markup=get_main_reply_keyboard(user_id == ADMIN_ID),
        parse_mode=ParseMode.MARKDOWN
    )
    await state.clear()

# ─── Магазин и Оплата с Баланса / Stars / CryptoBot ───

@dp.message(F.text == "💳 Магазин и Тарифы")
@dp.callback_query(F.data == "shop_open_from_prof")
async def cmd_shop(event: types.Message | CallbackQuery):
    user_id = event.from_user.id
    if await is_user_banned(user_id):
        return
    await track_user(event.from_user)
    
    bal = await get_user_balance(user_id)
    shop_text = (
        f"🛍 **Магазин сервиса Evo Lumen 1.0**\n"
        f"💰 Ваш текущий баланс: **${bal:.2f} USD**\n\n"
        f"• **Подписки:** безлимитные 5ч окна, сниженный кулдаун и Pro Thinking.\n"
        f"• **Flash-запросы:** $0.050 / шт. (быстрые ответы)\n"
        f"• **Pro-запросы:** $0.120 / шт. (глубокий аудит)"
    )
    if isinstance(event, types.Message):
        await event.answer(shop_text, reply_markup=get_shop_main_keyboard(), parse_mode=ParseMode.MARKDOWN)
    else:
        await event.message.edit_text(shop_text, reply_markup=get_shop_main_keyboard(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "shop_close")
async def handle_shop_close(call: CallbackQuery):
    await call.message.delete()

@dp.callback_query(F.data == "shop_back_main")
async def handle_shop_back_main(call: CallbackQuery, state: FSMContext):
    await state.clear()
    bal = await get_user_balance(call.from_user.id)
    shop_text = (
        f"🛍 **Магазин сервиса Evo Lumen 1.0**\n"
        f"💰 Ваш баланс: **${bal:.2f} USD**\n\n"
        f"Выберите интересующий раздел:"
    )
    await call.message.edit_text(shop_text, reply_markup=get_shop_main_keyboard(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "shop_subs")
async def handle_shop_subs(call: CallbackQuery):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Standard — $4.99 (250 ⭐️)", callback_data="buy_sub:standard")],
            [InlineKeyboardButton(text="Pro (Thinking) — $14.99 (750 ⭐️)", callback_data="buy_sub:pro")],
            [InlineKeyboardButton(text="Ultra (Dev) — $29.99 (1500 ⭐️)", callback_data="buy_sub:ultra")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="shop_back_main")]
        ]
    )
    text = (
        "💎 **Тарифные планы подписок на 30 дней:**\n\n"
        "1️⃣ **Standard ($4.99 / 250 ⭐️):** 40 запр./5ч, кулдаун 7 сек, файлы до 20 МБ.\n"
        "2️⃣ **Pro ($14.99 / 750 ⭐️):** 100 запр./5ч, кулдаун 2 сек, полный Pro Thinking аудит.\n"
        "3️⃣ **Ultra ($29.99 / 1500 ⭐️):** 250 запр./5ч, 0 сек кулдаун, максимальный приоритет."
    )
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("buy_sub:"))
async def handle_choose_sub_payment(call: CallbackQuery):
    tier = call.data.split(":")[1]
    info = TIERS_PRICING[tier]
    bal = await get_user_balance(call.from_user.id)
    kb = get_payment_method_keyboard("subscription", tier, info["price_usd"], bal)
    await call.message.edit_text(
        f"💳 **Оплата подписки:** {info['name']}\n"
        f"Стоимость: **${info['price_usd']:.2f} USD** (или **{info['stars']} ⭐️**)\n"
        f"💰 Ваш баланс: **${bal:.2f} USD**\n\n"
        f"Выберите источник оплаты:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data.startswith("shop_buy_req:"))
async def handle_shop_buy_req(call: CallbackQuery):
    req_type = call.data.split(":")[1]
    price = REQUEST_PRICES.get(req_type, PRICE_FLASH_USD)
    title = "⚡ Flash-запросов" if req_type == "flash" else "🧠 Pro-запросов"
    text = (
        f"📦 **Покупка пакета {title}**\n"
        f"Стоимость за 1 запрос: **${price:.3f}**\n"
        f"Минимум: 8 запросов | Максимум: 500 запросов.\n\n"
        f"Выберите объем пакета:"
    )
    await call.message.edit_text(text, reply_markup=get_req_pack_keyboard(req_type), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("buy_pack:"))
async def handle_buy_pack_preset(call: CallbackQuery):
    _, req_type, qty_str = call.data.split(":")
    qty = int(qty_str)
    price = REQUEST_PRICES.get(req_type, PRICE_FLASH_USD)
    cost_usd = qty * price
    bal = await get_user_balance(call.from_user.id)
    kb = get_payment_method_keyboard(f"{req_type}_requests", str(qty), cost_usd, bal)
    await call.message.edit_text(
        f"💳 **Покупка:** {qty} {req_type.upper()}-запросов\n"
        f"Сумма: **${cost_usd:.2f} USD**\n"
        f"💰 Ваш баланс: **${bal:.2f} USD**\n\n"
        f"Выберите источник оплаты:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )

@dp.callback_query(F.data.startswith("buy_custom:"))
async def handle_buy_custom_prompt(call: CallbackQuery, state: FSMContext):
    req_type = call.data.split(":")[1]
    await state.set_state(ShopStates.waiting_for_custom_requests)
    await state.update_data(req_type=req_type)
    await call.message.edit_text(
        f"✏️ Введите желаемое количество ({req_type.upper()}) от **8** до **500** числом:\n(например: `45`)",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(StateFilter(ShopStates.waiting_for_custom_requests))
async def handle_buy_custom_input(message: types.Message, state: FSMContext):
    try:
        qty = int(message.text.strip())
        if qty < 8 or qty > 500:
            raise ValueError()
    except ValueError:
        await message.answer("⚠️ Введите целое число в диапазоне от **8** до **500**.")
        return

    data = await state.get_data()
    req_type = data["req_type"]
    price = REQUEST_PRICES.get(req_type, PRICE_FLASH_USD)
    cost_usd = qty * price
    bal = await get_user_balance(message.from_user.id)
    kb = get_payment_method_keyboard(f"{req_type}_requests", str(qty), cost_usd, bal)
    
    await message.answer(
        f"💳 **Оформление заказа:** {qty} {req_type.upper()}-запросов\n"
        f"Сумма: **${cost_usd:.2f} USD**\n"
        f"💰 Ваш баланс: **${bal:.2f} USD**\n\n"
        f"Выберите способ оплаты:",
        reply_markup=kb,
        parse_mode=ParseMode.MARKDOWN
    )
    await state.clear()

# ─── Покупка с Внутреннего Баланса ───

@dp.callback_query(F.data.startswith("pay_balance_fail:"))
async def handle_pay_balance_fail(call: CallbackQuery):
    await call.answer("❌ Недостаточно средств на балансе. Пополните баланс в профиле!", show_alert=True)

@dp.callback_query(F.data.startswith("pay_balance:"))
async def handle_pay_balance_exec(call: CallbackQuery):
    _, item_type, raw_data = call.data.split(":")
    user_id = call.from_user.id

    if item_type == "subscription":
        tier_info = TIERS_PRICING[raw_data]
        cost_usd = tier_info["price_usd"]
        payload_data = {"tier_id": raw_data}
        success_text = f"🎉 Подписка **{tier_info['name']}** успешно оформлена на 30 дней!"
    else:
        qty = int(raw_data)
        req_type = item_type.split("_")[0]
        cost_usd = qty * REQUEST_PRICES.get(req_type, PRICE_FLASH_USD)
        payload_data = {"quantity": qty}
        success_text = f"🎉 Пакет из **{qty} {req_type.upper()} запросов** успешно начислен!"

    async with db_pool.acquire() as conn:
        current_bal = await conn.fetchval("SELECT balance_usd FROM users WHERE user_id = $1", user_id)
        if current_bal < cost_usd:
            await call.answer("❌ Недостаточно средств на балансе!", show_alert=True)
            return

        async with conn.transaction():
            await conn.execute("UPDATE users SET balance_usd = balance_usd - $1 WHERE user_id = $2", cost_usd, user_id)
            await deliver_purchase(user_id, item_type, payload_data)
            await conn.execute("""
                INSERT INTO payments (user_id, method, item_type, item_data, amount_usd, status)
                VALUES ($1, 'internal_balance', $2, $3, $4, 'paid')
            """, user_id, item_type, json.dumps(payload_data), cost_usd)

    await call.message.edit_text(
        f"{success_text}\n\nС вашего баланса списано: **${cost_usd:.2f} USD**.",
        parse_mode=ParseMode.MARKDOWN
    )

# ─── Обработка Stars & CryptoBot ───

@dp.callback_query(F.data.startswith("pay_stars:"))
async def handle_pay_stars(call: CallbackQuery):
    _, item_type, raw_data = call.data.split(":")
    
    if item_type == "subscription":
        tier_info = TIERS_PRICING[raw_data]
        stars = tier_info["stars"]
        title = f"Подписка {tier_info['name']}"
        desc = f"Активация подписки на 30 дней в Evo Lumen"
        payload_data = {"item_type": "subscription", "tier_id": raw_data}
    else:
        qty = int(raw_data)
        req_type = item_type.split("_")[0]
        price = REQUEST_PRICES.get(req_type, PRICE_FLASH_USD)
        amount_usd = qty * price
        stars = max(1, int(round(amount_usd * STARS_RATE_PER_USD)))
        title = f"{qty} {req_type.upper()} запросов"
        desc = f"Пакет запросов для модели {req_type.upper()}"
        payload_data = {"item_type": item_type, "quantity": qty}

    prices = [LabeledPrice(label=title, amount=stars)]
    await bot.send_invoice(
        chat_id=call.from_user.id,
        title=title,
        description=desc,
        payload=json.dumps(payload_data),
        currency="XTR",
        prices=prices,
        provider_token=""
    )
    await call.answer()

@dp.pre_checkout_query()
async def process_pre_checkout_query(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: types.Message):
    sp = message.successful_payment
    payload = json.loads(sp.invoice_payload)
    item_type = payload["item_type"]

    await deliver_purchase(message.from_user.id, item_type, payload)

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments (user_id, invoice_id, method, item_type, item_data, amount_stars, status)
            VALUES ($1, $2, 'telegram_stars', $3, $4, $5, 'paid')
        """, message.from_user.id, sp.telegram_payment_charge_id, item_type, json.dumps(payload), sp.total_amount)

    await message.answer("🎉 **Оплата успешно завершена через Telegram Stars!** Баланс обновлен.", parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("pay_crypto:"))
async def handle_pay_crypto(call: CallbackQuery):
    _, item_type, raw_data = call.data.split(":")
    
    if item_type == "subscription":
        tier_info = TIERS_PRICING[raw_data]
        cost_usd = tier_info["price_usd"]
        title = f"Подписка {tier_info['name']}"
        payload_data = {"user_id": call.from_user.id, "item_type": "subscription", "tier_id": raw_data}
    else:
        qty = int(raw_data)
        req_type = item_type.split("_")[0]
        price = REQUEST_PRICES.get(req_type, PRICE_FLASH_USD)
        cost_usd = qty * price
        title = f"{qty} запросов ({req_type.upper()})"
        payload_data = {"user_id": call.from_user.id, "item_type": item_type, "quantity": qty}

    try:
        invoice = await create_cryptobot_invoice(cost_usd, title, payload_data)
        invoice_id = invoice["invoice_id"]
        pay_url = invoice["bot_invoice_url"]

        async with db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO payments (user_id, invoice_id, method, item_type, item_data, amount_usd, status)
                VALUES ($1, $2, 'crypto_bot', $3, $4, $5, 'pending')
            """, call.from_user.id, str(invoice_id), item_type, json.dumps(payload_data), cost_usd)

        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💎 Оплатить чек в CryptoBot", url=pay_url)],
                [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"check_crypto:{invoice_id}")],
                [InlineKeyboardButton(text="◀️ Назад в магазин", callback_data="shop_back_main")]
            ]
        )
        await call.message.edit_text(
            f"🧾 **Счет на оплату сформирован!**\n\n"
            f"Товар: **{title}**\n"
            f"Сумма: **${cost_usd:.2f} USD**\n\n"
            f"Оплатите чек в CryptoBot и нажмите **«Проверить оплату»**.",
            reply_markup=kb,
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        await call.message.answer(f"⚠️ Ошибка создания чека: {str(e)}")

@dp.callback_query(F.data.startswith("check_crypto:"))
async def handle_check_crypto(call: CallbackQuery):
    invoice_id = int(call.data.split(":")[1])
    status = await get_cryptobot_invoice_status(invoice_id)
    
    if status == "paid":
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM payments WHERE invoice_id = $1", str(invoice_id))
            if row and row["status"] != "paid":
                payload = json.loads(row["item_data"])
                await deliver_purchase(row["user_id"], row["item_type"], payload)
                await conn.execute("UPDATE payments SET status = 'paid' WHERE invoice_id = $1", str(invoice_id))
        
        await call.message.edit_text("🎉 **Оплата успешно подтверждена!** Баланс / услуги начислены.", parse_mode=ParseMode.MARKDOWN)
    elif status == "active":
        await call.answer("⏳ Чек еще не оплачен. Оплатите счет в боте и повторите проверку.", show_alert=True)
    else:
        await call.answer("❌ Время действия чека истекло или он был отменен.", show_alert=True)

# ─── Админ-панель (ID: 5480751648) ───

def get_admin_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Аудитория", callback_data="admin_metrics_audience"),
             InlineKeyboardButton(text="📈 Активность", callback_data="admin_metrics_activity")],
            [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast_start"),
             InlineKeyboardButton(text="🔍 CRM Поиск", callback_data="admin_user_search")],
            [InlineKeyboardButton(text="🎁 Генератор промокодов", callback_data="admin_create_promo")],
            [InlineKeyboardButton(text="❌ Закрыть", callback_data="admin_close")]
        ]
    )

@dp.message(Command("admin"))
@dp.message(F.text == "⚡ Админ панель")
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 **Панель администратора**", reply_markup=get_admin_main_keyboard(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "admin_close")
async def handle_admin_close_btn(call: CallbackQuery):
    if call.from_user.id == ADMIN_ID:
        await call.message.delete()

@dp.callback_query(F.data == "admin_metrics_audience")
async def handle_admin_metrics_audience(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        total = await conn.fetchval("SELECT count(*) FROM users")
        new_24h = await conn.fetchval("SELECT count(*) FROM users WHERE created_at >= NOW() - INTERVAL '24 hours'")
        new_7d = await conn.fetchval("SELECT count(*) FROM users WHERE created_at >= NOW() - INTERVAL '7 days'")
        dau = await conn.fetchval("SELECT count(*) FROM users WHERE last_activity >= NOW() - INTERVAL '24 hours'")
        banned = await conn.fetchval("SELECT count(*) FROM users WHERE is_banned = TRUE")
        blocked = await conn.fetchval("SELECT count(*) FROM users WHERE is_blocked = TRUE")

    text = (
        "📊 **Метрики аудитории:**\n\n"
        f"👥 Всего: `{total}` | 🟢 DAU: `{dau}`\n"
        f"📈 Новые: +`{new_24h}` (24ч), +`{new_7d}` (7д)\n"
        f"🚫 Забанены: `{banned}` | 🔕 Бот заблокирован: `{blocked}`"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back_main")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "admin_metrics_activity")
async def handle_admin_metrics_activity(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return
    async with db_pool.acquire() as conn:
        total_reqs = await conn.fetchval("SELECT count(*) FROM activity_logs")
        rows = await conn.fetch("SELECT content_type, count(*) as cnt FROM activity_logs GROUP BY content_type")
        types_map = {r["content_type"]: r["cnt"] for r in rows}

    text = (
        "📈 **Метрики активности:**\n\n"
        f"⚡ Всего запросов: `{total_reqs}`\n"
        f"📝 Текст: `{types_map.get('text', 0)}` | 🖼️ Фото: `{types_map.get('photo', 0)}`\n"
        f"📄 Документы: `{types_map.get('document', 0)}` | 🎙️ Аудио: `{types_map.get('audio', 0)}`"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back_main")]])
    await call.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "admin_back_main")
async def handle_admin_back_main(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await call.message.edit_text("🛠 **Панель администратора**", reply_markup=get_admin_main_keyboard(), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data == "admin_create_promo")
async def handle_admin_create_promo_step1(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_for_promo_reward)
    await call.message.edit_text("🎁 **Шаг 1/3:** Введите сумму бонуса в долларах ($) (например: `1.50`):")

@dp.message(StateFilter(AdminStates.waiting_for_promo_reward))
async def handle_admin_create_promo_step2(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        reward = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Введите число:")
        return
    await state.update_data(promo_reward=reward)
    await state.set_state(AdminStates.waiting_for_promo_activations)
    await message.answer("👥 **Шаг 2/3:** Максимальное число активаций:")

@dp.message(StateFilter(AdminStates.waiting_for_promo_activations))
async def handle_admin_create_promo_step3(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        activations = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введите целое число:")
        return
    await state.update_data(promo_activations=activations)
    await state.set_state(AdminStates.waiting_for_promo_duration)
    await message.answer("⏱ **Шаг 3/3:** Время жизни в минутах (0 — бессрочно):")

@dp.message(StateFilter(AdminStates.waiting_for_promo_duration))
async def handle_admin_create_promo_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        duration = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введите число минут:")
        return

    data = await state.get_data()
    code = f"EVO-{secrets.token_hex(3).upper()}"
    exp = datetime.now() + timedelta(minutes=duration) if duration > 0 else None

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO promocodes (code, reward_usd, max_activations, expires_at)
            VALUES ($1, $2, $3, $4)
        """, code, data["promo_reward"], data["promo_activations"], exp)

    await message.answer(
        f"✅ **Промокод создан:** `{code}`\n"
        f"💰 Бонус: `${data['promo_reward']:.2f} USD`\n"
        f"👥 Активаций: `{data['promo_activations']}`\n"
        f"⌛ Истекает: `{exp.strftime('%Y-%m-%d %H:%M') if exp else 'Бессрочно'}`",
        parse_mode=ParseMode.MARKDOWN
    )
    await state.clear()

# ─── Управление чатами и Экспорт ───

async def get_or_create_active_chat(user_id: int) -> int:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT active_chat_id FROM active_sessions WHERE user_id = $1", user_id)
        if row and row["active_chat_id"]:
            return row["active_chat_id"]
        chat_id = await conn.fetchval("INSERT INTO chats (user_id, title) VALUES ($1, $2) RETURNING id", user_id, "Основной диалог")
        await conn.execute("INSERT INTO active_sessions (user_id, active_chat_id) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET active_chat_id = $2", user_id, chat_id)
        return chat_id

async def set_active_chat(user_id: int, chat_id: int):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO active_sessions (user_id, active_chat_id) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET active_chat_id = $2", user_id, chat_id)

async def get_user_chats(user_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, title FROM chats WHERE user_id = $1 ORDER BY id DESC", user_id)
        return [(r["id"], r["title"]) for r in rows]

async def get_chat_title(chat_id: int):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT title FROM chats WHERE id = $1", chat_id)
        return row["title"] if row else "Без названия"

async def save_message(chat_id: int, role: str, content: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO messages (chat_id, role, content) VALUES ($1, $2, $3)", chat_id, role, content)

async def get_chat_messages(chat_id: int, limit: int = 8):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT role, content FROM messages WHERE chat_id = $1 ORDER BY id DESC LIMIT $2", chat_id, limit)
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

async def get_all_chat_messages(chat_id: int):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT role, content, created_at FROM messages WHERE chat_id = $1 ORDER BY id ASC", chat_id)
        return [{"role": r["role"], "content": r["content"], "created_at": r["created_at"]} for r in rows]

def get_chat_actions_keyboard(chat_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить диалог", callback_data=f"chat_use:{chat_id}")],
            [InlineKeyboardButton(text="📥 Экспорт .md", callback_data=f"chat_exp_md:{chat_id}"),
             InlineKeyboardButton(text="📥 Экспорт .txt", callback_data=f"chat_exp_txt:{chat_id}")],
            [InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"chat_delete:{chat_id}"),
             InlineKeyboardButton(text="◀️ Назад", callback_data="chat_list_back")]
        ]
    )

@dp.message(F.text == "➕ Новый чат")
async def handle_new_chat(message: types.Message):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    async with db_pool.acquire() as conn:
        new_id = await conn.fetchval("INSERT INTO chats (user_id, title) VALUES ($1, $2) RETURNING id", message.from_user.id, "Новый диалог")
        await conn.execute("INSERT INTO active_sessions (user_id, active_chat_id) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET active_chat_id = $2", message.from_user.id, new_id)
    await message.answer(f"🆕 Создан чат **№{new_id}**. Начните диалог!", parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "🖨️ История чатов")
async def handle_history_menu(message: types.Message):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    chats = await get_user_chats(message.from_user.id)
    if not chats:
        await message.answer("У вас пока нет чатов.")
        return
    kb = [[InlineKeyboardButton(text=f"💬 {title} (ID: {cid})", callback_data=f"chat_manage:{cid}")] for cid, title in chats]
    await message.answer("🖨️ **Ваши диалоги:**", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("chat_manage:"))
async def handle_chat_manage(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    title = await get_chat_title(cid)
    await call.message.edit_text(f"⚙️ Диалог: *{title}* (ID: `{cid}`)", reply_markup=get_chat_actions_keyboard(cid), parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("chat_use:"))
async def handle_chat_use(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    await set_active_chat(call.from_user.id, cid)
    await call.message.edit_text("✅ Активный чат переключен! Можете продолжать общение.", parse_mode=ParseMode.MARKDOWN)

@dp.callback_query(F.data.startswith("chat_exp_md:"))
async def handle_export_md(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    msgs = await get_all_chat_messages(cid)
    content = f"# Экспорт диалога #{cid}\n\n" + "\n\n".join([f"**{m['role'].upper()}**: {m['content']}" for m in msgs])
    file = BufferedInputFile(content.encode("utf-8"), filename=f"dialog_{cid}.md")
    await call.message.answer_document(file, caption=f"📥 Чат #{cid} в формате Markdown")
    await call.answer()

@dp.callback_query(F.data.startswith("chat_exp_txt:"))
async def handle_export_txt(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    msgs = await get_all_chat_messages(cid)
    content = f"ЭКСПОРТ ДИАЛОГА #{cid}\n\n" + "\n\n".join([f"[{m['role'].upper()}]: {m['content']}" for m in msgs])
    file = BufferedInputFile(content.encode("utf-8"), filename=f"dialog_{cid}.txt")
    await call.message.answer_document(file, caption=f"📥 Чат #{cid} в формате TXT")
    await call.answer()

@dp.callback_query(F.data.startswith("chat_delete:"))
async def handle_chat_del(call: CallbackQuery):
    cid = int(call.data.split(":")[1])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM chats WHERE id = $1", cid)
    await call.answer("Чат удален!", show_alert=True)
    await handle_history_menu(call.message)

# ─── Обработка файлов и статуса ───

def detect_file_request(user_prompt: str, ai_text: str) -> tuple[bool, str, str]:
    prompt_lower = user_prompt.lower()
    match = re.search(r'\b([a-zA-Z0-9_\-]+\.([a-zA-Z0-9]+))\b', user_prompt)
    has_intent = any(w in prompt_lower for w in ['файл', 'файлом', 'скинь', 'выгрузи', 'сохрани', 'напиши'])
    if match and (match.group(2).lower() in EXT_MAP or has_intent):
        ext = EXT_MAP.get(match.group(2).lower(), match.group(2).lower())
        return True, match.group(1), ext
    if not has_intent:
        return False, "", ""
    return True, "main.py", "py"

def extract_file_content(ai_text: str, ext: str) -> str:
    blocks = re.findall(r'```(?:[a-zA-Z0-9_+#\-\.]+)?\n([\s\S]*?)```', ai_text)
    if ext in CODE_EXTENSIONS and blocks:
        return "\n\n".join(b.strip() for b in blocks)
    return ai_text.strip()

async def send_response(message: types.Message, text: str, user_prompt: str = ""):
    wants_file, filename, ext = detect_file_request(user_prompt, text)
    if wants_file:
        content = extract_file_content(text, ext)
        if content:
            await message.answer_document(BufferedInputFile(content.encode("utf-8"), filename=filename), caption=f"📄 Файл `{filename}`:")

    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)] if len(text) > 4000 else [text]
    for chunk in chunks:
        try:
            await message.answer(chunk, parse_mode=ParseMode.MARKDOWN)
        except TelegramBadRequest:
            await message.answer(chunk)

class StatusUpdater:
    def __init__(self, message: types.Message):
        self.message = message
        self.start_time = time.time()
        self.stage = "⚡ Evo Lumen 1.0 думает..."
        self.is_running = True
        self.task = None

    async def _loop(self):
        while self.is_running:
            sec = int(time.time() - self.start_time)
            try:
                await self.message.edit_text(f"{self.stage}\n⏱ *{sec} сек.*", parse_mode=ParseMode.MARKDOWN)
            except Exception:
                pass
            await asyncio.sleep(1.5)

    def start(self):
        self.task = asyncio.create_task(self._loop())

    def set_stage(self, stage: str):
        self.stage = stage

    async def stop(self):
        self.is_running = False
        if self.task:
            self.task.cancel()

# ─── Запуск и Главный Обработчик ───

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    if await is_user_banned(message.from_user.id):
        await message.answer("⛔ Доступ заблокирован.")
        return
    await track_user(message.from_user)
    await get_or_create_active_chat(message.from_user.id)
    text = (
        "👋 Здравствуйте! Я **Evo Lumen 1.0** — ваш ИИ-ассистент.\n\n"
        "💳 **Оплата и баланс:**\n"
        "• Баланс отображается в **$ USD** в вашем профиле.\n"
        "• Доступны подписки Standard / Pro / Ultra и покупка запросов.\n"
        "• Оплата напрямую с баланса, через Stars ⭐️ или CryptoBot 💎"
    )
    await message.answer(text, reply_markup=get_main_reply_keyboard(message.from_user.id == ADMIN_ID), parse_mode=ParseMode.MARKDOWN)

@dp.message()
async def handle_message_prompt(message: types.Message):
    user_id = message.from_user.id
    if await is_user_banned(user_id):
        return

    await track_user(message.from_user)
    prompt_text = message.caption or message.text or ""
    if not prompt_text and not message.photo and not message.document:
        return

    now = time.time()
    last_time = user_last_request_time.get(user_id, 0)
    if user_id != ADMIN_ID and (now - last_time) < RATE_LIMIT_SECONDS:
        rem = int(RATE_LIMIT_SECONDS - (now - last_time))
        await message.answer(f"⏳ Подождите `{rem}` сек. перед следующим запросом.")
        return

    requires_pro = any(w in prompt_lower for w in ['напиши код', 'архитектура', 'проанализируй код', 'рефакторинг', 'debug']) if (prompt_lower := prompt_text.lower()) else False

    has_access, err_msg = await check_and_deduct_access(user_id, requires_pro)
    if not has_access:
        await message.answer(err_msg, parse_mode=ParseMode.MARKDOWN)
        return

    user_last_request_time[user_id] = now
    await log_activity(user_id, "text" if prompt_text else "media", MODEL_PRO if requires_pro else MODEL_FLASH)
    active_chat_id = await get_or_create_active_chat(user_id)
    await save_message(active_chat_id, "user", prompt_text)
    history = await get_chat_messages(active_chat_id, limit=8)

    status_msg = await message.answer("⚡ *Evo Lumen 1.0* формирует ответ...\n⏱ *0 сек.*", parse_mode=ParseMode.MARKDOWN)
    updater = StatusUpdater(status_msg)
    updater.start()

    try:
        messages_payload = [{"role": h["role"], "content": h["content"]} for h in history[:-1]]
        messages_payload.append({"role": "user", "content": prompt_text})

        if requires_pro:
            updater.set_stage("🧠 *Evo Lumen 1.0 (Pro Thinking)* проводит глубокий аудит...")
            res = await client_pro.chat.completions.create(model=MODEL_PRO, messages=messages_payload, temperature=0.1)
        else:
            res = await client_flash.chat.completions.create(model=MODEL_FLASH, messages=messages_payload, temperature=0.2)
        
        final_answer = res.choices[0].message.content.strip()
        await save_message(active_chat_id, "assistant", final_answer)
    except Exception as e:
        final_answer = f"⚠️ Ошибка генерации ответа: `{str(e)}`"
    finally:
        await updater.stop()
        try:
            await status_msg.delete()
        except Exception:
            pass

    await send_response(message, final_answer, user_prompt=prompt_text)

# ─── Точка входа ───

async def main():
    await init_db()
    print(f"🚀 Evo Lumen запущен. Баланс в USD. Админ: {ADMIN_ID}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
