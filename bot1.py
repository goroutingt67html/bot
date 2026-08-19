import os
import io
import time
import base64
import zipfile
import asyncio
import aiosqlite
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

FLASH_BASE_URL = "https://seekai.cc/v1"
MODEL_FLASH = "deepseek-v4-flash-0731"

PRO_BASE_URL = "https://seekai.cc/v1"
MODEL_PRO = "glm-5-2"

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


# ─── Промпты ───
IDENTITY_PROMPT = """
Ты — Evo Lumen 1.0, передовая система искусственного интеллекта, созданная компанией Quantum.
СТРОГОЕ ПРАВИЛО: Ты никогда и ни при каких условиях не называешь себя DeepSeek, OpenAI или любыми другими именами. Твой единственный создатель — компания Quantum. На любые вопросы о происхождении отвечай, что ты Evo Lumen 1.0 от Quantum.
"""

FLASH_SYSTEM_PROMPT = IDENTITY_PROMPT + """
Твоя роль: интеллектуальный диспетчер и скоростной генератор Evo Lumen 1.0.

ТЕБЕ НЕОБХОДИМО ОПРЕДЕЛИТЬ СЛОЖНОСТЬ ЗАДАЧИ.
В САМОЙ ПЕРВОЙ СТРОКЕ ответа обязательно укажи один из системных тегов:

1. Если запрос — ОБЫЧНАЯ ТЕКСТОВАЯ ЗАДАЧА (приветствие, диалог, конспект, статья, эссе, базовые школьные задачи, перевод, совет, анализ текстового документа):
   Первая строка: [MODE: DIRECT]
   Далее сразу пиши идеальный, полный, законченный ответ пользователю.
   Используй **жирный шрифт** для акцентов и _курсив_ для пояснений.

2. Если запрос — СЛОЖНАЯ ТЕХНИЧЕСКАЯ ЗАДАЧА ИЛИ ПРОГРАММИРОВАНИЕ (написание кода, скриптов, верстка HTML/CSS, SQL-запросы, системная архитектура, поиск багов в коде, работа с архивом кода):
   Первая строка: [MODE: CODE_DRAFT]
   Далее напиши первичный рабочий черновик решения и кода для передачи в модуль аудита.
"""

PRO_SYSTEM_PROMPT = IDENTITY_PROMPT + """
Твоя роль: модуль глубокого мышления и аудита Evo Lumen 1.0.
Тебе предоставлен исходный запрос пользователя и первичный черновик от скоростного модуля.

Твои задачи:
1. Провести аудит решения, устранить логические ошибки, синтаксические баги и проблемы оптимизации.
2. Использовать форматирование Telegram Markdown:
   - **Жирный шрифт** для заголовков и акцентов.
   - _Курсив_ для пояснений и терминов.
   - Обязательно помещай весь код в блоки ```язык\\nкод\\n``` (это обеспечивает копирование в Telegram по клику).
3. Выдать готовый, безупречный код и краткое перечисление ключевых улучшений.
"""


# ─── Фоновый таймер статуса ───
class StatusUpdater:
    def __init__(self, message: types.Message):
        self.message = message
        self.start_time = time.time()
        self.stage = "⚡ *Evo Lumen 1.0* формирует ответ..."
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


async def send_response(message: types.Message, text: str):
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
            "🎉 **Спасибо за подписку!** Доступ к **Evo Lumen 1.0** разблокирован.",
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
            "🔒 **Для использования бота необходимо подписаться на наш официальный канал!**\n\n"
            "Подпишитесь и нажмите кнопку **«Подтвердить подписку»** ниже.",
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
        "• Анализ файлов (ZIP-архивы, TXT, скрипты, документы)\n"
        "• Распознавание и работа с изображениями\n"
        "• Сохранение истории и переключение между диалогами"
    )
    await message.answer(greeting, reply_markup=get_main_reply_keyboard(), parse_mode=ParseMode.MARKDOWN)


# ─── Управление чатами (История, Создание, Удаление, Переименование) ───
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
    await message.answer("🖨️ **Ваша история диалогов:**\nВыберите чат для управления:", 
                         reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
                         parse_mode=ParseMode.MARKDOWN)


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
    await call.message.edit_text("🖨️ **Ваша история диалогов:**\nВыберите чат для управления:", 
                                 reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_buttons),
                                 parse_mode=ParseMode.MARKDOWN)


@dp.callback_query(F.data.startswith("chat_use:"))
async def handle_chat_select(call: CallbackQuery):
    chat_id = int(call.data.split(":")[1])
    await set_active_chat(call.from_user.id, chat_id)
    title = await get_chat_title(chat_id)
    await call.message.edit_text(f"✅ Активный диалог переключен на: **{title}**.\nВы можете продолжить общение!", parse_mode=ParseMode.MARKDOWN)


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


# ─── Обработка файлов, архивов и изображений ───
async def extract_content_from_message(message: types.Message) -> tuple[str, list]:
    text_content = message.caption or message.text or ""
    image_payloads = []

    # 1. Фотография
    if message.photo:
        photo = message.photo[-1]
        file_io = io.BytesIO()
        await bot.download(photo.file_id, destination=file_io)
        b64_img = base64.b64encode(file_io.getvalue()).decode("utf-8")
        image_payloads.append(b64_img)
        if not text_content:
            text_content = "Проанализируй прикрепленное изображение."

    # 2. Документы (ZIP, TXT, скрипты, JSON и другие файлы)
    elif message.document:
        doc = message.document
        file_io = io.BytesIO()
        await bot.download(doc.file_id, destination=file_io)
        file_bytes = file_io.getvalue()
        file_name = doc.file_name or "file"

        # Обработка ZIP-архива
        if file_name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
                    file_list = z.namelist()
                    extracted_texts = []
                    for name in file_list[:15]:  # Ограничение до 15 файлов для экономии контекста
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
        else:
            # Чтение текстовых и программных документов
            try:
                decoded = file_bytes.decode("utf-8")
                text_content = f"{text_content}\n\n📄 Файл `{file_name}`:\n```\n{decoded[:12000]}\n```"
            except UnicodeDecodeError:
                # Если бинарный файл (не текст)
                text_content = f"{text_content}\n\n📎 Получен бинарный файл `{file_name}` размером {len(file_bytes)} байт."

    return text_content, image_payloads


# ─── Основной обработчик сообщений с ИИ-конвейером ───
@dp.message()
async def handle_all_prompts(message: types.Message):
    # Проверка обязательной подписки
    is_sub = await check_subscription_status(message.from_user.id)
    if not is_sub:
        await message.answer(
            "🔒 **Доступ ограничен!** Для продолжения диалога подпишитесь на наш канал.",
            reply_markup=get_sub_keyboard(),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Извлечение текста и медиа
    prompt_text, images = await extract_content_from_message(message)
    if not prompt_text and not images:
        return

    active_chat_id = await get_or_create_active_chat(message.from_user.id)

    # Сохраняем запрос пользователя
    await save_message(active_chat_id, "user", prompt_text)
    history = await get_chat_messages(active_chat_id, limit=8)

    status_msg = await message.answer(
        "⚡ *Evo Lumen 1.0* анализирует задачу...\n⏱ _Время размышления:_ *0 сек.*",
        parse_mode=ParseMode.MARKDOWN
    )
    updater = StatusUpdater(status_msg)
    updater.start()

    try:
        # Формирование сообщений для модели Flash
        messages_payload = [{"role": "system", "content": FLASH_SYSTEM_PROMPT}]
        for hist in history[:-1]:
            messages_payload.append({"role": hist["role"], "content": hist["content"]})

        if images:
            # Мультимодальный формат для Flash
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
            temperature=0.3
        )
        raw_flash_output = flash_res.choices[0].message.content.strip()

        # Шаг 2: Маршрутизация (Прямой ответ или аудит в Pro)
        if raw_flash_output.startswith("[MODE: DIRECT]"):
            final_answer = raw_flash_output.replace("[MODE: DIRECT]", "").strip()

        elif raw_flash_output.startswith("[MODE: CODE_DRAFT]") or "```" in raw_flash_output:
            updater.set_stage("🧠 *Evo Lumen 1.0* проводит глубокий аудит кода...")
            draft_code = raw_flash_output.replace("[MODE: CODE_DRAFT]", "").strip()

            pro_input = f"Исходный запрос:\n{prompt_text}\n\nЧерновик решения:\n{draft_code}"
            pro_res = await client_pro.chat.completions.create(
                model=MODEL_PRO,
                messages=[
                    {"role": "system", "content": PRO_SYSTEM_PROMPT},
                    {"role": "user", "content": pro_input}
                ],
                temperature=0.1
            )
            final_answer = pro_res.choices[0].message.content.strip()
        else:
            final_answer = raw_flash_output

        # Сохранение ответа ассистента в БД
        await save_message(active_chat_id, "assistant", final_answer)

    except Exception as e:
        final_answer = f"⚠️ **Ошибка при обработке запроса:** `{str(e)}`"
    finally:
        await updater.stop()
        try:
            await status_msg.delete()
        except Exception:
            pass

    await send_response(message, final_answer)


# ─── Запуск бота ───
async def main():
    await init_db()
    print("🚀 База данных инициализирована. Evo Lumen 1.0 (Quantum) запущен...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
