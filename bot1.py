import os
import io
import re
import time
import base64
import zipfile
import asyncio
import aiosqlite
import aiohttp
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

# ─── Конфигурация токенов и ключей (из переменных окружения) ───

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
FLASH_API_KEY = os.getenv("FLASH_API_KEY", "")
PRO_API_KEY = os.getenv("PRO_API_KEY", "")

# ─── Настройки каналов и моделей ───

CHANNEL_USERNAME = "@Quantum_Evo"
CHANNEL_URL = "https://t.me/Quantum_Evo"
DB_PATH = "bot_database.db"

FLASH_BASE_URL = "https://gorouter.app/v1"
MODEL_FLASH = "claude-opus-4-8"

PRO_BASE_URL = "https://gorouter.app/v1"
MODEL_PRO = "claude-opus-5-thinking"

# ─── Инициализация ───

bot = Bot(token=TG_BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

client_flash = AsyncOpenAI(api_key=FLASH_API_KEY, base_url=FLASH_BASE_URL)
client_pro = AsyncOpenAI(api_key=PRO_API_KEY, base_url=PRO_BASE_URL)

# ─── База Данных (aiosqlite) ───

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            role TEXT,
            content TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (chat_id) REFERENCES chats (id) ON DELETE CASCADE
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS active_sessions (
            user_id INTEGER PRIMARY KEY,
            active_chat_id INTEGER
        )
        """)
        await db.commit()

async def get_or_create_active_chat(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT active_chat_id FROM active_sessions WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                return row[0]

        async with db.execute("INSERT INTO chats (user_id, title) VALUES (?, ?)", (user_id, "Основной диалог")) as cursor:  
            chat_id = cursor.lastrowid  
        await db.execute("INSERT OR REPLACE INTO active_sessions (user_id, active_chat_id) VALUES (?, ?)", (user_id, chat_id))  
        await db.commit()  
        return chat_id

async def set_active_chat(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO active_sessions (user_id, active_chat_id) VALUES (?, ?)", (user_id, chat_id))
        await db.commit()

async def get_user_chats(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT id, title FROM chats WHERE user_id = ? ORDER BY id DESC", (user_id,)) as cursor:
            return await cursor.fetchall()

async def get_chat_title(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT title FROM chats WHERE id = ?", (chat_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else "Без названия"

async def delete_chat_db(chat_id: int, user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM messages WHERE chat_id = ?", (chat_id,))
        await db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        async with db.execute("SELECT active_chat_id FROM active_sessions WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            if row and row[0] == chat_id:
                await db.execute("DELETE FROM active_sessions WHERE user_id = ?", (user_id,))
        await db.commit()

async def rename_chat_db(chat_id: int, new_title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE chats SET title = ? WHERE id = ?", (new_title, chat_id))
        await db.commit()

async def save_message(chat_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO messages (chat_id, role, content) VALUES (?, ?, ?)", (chat_id, role, content))
        await db.commit()

async def get_chat_messages(chat_id: int, limit: int = 10):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT role, content FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit)
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

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

# ─── Отправка ответов (включая .txt файлы по запросу) ───

async def send_response(message: types.Message, text: str, user_prompt: str = ""):
    file_triggers = [
        r'\b(в\s+(виде\s+|формате\s+)?(файла?|тхт|txt))\b',
        r'\b(файлом|текстовым\s+файлом|как\s+файл)\b',
        r'\b(сохрани|скинь|отправь|выдай|выгрузи|дай).*(файл|тхт|txt)\b',
        r'\b(файл|тхт|txt).*(сохрани|скинь|отправь|выдай|выгрузи|дай)\b'
    ]
    wants_file = any(re.search(pattern, user_prompt, re.IGNORECASE) for pattern in file_triggers)

    if wants_file:
        code_blocks = re.findall(r'```(?:\w+)?\n([\s\S]*?)```', text)
        if code_blocks:
            file_content = "\n\n".join(code_blocks)
            filename = "code.txt"
        else:
            file_content = text
            filename = "response.txt"

        input_file = BufferedInputFile(file_content.encode("utf-8"), filename=filename)
        try:
            await message.answer_document(document=input_file, caption="📄 Файл по вашему запросу:")
        except Exception:
            pass

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
        "• Выгрузка готового кода в файлы `.txt`\n"  
        "• Распознавание изображений\n"  
        "• Сохранение истории и переключение между диалогами"  
    )  
    await message.answer(greeting, reply_markup=get_main_reply_keyboard(), parse_mode=ParseMode.MARKDOWN)

# ─── Управление чатами ───

@dp.message(F.text == "➕ Новый чат")
async def handle_new_chat(message: types.Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("INSERT INTO chats (user_id, title) VALUES (?, ?)", (message.from_user.id, "Новый диалог")) as cursor:
            new_id = cursor.lastrowid
        await db.execute("INSERT OR REPLACE INTO active_sessions (user_id, active_chat_id) VALUES (?, ?)", (message.from_user.id, new_id))
        await db.commit()

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

# ─── Обработка файлов, архивов, изображений, голосовых и URL ───

async def extract_content_from_message(message: types.Message) -> tuple[str, list]:
    text_content = message.caption or message.text or ""
    image_payloads = []

    # Голосовые и аудиофайлы
    if message.voice:
        transcription = await transcribe_audio_file(message.voice.file_id)
        text_content = f"{text_content}\n\n🎙 Голосовое сообщение: {transcription}" if text_content else transcription
    elif message.audio:
        transcription = await transcribe_audio_file(message.audio.file_id)
        text_content = f"{text_content}\n\n🎙 Аудиозапись: {transcription}" if text_content else transcription

    # Фотографии
    elif message.photo:  
        photo = message.photo[-1]  
        file_io = io.BytesIO()  
        await bot.download(photo.file_id, destination=file_io)  
        b64_img = base64.b64encode(file_io.getvalue()).decode("utf-8")  
        image_payloads.append(b64_img)  
        if not text_content:  
            text_content = "Проанализируй прикрепленное изображение."  

    # Документы (PDF, DOCX, FB2, EPUB, ZIP, TXT)
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

    # Парсинг URL ссылок в тексте сообщения
    if text_content:
        urls_info = await extract_urls_content(text_content)
        if urls_info:
            text_content += urls_info

    return text_content, image_payloads

# ─── Основной обработчик сообщений с ИИ-конвейером ───

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

        # Шаг 1: Flash модуль  
        flash_res = await client_flash.chat.completions.create(  
            model=MODEL_FLASH,  
            messages=messages_payload,  
            temperature=0.2  
        )  
        raw_flash_output = flash_res.choices[0].message.content.strip()  

        # Шаг 2: Маршрутизация  
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
    print("🚀 База данных инициализирована. Evo Lumen 1.0 (Quantum) запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
