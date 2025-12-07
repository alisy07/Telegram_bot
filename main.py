#!/usr/bin/env python3
# main.py — Integrated Telegram control bot + Pyrogram listener with session creation
import os
import re
import sqlite3
import base64
import logging
import threading
import asyncio
from typing import Optional, List, Tuple, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from pyrogram import Client, errors as py_errors, filters as py_filters
from pyrogram.handlers import MessageHandler as PyroMessageHandler

# ---------------- Config ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")  # must be set
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # optional
PORT = int(os.environ.get("PORT", 10000))

ADMIN_ID = 1037850299  # change if needed

# paths
DB_FILE = "bot_data.db"
SESSIONS_DIR = "/opt/render/project/src/sessions"  # fixed path for sessions
os.makedirs(SESSIONS_DIR, exist_ok=True)
SESSION_FILE = os.path.join(SESSIONS_DIR, "listener.session")

# Conversation states
(UPLOAD_SESSION, ADD_CHANNEL, DELETE_CHANNEL, ADD_API, CREATE_SESSION) = range(5)

# ---------------- Database helpers ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS apis (
            user_id INTEGER PRIMARY KEY,
            api_id TEXT,
            api_hash TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_username TEXT,
            target_bot_username TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            data_b64 TEXT
        )
    """)
    conn.commit()
    conn.close()

def save_api(user_id: int, api_id: str, api_hash: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO apis(user_id, api_id, api_hash) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET api_id=excluded.api_id, api_hash=excluded.api_hash",
        (user_id, api_id, api_hash)
    )
    conn.commit()
    conn.close()

def get_api(user_id: int) -> Optional[Tuple[str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT api_id, api_hash FROM apis WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return (row[0], row[1]) if row else None

def save_session_db(user_id: int, filename: str, data_b64: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO sessions(user_id, filename, data_b64) VALUES(?,?,?)", (user_id, filename, data_b64))
    conn.commit()
    conn.close()

def get_last_session_row() -> Optional[Tuple[int, int, str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, filename, data_b64 FROM sessions ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    conn.close()
    return row

def list_sessions_db(user_id: int) -> List[Tuple[int, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, filename FROM sessions WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def add_channel_db(user_id: int, channel: str, target_bot: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT INTO channels(user_id, channel_username, target_bot_username) VALUES(?,?,?)", (user_id, channel, target_bot))
    conn.commit()
    conn.close()

def list_channels_db(user_id: int) -> List[Tuple[int, str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, channel_username, target_bot_username FROM channels WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_channel_db(channel_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()

# ---------------- Filtering ----------------
def filter_text_preserve_rules(text: str) -> str:
    text = re.sub(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', '', text)
    text = re.sub(r'(?i)code', '', text)
    text = re.sub(r'(https?://\S+)|www\.\S+|t\.me/\S+|telegram\.me/\S+', '', text)
    text = re.sub(r'(?<![A-Za-z])\d+(?![A-Za-z])', '', text)
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return "❌ لا يبقى نص قابل للإرسال بعد عملية الفلترة."
    return text

# ---------------- Pyrogram Listener ----------------
class PyroListener:
    def __init__(self):
        self.thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Optional[Client] = None
        self.running = False
        self.monitored_channels: Set[str] = set()
        self.session_user_id: Optional[int] = None

    def _pyro_thread_target(self, session_path: str, api_id: int, api_hash: str, session_user_id: int):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.session_user_id = session_user_id
        client = Client(session_path, api_id=api_id, api_hash=api_hash, workdir=SESSIONS_DIR)
        self.client = client

        async def on_message(c, m):
            chat = m.chat
            if not chat:
                return
            ch_username = getattr(chat, "username", None)
            if not ch_username or not ch_username.startswith("@"):
                return
            if ch_username not in self.monitored_channels:
                return
            raw_text = m.text or m.caption
            if not raw_text:
                return
            filtered = filter_text_preserve_rules(raw_text)
            if filtered.startswith("❌"):
                return
            # find target bot
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT target_bot_username FROM channels WHERE user_id=? AND channel_username=? LIMIT 1",
                        (session_user_id, ch_username))
            row = cur.fetchone()
            conn.close()
            if not row:
                return
            target_bot = row[0]
            if not target_bot.startswith("@"):
                target_bot = "@" + target_bot
            try:
                await c.send_message(target_bot, filtered)
            except Exception:
                pass

        client.add_handler(PyroMessageHandler(on_message, py_filters.all))

        try:
            loop.run_until_complete(client.start())
            self.running = True
            loop.run_until_complete(client.idle())
        except Exception:
            pass
        finally:
            try:
                loop.run_until_complete(client.stop())
            except Exception:
                pass
            self.running = False

    def start_with_session_file(self, session_path: str, api_id: int, api_hash: str, session_user_id: int):
        self.stop()
        t = threading.Thread(target=self._pyro_thread_target, args=(session_path, api_id, api_hash, session_user_id), daemon=True)
        t.start()
        self.thread = t
        # reload channels
        rows = list_channels_db(session_user_id)
        self.monitored_channels = set([r[1] if r[1].startswith("@") else "@" + r[1] for r in rows])
        return True

    def stop(self):
        if self.client and self.loop:
            try:
                asyncio.run_coroutine_threadsafe(self.client.stop(), self.loop).result(timeout=15)
            except Exception:
                pass
        self.client = None
        self.loop = None
        self.thread = None
        self.running = False

pyro_listener = PyroListener()

# ---------------- Telegram UI ----------------
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع جلسة", callback_data="upload_session")],
        [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
        [InlineKeyboardButton("🗑️ حذف قناة", callback_data="delete_channel")],
        [InlineKeyboardButton("📜 عرض القنوات", callback_data="list_channels")],
        [InlineKeyboardButton("🔐 إضافة API", callback_data="add_api")],
        [InlineKeyboardButton("👀 عرض API", callback_data="view_api")],
        [InlineKeyboardButton("🔑 إنشاء جلسة جديدة", callback_data="create_session")],
        [InlineKeyboardButton("🔁 إعادة تشغيل المستمع", callback_data="restart_listener")],
    ])

# ---------------- Handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text("مرحباً — اختر من القائمة:", reply_markup=main_menu())

async def pressed_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data_cb = q.data

    if data_cb == "create_session":
        context.user_data["awaiting_api"] = True
        await q.message.reply_text("🔑 أرسل api_id و api_hash مفصولين بمسافة.")
        return
    if data_cb == "restart_listener":
        api = get_api(uid)
        last = get_last_session_row()
        if last and api:
            pyro_listener.start_with_session_file(SESSION_FILE, int(api[0]), api[1], uid)
            await q.message.reply_text("🔁 تم إعادة تشغيل المستمع.")
        else:
            await q.message.reply_text("❌ لا توجد جلسة أو API صالح لتشغيل المستمع.")
        return

async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()
    if context.user_data.get("awaiting_api"):
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("❌ الصيغة خاطئة. أرسل api_id api_hash")
            return
        api_id, api_hash = parts
        save_api(uid, api_id, api_hash)
        context.user_data["api_id"] = api_id
        context.user_data["api_hash"] = api_hash
        context.user_data["awaiting_api"] = False
        context.user_data["awaiting_phone"] = True
        await update.message.reply_text("📱 أرسل رقم الهاتف بصيغة دولية (+964...)")
        return

    if context.user_data.get("awaiting_phone"):
        context.user_data["phone"] = text
        context.user_data["awaiting_phone"] = False
        context.user_data["awaiting_code"] = True
        await update.message.reply_text("⏳ جاري إرسال الكود...")
        asyncio.create_task(send_pyro_code(update, context))
        return

    if context.user_data.get("awaiting_code"):
        context.user_data["code"] = text
        context.user_data["awaiting_code"] = False
        asyncio.create_task(complete_pyro_login(update, context))
        return

async def send_pyro_code(update, context):
    api_id = int(context.user_data["api_id"])
    api_hash = context.user_data["api_hash"]
    phone = context.user_data["phone"]
    client = Client("temp.session", api_id=api_id, api_hash=api_hash, workdir=SESSIONS_DIR)
    context.user_data["login_client"] = client
    await client.connect()
    try:
        await client.send_code(phone)
        await update.message.reply_text("📨 تم إرسال الكود إلى هاتفك، أرسل الكود هنا.")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ أثناء إرسال الكود: {e}")

async def complete_pyro_login(update, context):
    client: Client = context.user_data.get("login_client")
    code = context.user_data.get("code")
    phone = context.user_data.get("phone")
    try:
        await client.sign_in(phone_number=phone, phone_code=code)
        await client.disconnect()
        # move temp.session -> listener.session
        src = os.path.join(SESSIONS_DIR, "temp.session")
        if os.path.exists(src):
            os.replace(src, SESSION_FILE)
        await update.message.reply_text("✔️ تم إنشاء الجلسة وتشغيل المستمع تلقائياً.")
        api_id = int(context.user_data["api_id"])
        api_hash = context.user_data["api_hash"]
        pyro_listener.start_with_session_file(SESSION_FILE, api_id, api_hash, update.effective_user.id)
    except Exception as e:
        await update.message.reply_text(f"❌ فشل تسجيل الدخول: {e}")

# ----------------- Start bot -----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(pressed_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    # start listener if last session exists
    last = get_last_session_row()
    if last:
        api = get_api(last[1])
        if api:
            pyro_listener.start_with_session_file(SESSION_FILE, int(api[0]), api[1], last[1])

    app.run_polling()

if __name__ == "__main__":
    main()
