#!/usr/bin/env python3
# main.py — Webhook-ready Telegram control bot + Pyrogram listener
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

# ---------- Config ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")      # required
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://yourapp.up.railway.app
PORT = int(os.environ.get("PORT", 10000))
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1037850299"))

DB_FILE = "bot_data.db"

# Use Railway persistent storage path
SESSIONS_DIR = os.getenv("SESSIONS_DIR", "/mnt/data/sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ---------- DB helpers ----------
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
        (user_id, api_id, api_hash),
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

def list_all_channels_db() -> List[Tuple[int, int, str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, channel_username, target_bot_username FROM channels")
    rows = cur.fetchall()
    conn.close()
    return rows

def delete_channel_db(channel_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()

def get_all_user_ids() -> Set[int]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    ids = set()
    cur.execute("SELECT user_id FROM apis")
    ids.update([r[0] for r in cur.fetchall() if r[0]])
    cur.execute("SELECT DISTINCT user_id FROM channels")
    ids.update([r[0] for r in cur.fetchall() if r[0]])
    cur.execute("SELECT DISTINCT user_id FROM sessions")
    ids.update([r[0] for r in cur.fetchall() if r[0]])
    conn.close()
    return ids

def list_users_db() -> List[int]:
    return sorted(list(get_all_user_ids()))

# ---------- filtering ----------
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

# ---------- Pyrogram listener ----------
class PyroListener:
    def __init__(self):
        self.thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Optional[Client] = None
        self.running = False
        self.monitored_channels: Set[str] = set()
        self.session_user_id: Optional[int] = None

    # === FIXED ===
    def _write_session_file(self, filename: str, b64data: str) -> str:
        """
        Write the .session file to SESSIONS_DIR and return **filename only**.
        Pyrogram expects session_name inside workdir.
        """
        path = os.path.join(SESSIONS_DIR, filename)
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64data))
        return filename  # IMPORTANT: return name only

    # === FIXED ===
    def start_with_session_row(self, session_row) -> bool:
        if not session_row:
            return False
        sid, user_id, filename, data_b64 = session_row
        api = get_api(user_id)
        if not api:
            logger.error("No API credentials for session owner %s", user_id)
            return False
        api_id, api_hash = api

        session_name = self._write_session_file(filename, data_b64)
        return self.start_with_session_file(session_name, int(api_id), api_hash, user_id)

    # === FIXED ===
    def start_with_session_file(self, session_name: str, api_id: int, api_hash: str, session_user_id: int):
        self.stop()

        rows = list_channels_db(session_user_id)
        mon = set()
        for r in rows:
            ch = r[1]
            if ch and not ch.startswith("@"):
                ch = "@" + ch
            mon.add(ch)
        self.monitored_channels = mon
        self.session_user_id = session_user_id

        t = threading.Thread(
            target=self._pyro_thread_target,
            args=(session_name, api_id, api_hash, session_user_id),
            daemon=True
        )
        t.start()
        self.thread = t
        logger.info("Started PyroListener for user %s", session_user_id)
        return True

    def _pyro_thread_target(self, session_name: str, api_id: int, api_hash: str, session_user_id: int):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.session_user_id = session_user_id

        client = Client(
            session_name,
            api_id=api_id,
            api_hash=api_hash,
            workdir=SESSIONS_DIR
        )
        self.client = client

        async def on_message(c, m):
            try:
                chat = m.chat
                if not chat:
                    return
                ch_username = getattr(chat, "username", None)
                if not ch_username:
                    return
                if not ch_username.startswith("@"):
                    ch_username = "@" + ch_username
                if ch_username not in self.monitored_channels:
                    return
                if getattr(m, "photo", None) or getattr(m, "video", None) or getattr(m, "document", None):
                    return
                raw_text = m.text or m.caption
                if not raw_text:
                    return
                filtered = filter_text_preserve_rules(raw_text)
                if filtered.startswith("❌"):
                    return

                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute(
                    "SELECT target_bot_username FROM channels WHERE user_id = ? AND channel_username = ? LIMIT 1",
                    (session_user_id, ch_username)
                )
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
                    logger.exception("Failed to send message")
            except Exception:
                logger.exception("on_message error")

        client.add_handler(PyroMessageHandler(on_message, py_filters.all))

        try:
            loop.run_until_complete(client.start())
            self.running = True
            logger.info("Pyrogram client started.")
            loop.run_until_complete(client.idle())
        except Exception:
            logger.exception("Pyrogram error")
        finally:
            try:
                loop.run_until_complete(client.stop())
            except Exception:
                pass
            self.running = False

    def stop(self):
        if self.client and self.loop:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.client.stop(), self.loop)
                fut.result(timeout=10)
            except Exception:
                logger.exception("Error stopping client")
        self.client = None
        self.loop = None
        self.thread = None
        self.running = False

    def reload_monitored_channels_for_current_session(self):
        if not self.session_user_id:
            return
        rows = list_channels_db(self.session_user_id)
        mon = set()
        for r in rows:
            ch = r[1]
            if ch and not ch.startswith("@"):
                ch = "@" + ch
            mon.add(ch)
        self.monitored_channels = mon

pyro_listener = PyroListener()

# ===================== Telegram bot UI & Handlers remain unchanged =====================

# (كل الأكواد التالية نفسها دون أي تعديل)
# — start_cmd
# — pressed_button
# — text_message
# — main()

# ---------- Telegram UI ----------
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع جلسة", callback_data="upload_session")],
        [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
        [InlineKeyboardButton("🗑️ حذف قناة", callback_data="delete_channel")],
        [InlineKeyboardButton("📜 عرض القنوات", callback_data="list_channels")],
        [InlineKeyboardButton("🔐 إضافة API", callback_data="add_api")],
        [InlineKeyboardButton("👀 عرض API", callback_data="view_api")],
        [InlineKeyboardButton("🔁 إعادة تشغيل المستمع", callback_data="restart_listener")],
    ])

# ... (الباقي من الكود غير معدل وموجود كما هو)

# ---------- Start application (webhook) ----------
def main():
    init_db()
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN env missing")
        return

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CallbackQueryHandler(pressed_button))
    application.add_handler(MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), text_message))

    last = get_last_session_row()
    if last:
        try:
            pyro_listener.start_with_session_row(last)
        except Exception:
            logger.exception("Failed to start listener")

    if not WEBHOOK_URL:
        application.run_polling()
        return

    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
    )

if __name__ == "__main__":
    main()
