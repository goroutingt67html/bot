import os
import sys
import json
import base64
import io
import zipfile
import logging
import asyncio
from typing import List, Dict, Any

from aiohttp import web
from dotenv import load_dotenv
from openai import AsyncOpenAI

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

load_dotenv()

# Configuration from Environment
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GOROUTER_API_KEY = os.getenv("GOROUTER_API_KEY", "")
GOROUTER_BASE_URL = os.getenv("GOROUTER_BASE_URL", "https://gorouter.app/v1")
PORT = int(os.getenv("PORT", "8080"))
WEBAPP_URL = os.getenv("WEBAPP_URL", f"https://bot-production-57e1.up.railway.app")

MODELS = [
    {"id": "claude-opus-5", "name": "Claude Opus 5"},
    {"id": "claude-opus-5-thinking", "name": "Claude Opus 5 Thinking"},
    {"id": "claude-opus-4-8", "name": "Claude Opus 4.8"},
    {"id": "claude-opus-4-8-thinking", "name": "Claude Opus 4.8 Thinking"},
]

client = AsyncOpenAI(
    api_key=GOROUTER_API_KEY,
    base_url=GOROUTER_BASE_URL,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN) if BOT_TOKEN else None
dp = Dispatcher()


# --- HTML Template with Claude Orange Theme, Dark/Light mode, Chat History & Attachments ---
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ru" data-theme="dark">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>Agent Task</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <style>
    :root {
      --bg-primary: #181816;
      --bg-secondary: #22221f;
      --bg-surface: #2b2b27;
      --bg-input: #1e1e1b;
      --text-main: #f3f3ee;
      --text-muted: #a3a398;
      --border-color: #383832;
      --accent: #d97757;
      --accent-hover: #c15f3e;
      --user-bubble: #2d2a26;
      --bot-bubble: #22221f;
      --font-stack: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    }

    [data-theme="light"] {
      --bg-primary: #fbfaf7;
      --bg-secondary: #f3f1eb;
      --bg-surface: #e8e5dc;
      --bg-input: #ffffff;
      --text-main: #242422;
      --text-muted: #73726c;
      --border-color: #dedbd2;
      --accent: #d97757;
      --accent-hover: #c15f3e;
      --user-bubble: #f1ede4;
      --bot-bubble: #ffffff;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: var(--font-stack);
      -webkit-tap-highlight-color: transparent;
    }

    body {
      background-color: var(--bg-primary);
      color: var(--text-main);
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }

    /* Header */
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 12px 16px;
      background: var(--bg-secondary);
      border-bottom: 1px solid var(--border-color);
      z-index: 10;
    }

    .header-left, .header-right {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .app-title {
      font-weight: 700;
      font-size: 1.1rem;
      color: var(--accent);
      letter-spacing: -0.3px;
    }

    .icon-btn {
      background: transparent;
      border: 1px solid var(--border-color);
      color: var(--text-main);
      padding: 6px 10px;
      border-radius: 8px;
      cursor: pointer;
      display: flex;
      align-items: center;
      font-size: 0.85rem;
      transition: all 0.2s;
    }

    .icon-btn:hover {
      background: var(--bg-surface);
      border-color: var(--accent);
    }

    /* Sidebar / Drawer */
    #sidebar {
      position: fixed;
      top: 0;
      left: -280px;
      width: 280px;
      height: 100%;
      background: var(--bg-secondary);
      border-right: 1px solid var(--border-color);
      z-index: 100;
      transition: left 0.3s ease;
      display: flex;
      flex-direction: column;
    }

    #sidebar.open {
      left: 0;
    }

    .sidebar-header {
      padding: 16px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .sidebar-content {
      flex: 1;
      overflow-y: auto;
      padding: 12px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .chat-item {
      padding: 10px 12px;
      border-radius: 8px;
      background: var(--bg-primary);
      border: 1px solid var(--border-color);
      font-size: 0.85rem;
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: center;
      color: var(--text-main);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .chat-item.active {
      border-color: var(--accent);
      font-weight: 600;
    }

    .overlay {
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: rgba(0, 0, 0, 0.5);
      z-index: 90;
      display: none;
    }

    .overlay.active {
      display: block;
    }

    /* Chat Area */
    #chat-container {
      flex: 1;
      overflow-y: auto;
      padding: 16px;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .message {
      max-width: 88%;
      padding: 12px 16px;
      border-radius: 12px;
      line-height: 1.5;
      font-size: 0.92rem;
      word-break: break-word;
    }

    .message.user {
      align-self: flex-end;
      background: var(--user-bubble);
      border: 1px solid var(--border-color);
      border-bottom-right-radius: 4px;
    }

    .message.assistant {
      align-self: flex-start;
      background: var(--bot-bubble);
      border: 1px solid var(--border-color);
      border-bottom-left-radius: 4px;
    }

    .message .model-tag {
      font-size: 0.72rem;
      color: var(--accent);
      margin-bottom: 4px;
      font-weight: 600;
    }

    .message pre {
      background: var(--bg-primary);
      padding: 10px;
      border-radius: 6px;
      overflow-x: auto;
      margin: 8px 0;
      border: 1px solid var(--border-color);
    }

    .message code {
      font-family: monospace;
      font-size: 0.85rem;
    }

    .files-badge-container {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-bottom: 8px;
    }

    .file-badge {
      font-size: 0.75rem;
      background: var(--bg-surface);
      padding: 4px 8px;
      border-radius: 6px;
      border: 1px solid var(--border-color);
      color: var(--text-muted);
    }

    /* Input Section */
    .input-section {
      background: var(--bg-secondary);
      border-top: 1px solid var(--border-color);
      padding: 10px 14px;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .attachments-preview {
      display: flex;
      gap: 8px;
      overflow-x: auto;
      padding-bottom: 4px;
    }

    .preview-pill {
      display: flex;
      align-items: center;
      gap: 6px;
      background: var(--bg-surface);
      border: 1px solid var(--border-color);
      padding: 4px 8px;
      border-radius: 6px;
      font-size: 0.78rem;
    }

    .preview-pill span.remove {
      color: var(--accent);
      cursor: pointer;
      font-weight: bold;
    }

    .input-row {
      display: flex;
      gap: 8px;
      align-items: flex-end;
    }

    textarea {
      flex: 1;
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      color: var(--text-main);
      padding: 10px 12px;
      border-radius: 10px;
      resize: none;
      height: 44px;
      max-height: 120px;
      font-size: 0.95rem;
      outline: none;
    }

    textarea:focus {
      border-color: var(--accent);
    }

    .action-btn {
      width: 44px;
      height: 44px;
      background: var(--accent);
      color: #ffffff;
      border: none;
      border-radius: 10px;
      cursor: pointer;
      display: flex;
      justify-content: center;
      align-items: center;
      flex-shrink: 0;
      transition: background 0.2s;
    }

    .action-btn:hover {
      background: var(--accent-hover);
    }

    .action-btn:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .attach-btn {
      width: 44px;
      height: 44px;
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      color: var(--text-main);
      border-radius: 10px;
      cursor: pointer;
      display: flex;
      justify-content: center;
      align-items: center;
      flex-shrink: 0;
    }

    /* Model Selector */
    .bottom-controls {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .model-selector {
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      color: var(--text-main);
      padding: 5px 8px;
      border-radius: 8px;
      font-size: 0.8rem;
      outline: none;
      cursor: pointer;
    }

    .model-selector:focus {
      border-color: var(--accent);
    }

    #file-input {
      display: none;
    }
  </style>
</head>
<body>

  <div class="overlay" id="overlay" onclick="toggleSidebar()"></div>

  <!-- Sidebar: Chat History -->
  <div id="sidebar">
    <div class="sidebar-header">
      <h3 style="font-size:1rem;">История чатов</h3>
      <button class="icon-btn" onclick="toggleSidebar()">✕</button>
    </div>
    <div style="padding: 12px 12px 0;">
      <button class="icon-btn" style="width:100%; justify-content:center; background: var(--accent); color:#fff; border:none;" onclick="createNewChat()">+ Новый чат</button>
    </div>
    <div class="sidebar-content" id="chat-list"></div>
  </div>

  <!-- Header -->
  <header>
    <div class="header-left">
      <button class="icon-btn" onclick="toggleSidebar()">☰</button>
      <span class="app-title">Agent Task</span>
    </div>
    <div class="header-right">
      <button class="icon-btn" onclick="toggleTheme()" id="theme-btn">☀️ Тема</button>
    </div>
  </header>

  <!-- Chat Content -->
  <div id="chat-container"></div>

  <!-- Input Section -->
  <div class="input-section">
    <div class="attachments-preview" id="attachments-preview"></div>
    
    <div class="input-row">
      <input type="file" id="file-input" multiple onchange="handleFileSelect(event)" accept="*/*">
      <button class="attach-btn" onclick="document.getElementById('file-input').click()" title="Прикрепить фото, zip или файлы">📎</button>
      <textarea id="prompt-input" placeholder="Спросите что-нибудь..." rows="1" oninput="autoResize(this)" onkeydown="handleKeyDown(event)"></textarea>
      <button class="action-btn" id="send-btn" onclick="sendMessage()">➤</button>
    </div>

    <div class="bottom-controls">
      <div style="font-size:0.75rem; color:var(--text-muted);">Модель Claude:</div>
      <select class="model-selector" id="model-select">
        <option value="claude-opus-5">Claude Opus 5</option>
        <option value="claude-opus-5-thinking">Claude Opus 5 Thinking</option>
        <option value="claude-opus-4-8">Claude Opus 4.8</option>
        <option value="claude-opus-4-8-thinking">Claude Opus 4.8 Thinking</option>
      </select>
    </div>
  </div>

  <script>
    if (window.Telegram && window.Telegram.WebApp) {
      window.Telegram.WebApp.ready();
      window.Telegram.WebApp.expand();
    }

    let attachedFiles = [];
    let chats = JSON.parse(localStorage.getItem('agent_task_chats') || '[]');
    let currentChatId = localStorage.getItem('agent_task_current_chat') || null;

    function init() {
      if (!chats.length || !currentChatId) {
        createNewChat();
      } else {
        loadChat(currentChatId);
      }
      renderChatList();
    }

    function toggleTheme() {
      const html = document.documentElement;
      const nextTheme = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
      html.setAttribute('data-theme', nextTheme);
      document.getElementById('theme-btn').innerText = nextTheme === 'dark' ? '☀️ Тема' : '🌙 Тема';
    }

    function toggleSidebar() {
      document.getElementById('sidebar').classList.toggle('open');
      document.getElementById('overlay').classList.toggle('active');
    }

    function createNewChat() {
      const newId = 'chat_' + Date.now();
      const newChat = {
        id: newId,
        title: 'Новый диалог',
        messages: []
      };
      chats.unshift(newChat);
      currentChatId = newId;
      saveChats();
      renderChatList();
      loadChat(newId);
      if (document.getElementById('sidebar').classList.contains('open')) {
        toggleSidebar();
      }
    }

    function loadChat(id) {
      currentChatId = id;
      localStorage.setItem('agent_task_current_chat', id);
      const chat = chats.find(c => c.id === id);
      const container = document.getElementById('chat-container');
      container.innerHTML = '';

      if (chat && chat.messages) {
        chat.messages.forEach(msg => appendMessageUI(msg.role, msg.content, msg.model, msg.files));
      }
      renderChatList();
    }

    function saveChats() {
      localStorage.setItem('agent_task_chats', JSON.stringify(chats));
    }

    function renderChatList() {
      const list = document.getElementById('chat-list');
      list.innerHTML = '';
      chats.forEach(c => {
        const item = document.createElement('div');
        item.className = 'chat-item' + (c.id === currentChatId ? ' active' : '');
        item.innerHTML = `<span>${escapeHtml(c.title || 'Чат')}</span><span onclick="deleteChat(event, '${c.id}')" style="color:var(--text-muted); padding-left:8px;">✕</span>`;
        item.onclick = (e) => {
          if (e.target.tagName !== 'SPAN' || !e.target.innerText.includes('✕')) {
            loadChat(c.id);
            toggleSidebar();
          }
        };
        list.appendChild(item);
      });
    }

    function deleteChat(e, id) {
      e.stopPropagation();
      chats = chats.filter(c => c.id !== id);
      if (currentChatId === id) {
        currentChatId = chats.length ? chats[0].id : null;
      }
      saveChats();
      if (!chats.length) {
        createNewChat();
      } else {
        loadChat(currentChatId);
      }
      renderChatList();
    }

    function autoResize(el) {
      el.style.height = 'auto';
      el.style.height = Math.min(el.scrollHeight, 120) + 'px';
    }

    function handleKeyDown(e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    }

    function handleFileSelect(event) {
      const files = Array.from(event.target.files);
      files.forEach(file => {
        const reader = new FileReader();
        reader.onload = (e) => {
          attachedFiles.push({
            name: file.name,
            type: file.type,
            size: file.size,
            data: e.target.result
          });
          renderAttachmentPreviews();
        };
        reader.readAsDataURL(file);
      });
      event.target.value = '';
    }

    function renderAttachmentPreviews() {
      const container = document.getElementById('attachments-preview');
      container.innerHTML = '';
      attachedFiles.forEach((file, index) => {
        const pill = document.createElement('div');
        pill.className = 'preview-pill';
        pill.innerHTML = `<span>📄 ${escapeHtml(file.name)}</span><span class="remove" onclick="removeAttachment(${index})">✕</span>`;
        container.appendChild(pill);
      });
    }

    function removeAttachment(index) {
      attachedFiles.splice(index, 1);
      renderAttachmentPreviews();
    }

    function appendMessageUI(role, content, model = null, files = []) {
      const container = document.getElementById('chat-container');
      const msgDiv = document.createElement('div');
      msgDiv.className = `message ${role}`;

      let inner = '';
      if (role === 'assistant' && model) {
        inner += `<div class="model-tag">${escapeHtml(model)}</div>`;
      }
      if (files && files.length > 0) {
        inner += `<div class="files-badge-container">`;
        files.forEach(f => {
          inner += `<span class="file-badge">📎 ${escapeHtml(f.name || f)}</span>`;
        });
        inner += `</div>`;
      }

      inner += `<div>${role === 'assistant' ? marked.parse(content) : escapeHtml(content).replace(/\\n/g, '<br>')}</div>`;
      msgDiv.innerHTML = inner;
      container.appendChild(msgDiv);
      container.scrollTop = container.scrollHeight;
    }

    async function sendMessage() {
      const input = document.getElementById('prompt-input');
      const text = input.value.trim();
      const model = document.getElementById('model-select').value;
      const sendBtn = document.getElementById('send-btn');

      if (!text && attachedFiles.length === 0) return;

      const currentFiles = [...attachedFiles];
      attachedFiles = [];
      renderAttachmentPreviews();

      input.value = '';
      input.style.height = '44px';

      appendMessageUI('user', text, null, currentFiles);

      const chat = chats.find(c => c.id === currentChatId);
      if (chat) {
        if (chat.messages.length === 0 && text) {
          chat.title = text.slice(0, 24) + (text.length > 24 ? '...' : '');
        }
        chat.messages.push({ role: 'user', content: text, files: currentFiles });
        saveChats();
        renderChatList();
      }

      sendBtn.disabled = true;

      try {
        const response = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            model: model,
            messages: chat ? chat.messages : [{ role: 'user', content: text }],
            current_message: text,
            files: currentFiles
          })
        });

        const data = await response.json();
        if (data.error) {
          appendMessageUI('assistant', '⚠️ Ошибка: ' + data.error);
        } else {
          appendMessageUI('assistant', data.reply, model);
          if (chat) {
            chat.messages.push({ role: 'assistant', content: data.reply, model: model });
            saveChats();
          }
        }
      } catch (err) {
        appendMessageUI('assistant', '⚠️ Не удалось связаться с сервером.');
      } finally {
        sendBtn.disabled = false;
      }
    }

    function escapeHtml(text) {
      if (!text) return '';
      const div = document.createElement('div');
      div.innerText = text;
      return div.innerHTML;
    }

    init();
  </script>
</body>
</html>
"""


# --- File and Data Processing Helpers ---
def extract_file_content(file_item: Dict[str, Any]) -> Dict[str, Any]:
    """Decodes data URL, extracts text from files or archives, prepares multimodal format."""
    name = file_item.get("name", "file")
    data_url = file_item.get("data", "")

    if "," in data_url:
        header, b64_data = data_url.split(",", 1)
    else:
        header, b64_data = "", data_url

    try:
        raw_bytes = base64.b64decode(b64_data)
    except Exception:
        return {"type": "text", "text": f"[Не удалось декодировать файл {name}]"}

    # Handle Images
    if any(name.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]):
        return {
            "type": "image_url",
            "image_url": {"url": data_url}
        }

    # Handle ZIP Archives
    if name.lower().endswith(".zip"):
        extracted_summary = [f"--- Содержимое архива {name} ---"]
        try:
            with zipfile.ZipFile(io.BytesIO(raw_bytes)) as z:
                for file_info in z.infolist():
                    if file_info.is_dir():
                        continue
                    extracted_summary.append(f"Файл: {file_info.filename} ({file_info.file_size} байт)")
                    # If it is a text-like file, read up to 10KB
                    if any(file_info.filename.lower().endswith(ext) for ext in [".txt", ".py", ".js", ".json", ".md", ".csv", ".html", ".css", ".xml", ".env"]):
                        try:
                            with z.open(file_info) as f:
                                preview = f.read(10000).decode("utf-8", errors="ignore")
                                extracted_summary.append(f"```\\n{preview}\\n```")
                        except Exception:
                            pass
            return {"type": "text", "text": "\\n".join(extracted_summary)}
        except Exception as e:
            return {"type": "text", "text": f"[Ошибка чтения zip архива {name}: {str(e)}]"}

    # Handle Plain text/Code/Documents
    try:
        text_content = raw_bytes.decode("utf-8", errors="ignore")
        return {"type": "text", "text": f"--- Файл: {name} ---\\n{text_content}"}
    except Exception:
        return {"type": "text", "text": f"[Файл {name} содержит бинарные данные, размер: {len(raw_bytes)} байт]"}


# --- Web Server Handlers ---
async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=HTML_TEMPLATE, content_type="text/html")


async def handle_chat_api(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        model_id = body.get("model", "claude-opus-5")
        raw_messages = body.get("messages", [])
        current_message = body.get("current_message", "")
        files = body.get("files", [])

        # Build payload for OpenAI-compatible endpoint
        api_messages = []
        for msg in raw_messages[:-1]:
            role = "user" if msg.get("role") == "user" else "assistant"
            content = msg.get("content", "")
            if content:
                api_messages.append({"role": role, "content": content})

        # Process current turn with potential multimodal/file attachments
        user_content_parts = []
        if current_message:
            user_content_parts.append({"type": "text", "text": current_message})

        for f in files:
            extracted = extract_file_content(f)
            user_content_parts.append(extracted)

        if not user_content_parts:
            user_content_parts.append({"type": "text", "text": "Привет"})

        api_messages.append({"role": "user", "content": user_content_parts})

        response = await client.chat.completions.create(
            model=model_id,
            messages=api_messages,
        )

        reply = response.choices[0].message.content
        return web.json_response({"reply": reply})

    except Exception as e:
        logger.error(f"API Error: {str(e)}")
        return web.json_response({"error": str(e)}, status=500)


# --- Aiogram Bot Setup ---
@dp.message(CommandStart())
async def cmd_start(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🚀 Открыть Agent Task",
                    web_app=WebAppInfo(url=WEBAPP_URL)
                )
            ]
        ]
    )
    await message.answer(
        "👋 Привет! Добро пожаловать в **Agent Task**.\n\n"
        "Нажмите кнопку ниже, чтобы запустить чат с моделями Claude.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


# --- Application Runner ---
async def start_all():
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/chat", handle_chat_api)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Mini App server running on port {PORT}")

    if bot:
        logger.info("Starting Telegram Bot Polling...")
        await dp.start_polling(bot)
    else:
        logger.warning("BOT_TOKEN not provided. Running Web Server only.")
        while True:
            await asyncio.sleep(3600)


if __name__ == "__main__":
    try:
        asyncio.run(start_all())
    except (KeyboardInterrupt, SystemExit):
        logger.info("App stopped.")
