import os
import io
import re
import time
import base64
import zipfile
import asyncio
import aiohttp
import asyncpg
from bs4 import BeautifulSoup
import xml.etree.ElementTree as ET
from pypdf import PdfReader
import docx
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, StateFilter
from aiogram.exceptions import TelegramBadRequest
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
# Railway иногда выдает префикс postgres:// вместо postgresql://
DATABASE_URL = RAW_DB_URL.replace("postgres://", "postgresql://") if RAW_DB_URL else ""

# ─── Настройки каналов и моделей ───

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

# ─── Словари форматов и расширений ───

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
    'py': 'main.py',
    'js': 'index.js',
    'ts': 'index.ts',
    'html': 'index.html',
    'css': 'style.css',
    'json': 'data.json',
    'csv': 'data.csv',
    'sql': 'query.sql',
    'sh': 'script.sh',
    'md': 'README.md',
    'txt': 'document.txt',
    'cpp': 'main.cpp',
    'c': 'main.c',
    'cs': 'Program.cs',
    'java': 'Main.java',
    'go': 'main.go',
    'rs': 'main.rs',
    'php': 'index.php',
    'yaml': 'config.yaml',
    'yml': 'config.yml'
}

# ─── База Данных (PostgreSQL via asyncpg) ───

async def init_db():
    global db_pool
    if not DATABASE_URL:
        raise ValueError("DATABASE_URL не задана в переменных окружения Railway!")
    
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
    
    async with db_pool.acquire() as conn:
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id SERIAL PRIMARY KEY,
            user_id BIGINT,
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
            user_id BIGINT PRIMARY KEY,
            active_chat_id INTEGER
        );
        """)

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
        await conn.execute("DELETE FROM messages WHERE chat_id = $1", chat_id)
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
            "SELECT role, content FROM messages WHERE chat_id = $1 ORDER BY id DESC LIMIT $2",
            chat_id, limit
        )
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

# ─── FSM Состояния ───

class ChatStates(StatesGroup):
    waiting_for_chat_rename = State()

# ─── Клавиатуры ───

def get_sub_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📢 Перейти в канал", url=CHANNEL_URL)],
            [InlineKeyboardButton(text="✅ Подтвердить подписку", callback_data="verify_subscription")]
        ]
    )

def get_main_reply_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Новый чат"), KeyboardButton(text="🖨️ История чатов")]
        ],
        resize_keyboard=True
    )

def get_chat_actions_keyboard(chat_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить диалог", callback_data=f"chat_use:{chat_id}")],
            [InlineKeyboardButton(text="✏️ Изменить название", callback_data=f"chat_rename:{chat_id}")],
            [InlineKeyboardButton(text="🗑️ Удалить чат", callback_data=f"chat_delete:{chat_id}")],
            [InlineKeyboardButton(text="◀️ Назад к списку", callback_data="chat_list_back")]
        ]
    )

# ─── Проверка подписки ───

async def check_subscription_status(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=CHANNEL_USERNAME, user_id=user_id)
        return member.status not in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED)
    except Exception:
        return False

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

# ─── Логика распознавания и выгрузки файлов ───

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
    
    # Для языков программирования отдаем ЧИСТЫЙ код без текста и маркдауна
    if ext in CODE_EXTENSIONS:
        if code_blocks:
            return "\n\n".join(b.strip() for b in code_blocks)
        else:
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

    if code_blocks:
        return "\n\n".join(b.strip() for b in code_blocks)
    return ai_text.strip()

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

# ─── Обработка подписки ───

@dp.callback_query(F.data == "verify_subscription")
async def verify_sub_callback(call: CallbackQuery):
    is_sub = await check_subscription_status(call.from_user.id)
    if is_sub:
        await call.message.delete()
        await call.message.answer(
            "🎉 Спасибо за подписку! Доступ к Evo Lumen 1.0 разблокирован.",
            reply_markup=get_main_reply_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await call.answer("❌ Вы еще не подписались на канал!", show_alert=True)

# ─── Обработчик команды /start ───

@dp.message(CommandStart())
async def cmd_start(message: types.Message):
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
        "• Чтение книг и файлов (PDF, DOCX, EPUB, FB2, TXT, ZIP)\n"  
        "• Анализ содержимого веб-страниц по URL-ссылкам\n"  
        "• Выгрузка любого кода и текста в файлы нужного формата (`.py`, `.js`, `.html`, `.json`, `.md`, `.txt` и др.)\n"  
        "• Сохранение истории в постоянной базе данных"  
    )  
    await message.answer(greeting, reply_markup=get_main_reply_keyboard(), parse_mode=ParseMode.MARKDOWN)

# ─── Управление чатами ───

@dp.message(F.text == "➕ Новый чат")
async def handle_new_chat(message: types.Message):
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
        f"⚙️ Управление чатом:\n📌 {title} (ID: {chat_id})",
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
    await call.message.edit_text(f"✅ Активный диалог переключен на: {title}.\nВы можете продолжить общение!", parse_mode=ParseMode.MARKDOWN)

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
        await message.answer(f"✅ Название чата успешно изменено на: **{new_title}**", parse_mode=ParseMode.MARKDOWN)  
      
    await state.clear()

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

# ─── Обработка файлов, документов и изображений ───

async def extract_content_from_message(message: types.Message) -> tuple[str, list]:
    text_content = message.caption or message.text or ""
    image_payloads = []

    if message.voice:
        transcription = await transcribe_audio_file(message.voice.file_id)
        text_content = f"{text_content}\n\n🎙 Голосовое сообщение: {transcription}" if text_content else transcription
    elif message.audio:
        transcription = await transcribe_audio_file(message.audio.file_id)
        text_content = f"{text_content}\n\n🎙 Аудиозапись: {transcription}" if text_content else transcription

    elif message.photo:  
        photo = message.photo[-1]  
        file_io = io.BytesIO()  
        await bot.download(photo.file_id, destination=file_io)  
        b64_img = base64.b64encode(file_io.getvalue()).decode("utf-8")  
        image_payloads.append(b64_img)  
        if not text_content:  
            text_content = "Проанализируй прикрепленное изображение."  

    elif message.document:  
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
                pdf_texts = []
                for page in reader.pages[:40]:
                    extracted = page.extract_text()
                    if extracted:
                        pdf_texts.append(extracted)
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
                                clean_text = soup.get_text()
                                if clean_text.strip():
                                    epub_texts.append(clean_text)
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

    return text_content, image_payloads

# ─── Основной обработчик сообщений с ИИ ───

@dp.message()
async def handle_all_prompts(message: types.Message):
    is_sub = await check_subscription_status(message.from_user.id)
    if not is_sub:
        await message.answer(
            "🔒 Доступ ограничен! Для продолжения диалога подпишитесь на наш канал.",
            reply_markup=get_sub_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    prompt_text, images = await extract_content_from_message(message)  
    if not prompt_text and not images:  
        return  

    active_chat_id = await get_or_create_active_chat(message.from_user.id)  

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
                messages=[  
                    {"role": "user", "content": pro_input}  
                ],  
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

# ─── Запуск бота ───

async def main():
    await init_db()
    print("🚀 База данных PostgreSQL инициализирована. Evo Lumen 1.0 запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
