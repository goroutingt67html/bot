import os
import io
import re
import time
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
from aiogram.enums import ParseMode, ChatMemberStatus, ContentType
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
    BufferedInputFile
)
from openai import AsyncOpenAI

load_dotenv()

# ─── Конфигурация токенов и базы ───

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
FLASH_API_KEY = os.getenv("FLASH_API_KEY", "")
PRO_API_KEY = os.getenv("PRO_API_KEY", "")

RAW_DB_URL = os.getenv("DATABASE_URL", "")
DATABASE_URL = RAW_DB_URL.replace("postgres://", "postgresql://") if RAW_DB_URL else ""

ADMIN_ID = 5480751648
RATE_LIMIT_SECONDS = 15

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

async def log_activity(user_id: int, content_type: str):
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO activity_logs (user_id, content_type) VALUES ($1, $2)", user_id, content_type)

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

class AdminStates(StatesGroup):
    waiting_for_user_query = State()
    waiting_for_promo_reward = State()
    waiting_for_promo_activations = State()
    waiting_for_promo_duration = State()
    waiting_for_broadcast_target = State()
    waiting_for_broadcast_content = State()
    waiting_for_broadcast_buttons = State()

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
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🎁 Промокод")]
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
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin_broadcast_start"),
             InlineKeyboardButton(text="🔍 Поиск пользователя", callback_data="admin_user_search")],
            [InlineKeyboardButton(text="🎁 Создать промокод", callback_data="admin_create_promo")],
            [InlineKeyboardButton(text="❌ Закрыть панель", callback_data="admin_close")]
        ]
    )

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
    await call.message.edit_text(f"✅ Активный диалог переключен на: *{title}*.\nВы можете продолжить общение!", parse_mode=ParseMode.MARKDOWN)

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

# ─── Профиль и Промокоды (Пользователь) ───

@dp.message(F.text == "👤 Мой профиль")
async def handle_user_profile(message: types.Message):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    async with db_pool.acquire() as conn:
        u = await conn.fetchrow("SELECT balance, created_at FROM users WHERE user_id = $1", message.from_user.id)
        chat_count = await conn.fetchval("SELECT count(*) FROM chats WHERE user_id = $1", message.from_user.id)
        req_count = await conn.fetchval("SELECT count(*) FROM activity_logs WHERE user_id = $1", message.from_user.id)
    
    balance = u["balance"] if u else 0
    reg_date = u["created_at"].strftime("%Y-%m-%d %H:%M") if u and u["created_at"] else "Неизвестно"
    
    profile_text = (
        f"👤 **Ваш профиль:**\n\n"
        f"🆔 **ID:** `{message.from_user.id}`\n"
        f"💰 **Баланс:** `{balance}` баллов\n"
        f"💬 **Всего чатов:** {chat_count}\n"
        f"⚡ **Всего запросов:** {req_count}\n"
        f"📅 **Дата регистрации:** {reg_date}"
    )
    await message.answer(profile_text, parse_mode=ParseMode.MARKDOWN)

@dp.message(F.text == "🎁 Промокод")
async def handle_enter_promo_btn(message: types.Message, state: FSMContext):
    if await is_user_banned(message.from_user.id):
        return
    await track_user(message.from_user)
    await state.set_state(UserStates.waiting_for_promo_code)
    await message.answer("🎁 Введите промокод для активации бонуса:")

@dp.message(StateFilter(UserStates.waiting_for_promo_code))
async def handle_promo_activation(message: types.Message, state: FSMContext):
    code_text = message.text.strip().upper()
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        promo = await conn.fetchrow("""
            SELECT code, reward, max_activations, used_count, expires_at 
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

        # Начисляем баланс
        async with conn.transaction():
            await conn.execute("INSERT INTO promocode_activations (code, user_id) VALUES ($1, $2)", code_text, user_id)
            await conn.execute("UPDATE promocodes SET used_count = used_count + 1 WHERE code = $1", code_text)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", promo["reward"], user_id)

    await message.answer(f"🎉 Промокод успешно активирован! Вам начислено: **+{promo['reward']}** баллов.", parse_mode=ParseMode.MARKDOWN)
    await state.clear()

# ─── Админ-панель (ID: 5480751648) ───

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

# 1. Метрики аудитории
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

    text = (
        "📊 **Метрики аудитории:**\n\n"
        f"👥 **Всего пользователей:** `{total}`\n"
        f"🟢 **DAU (активные за 24ч):** `{dau}`\n\n"
        f"📈 **Новые пользователи:**\n"
        f" ├ За 24 часа: `+{new_24h}`\n"
        f" ├ За 7 дней: `+{new_7d}`\n"
        f" └ За 30 дней: `+{new_30d}`\n\n"
        f"🚫 **Заблокированы администратором:** `{banned}`\n"
        f"🔕 **Заблокировали бота:** `{blocked}`"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]])
    await call.message.edit_text(text, reply_markup=back_kb, parse_mode=ParseMode.MARKDOWN)

# 2. Метрики активности
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

@dp.callback_query(F.data == "admin_back_main")
async def handle_admin_back_main(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.clear()
    await call.message.edit_text(
        "🛠 **Панель управления администратора**\nВыберите нужный раздел:",
        reply_markup=get_admin_main_keyboard(),
        parse_mode=ParseMode.MARKDOWN
    )

# 3. CRM / Поиск пользователей
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
    q = message.text.strip().replace("@", "")
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
    async with db_pool.acquire() as conn:
        chats = await conn.fetch("SELECT id, title FROM chats WHERE user_id = $1 ORDER BY id DESC LIMIT 5", uid)
        req_count = await conn.fetchval("SELECT count(*) FROM activity_logs WHERE user_id = $1", uid)

    chats_list_str = "\n".join([f" • ID {c['id']}: {c['title']}" for c in chats]) if chats else "Нет чатов"
    ban_status = "🔴 Заблокирован" if user["is_banned"] else "🟢 Активен"
    
    card = (
        f"👤 **Карточка пользователя:**\n\n"
        f"🆔 **ID:** `{uid}`\n"
        f"👤 **Username:** @{user['username'] or 'нет'}\n"
        f"📛 **Имя:** {user['first_name']}\n"
        f"💰 **Баланс:** `{user['balance']}`\n"
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
            [InlineKeyboardButton(text="🧹 Сбросить контекст/сессию", callback_data=f"admin_reset_session:{uid}")],
            [InlineKeyboardButton(text="🗑️ Удалить все диалоги", callback_data=f"admin_clear_chats:{uid}")],
            [InlineKeyboardButton(text="◀️ Меню админки", callback_data="admin_back_main")]
        ]
    )
    await message.answer(card, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
    await state.clear()

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
    await handle_admin_back_main(call, None)

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

# 4. Генератор промокодов
@dp.callback_query(F.data == "admin_create_promo")
async def handle_admin_create_promo_step1(call: CallbackQuery, state: FSMContext):
    if call.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminStates.waiting_for_promo_reward)
    await call.message.edit_text("🎁 **Шаг 1/3:** Введите количество бонусных баллов за активацию:")

@dp.message(StateFilter(AdminStates.waiting_for_promo_reward))
async def handle_admin_create_promo_step2(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        reward = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Введите корректное число для баланса:")
        return
    await state.update_data(promo_reward=reward)
    await state.set_state(AdminStates.waiting_for_promo_activations)
    await message.answer("👥 **Шаг 2/3:** Введите максимальное число активаций (например: 10):")

@dp.message(StateFilter(AdminStates.waiting_for_promo_activations))
async def handle_admin_create_promo_step3(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        activations = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введите целое число активаций:")
        return
    await state.update_data(promo_activations=activations)
    await state.set_state(AdminStates.waiting_for_promo_duration)
    await message.answer("⏱ **Шаг 3/3:** Введите время жизни промокода в минутах (0 — бессрочно):")

@dp.message(StateFilter(AdminStates.waiting_for_promo_duration))
async def handle_admin_create_promo_finish(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        duration_minutes = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введите число минут (0 — бессрочно):")
        return

    data = await state.get_data()
    reward = data["promo_reward"]
    activations = data["promo_activations"]
    code = f"EVO-{secrets.token_hex(3).upper()}"
    expires_at = datetime.now() + timedelta(minutes=duration_minutes) if duration_minutes > 0 else None

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO promocodes (code, reward, max_activations, expires_at)
            VALUES ($1, $2, $3, $4)
        """, code, reward, activations, expires_at)

    exp_str = expires_at.strftime('%Y-%m-%d %H:%M') if expires_at else "Бессрочно"
    res_text = (
        f"✅ **Промокод успешно создан!**\n\n"
        f"🔑 **Код:** `{code}`\n"
        f"💰 **Бонус:** `{reward}` баллов\n"
        f"👥 **Лимит активаций:** `{activations}`\n"
        f"⌛ **Действителен до:** `{exp_str}`"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В панель", callback_data="admin_back_main")]])
    await message.answer(res_text, reply_markup=back_kb, parse_mode=ParseMode.MARKDOWN)
    await state.clear()

# 5. Система Рассылки сообщений
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
    btn_text = message.text.strip()
    if btn_text not in ["0", "пропустить", "нет", "none"]:
        if "|" in btn_text:
            title, url = btn_text.split("|", 1)
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=title.strip(), url=url.strip())]])

    # Выборка получателей
    async with db_pool.acquire() as conn:
        if target == "all":
            rows = await conn.fetch("SELECT user_id FROM users WHERE is_banned = FALSE AND is_blocked = FALSE")
        else:
            days = int(target)
            rows = await conn.fetch("""
                SELECT user_id FROM users 
                WHERE is_banned = FALSE AND is_blocked = FALSE 
                AND last_activity >= NOW() - ($1 || ' days')::INTERVAL
            """, str(days))

    user_ids = [r["user_id"] for r in rows]
    total = len(user_ids)
    
    status_msg = await message.answer(f"🚀 Запуск рассылки на {total} получателей...")
    
    success, blocked, failed = 0, 0, 0
    # Пакетная отправка: 25 сообщений/сек
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
async def cmd_start(message: types.Message):
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
    greeting = (  
        "👋 Здравствуйте! Я **Evo Lumen 1.0** — искусственный интеллект, "  
        "разработанный компанией **Quantum**.\n\n"  
        "✨ **Возможности:**\n"  
        "• Мгновенные ответы и решение задач\n"  
        "• Проектирование и аудит сложного программного кода\n"  
        "• Анализ голосовых сообщений и аудио\n"  
        "• Чтение документов (PDF, DOCX, EPUB, FB2, TXT, ZIP)\n"  
        "• Экспорт всей истории чата в файлы `.md` или `.txt`\n"  
        "• Анализ содержимого веб-страниц по URL-ссылкам\n"  
        "• Выгрузка любого сгенерированного кода в файлы нужного формата\n"  
        "• Система промокодов и сохранение сессий в БД"  
    )  
    await message.answer(
        greeting, 
        reply_markup=get_main_reply_keyboard(message.from_user.id == ADMIN_ID), 
        parse_mode=ParseMode.MARKDOWN
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

# ─── Основной обработчик запросов к ИИ с защитой от флуда ───

@dp.message()
async def handle_all_prompts(message: types.Message):
    user_id = message.from_user.id
    
    # 1. Проверка бана
    if await is_user_banned(user_id):
        await message.answer("⛔ Вы заблокированы в системе.")
        return

    # 2. Трекинг пользователя
    await track_user(message.from_user)

    # 3. Проверка подписки
    is_sub = await check_subscription_status(user_id)
    if not is_sub:
        await message.answer(
            "🔒 Доступ ограничен! Для продолжения диалога подпишитесь на наш канал.",
            reply_markup=get_sub_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # 4. Защита от флуда (1 запрос в 15 секунд)
    now = time.time()
    last_time = user_last_request_time.get(user_id, 0)
    if user_id != ADMIN_ID and (now - last_time) < RATE_LIMIT_SECONDS:
        remaining = int(RATE_LIMIT_SECONDS - (now - last_time))
        await message.answer(f"⏳ **Защита от флуда:** Пожалуйста, подождите `{remaining}` сек. перед следующим запросом.")
        return

    prompt_text, images, content_type = await extract_content_from_message(message)  
    if not prompt_text and not images:  
        return  

    user_last_request_time[user_id] = now
    await log_activity(user_id, content_type)

    active_chat_id = await get_or_create_active_chat(user_id)  
    await save_message(active_chat_id, "user", prompt_text)  
    history = await get_chat_messages(active_chat_id, limit=8)  

    status_msg = await message.answer(  
        "⚡ *Evo Lumen 1.0* анализирует задачу...\n⏱ _Время размышления:_ *0 сек.*",  
        parse_mode=ParseMode.MARKDOWN  
    )  
    updater = StatusUpdater(status_msg)  
    updater.start()  

    try:  
        messages_payload = []  
        for hist in history[:-1]:  
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

        flash_res = await client_flash.chat.completions.create(  
            model=MODEL_FLASH,  
            messages=messages_payload,  
            temperature=0.2  
        )  
        raw_flash_output = flash_res.choices[0].message.content.strip()  

        if "[MODE: DIRECT]" in raw_flash_output:  
            final_answer = raw_flash_output.replace("[MODE: DIRECT]", "").strip()  
        elif "[MODE: CODE_DRAFT]" in raw_flash_output or "```" in raw_flash_output:  
            updater.set_stage("🧠 *Evo Lumen 1.0* проводит глубокий аудит кода...")  
            draft_code = raw_flash_output.replace("[MODE: CODE_DRAFT]", "").strip()  

            pro_input = f"Исходный запрос:\n{prompt_text}\n\nЧерновик решения:\n{draft_code}"  
            pro_res = await client_pro.chat.completions.create(  
                model=MODEL_PRO,  
                messages=[{"role": "user", "content": pro_input}],  
                temperature=0.1  
            )  
            final_answer = pro_res.choices[0].message.content.strip()  
        else:  
            final_answer = raw_flash_output  

        await save_message(active_chat_id, "assistant", final_answer)  

    except Exception as e:  
        final_answer = f"⚠️ **Ошибка при обработке запроса:** `{str(e)}`"  
    finally:  
        await updater.stop()  
        try:  
            await status_msg.delete()  
        except Exception:  
            pass  

    await send_response(message, final_answer, user_prompt=prompt_text)

# ─── Запуск приложения ───

async def main():
    await init_db()
    print("🚀 База данных PostgreSQL инициализирована.")
    print(f"👑 Админ панель активирована для ID: {ADMIN_ID}")
    print("🤖 Evo Lumen 1.0 запущен и готов к работе...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
