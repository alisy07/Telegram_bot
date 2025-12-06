# main.py
import os
import sqlite3
import base64
import logging
import threading
import asyncio
import re
from typing import Optional, List, Tuple, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# Pyrogram (user client)
from pyrogram import Client, filters as py_filters
from pyrogram.handlers import MessageHandler as PyroMessageHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============ إعدادات عامة ============
DB_FILE = "bot_data.db"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ADMIN ID
ADMIN_ID = 1037850299

# ============ DB helpers ============
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS apis (
            user_id INTEGER PRIMARY KEY,
            api_id TEXT,
            api_hash TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_username TEXT,
            target_bot_username TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            data_b64 TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def save_api(user_id: int, api_id: str, api_hash: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO apis(user_id, api_id, api_hash) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET api_id=excluded.api_id, api_hash=excluded.api_hash;",
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


def add_channel_db(user_id: int, channel: str, target_bot: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO channels(user_id, channel_username, target_bot_username) VALUES(?,?,?)",
        (user_id, channel, target_bot),
    )
    conn.commit()
    conn.close()


def list_channels_db(user_id: int) -> List[Tuple[int, str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, channel_username, target_bot_username FROM channels WHERE user_id = ?",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def list_all_channels_db() -> List[Tuple[int, int, str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, channel_username, target_bot_username FROM channels"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_channel_db(channel_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()


def save_session_db(user_id: int, filename: str, data_b64: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions(user_id, filename, data_b64) VALUES(?,?,?)",
        (user_id, filename, data_b64),
    )
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


def list_all_sessions_db() -> List[Tuple[int, int, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, filename FROM sessions")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_user_ids() -> Set[int]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    ids = set()
    cur.execute("SELECT user_id FROM apis")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    cur.execute("SELECT DISTINCT user_id FROM channels")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    cur.execute("SELECT DISTINCT user_id FROM sessions")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    conn.close()
    return ids


def list_users_db() -> List[int]:
    return sorted(list(get_all_user_ids()))


# ============ فلتر النصوص ============
def filter_text_preserve_rules(text: str) -> str:
    arabic_pattern = re.compile(
        r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]'
    )
    text = arabic_pattern.sub("", text)
    text = re.sub(r"(?i)code", "", text)
    link_pattern = re.compile(
        r'(https?://\S+)|www\.\S+|t\.me/\S+|telegram\.me/\S+|\bhttps?:\S+'
    )
    text = link_pattern.sub("", text)
    text = re.sub(r'(?<![A-Za-z])\d+(?![A-Za-z])', '', text)
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return "❌ لا يبقى نص قابل للإرسال بعد عملية الفلترة."
    return text


# ============ Pyrogram Listener ============
class PyroListener:
    def __init__(self):
        self.thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Optional[Client] = None
        self.running = False
        self.session_user_id: Optional[int] = None
        self.monitored_channels: Set[str] = set()

    def _write_session_file(self, filename: str, b64data: str) -> str:
        path = os.path.join(SESSIONS_DIR, filename)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64data))
        return path

    def _pyro_thread_target(self, session_path: str, api_id: int, api_hash: str, session_user_id: int):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        client = Client(
            session_name=session_path,
            api_id=api_id,
            api_hash=api_hash,
            workdir=SESSIONS_DIR
        )
        self.client = client
        self.session_user_id = session_user_id

        async def on_message(client_obj, message):
            try:
                chat = message.chat
                if not chat or chat.type not in ["channel", "supergroup", "group"]:
                    return
                ch_username = getattr(chat, "username", None)
                if not ch_username:
                    return
                if not ch_username.startswith("@"):
                    ch_username = "@" + ch_username
                if ch_username not in self.monitored_channels:
                    return
                if message.photo or message.video or message.document or message.audio or message.animation or message.voice or message.sticker:
                    return
                raw_text = message.text or message.caption
                if not raw_text:
                    return
                filtered = filter_text_preserve_rules(raw_text)
                if not filtered or filtered.startswith("❌ لا يبقى"):
                    return
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute(
                    "SELECT target_bot_username FROM channels WHERE user_id = ? AND channel_username = ? LIMIT 1",
                    (session_user_id, ch_username),
                )
                row = cur.fetchone()
                conn.close()
                if not row:
                    return
                target_bot = row[0]
                if not target_bot.startswith("@"):
                    target_bot = "@" + target_bot
                try:
                    await client_obj.send_message(target_bot, filtered)
                    logger.info("Forwarded filtered text from %s to %s", ch_username, target_bot)
                except Exception:
                    pass
            except Exception:
                logger.exception("Error in on_message handler")

        client.add_handler(PyroMessageHandler(on_message, py_filters.all))

        try:
            loop.run_until_complete(client.start())
            self.running = True
            loop.run_until_complete(client.idle())
        except Exception:
            logger.exception("Pyrogram client error")
        finally:
            try:
                loop.run_until_complete(client.stop())
            except:
                pass
            self.running = False

    def start_with_session_row(self, session_row):
        if not session_row:
            return False
        sid, user_id, filename, data_b64 = session_row
        api = get_api(user_id)
        if not api:
            return False
        api_id, api_hash = api
        session_path = self._write_session_file(filename, data_b64)
        self.stop()
        rows = list_channels_db(user_id)
        mon = set()
        for r in rows:
            ch = r[1]
            if not ch.startswith("@"):
                ch = "@" + ch
            mon.add(ch)
        self.monitored_channels = mon
        t = threading.Thread(target=self._pyro_thread_target, args=(session_path, int(api_id), api_hash, user_id), daemon=True)
        t.start()
        self.thread = t
        return True

    def stop(self):
        try:
            if self.client and self.loop:
                fut = asyncio.run_coroutine_threadsafe(self.client.stop(), self.loop)
                fut.result(timeout=10)
        except:
            pass
        try:
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
        except:
            pass
        if self.thread:
            self.thread.join(timeout=5)
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
            if not ch.startswith("@"):
                ch = "@" + ch
            mon.add(ch)
        self.monitored_channels = mon


pyro_listener = PyroListener()


# ============ Telegram helper ============
def main_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 رفع جلسة", callback_data="upload_session")],
            [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
            [InlineKeyboardButton("🗑️ حذف قناة", callback_data="delete_channel")],
            [InlineKeyboardButton("📜 عرض القنوات", callback_data="list_channels")],
            [InlineKeyboardButton("🔐 إضافة API (api_id / api_hash)", callback_data="add_api")],
            [InlineKeyboardButton("👀 عرض API الخاص بي", callback_data="view_api")],
            [InlineKeyboardButton("📁 عرض الجلسات", callback_data="list_sessions")],
            [InlineKeyboardButton("🔁 إعادة تشغيل المستمع", callback_data="restart_listener")],
        ]
    )


def admin_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 عرض المستخدمين", callback_data="admin_list_users")],
            [InlineKeyboardButton("🔐 عرض كل APIs", callback_data="admin_list_apis")],
            [InlineKeyboardButton("📜 عرض كل القنوات", callback_data="admin_list_channels")],
            [InlineKeyboardButton("📁 عرض كل الجلسات", callback_data="admin_list_sessions")],
            [InlineKeyboardButton("📢 رسالة جماعية (broadcast)", callback_data="admin_broadcast")],
            [InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats")],
        ]
    )


# ============ safe edit ============
async def safe_edit(query, text, markup=None):
    try:
        old_text = query.message.text or ""
        if old_text.strip() == text.strip():
            return
        await query.edit_message_text(text, reply_markup=markup)
    except Exception as e:
        print("edit_message error:", e)


# ============ shutdown pyrogram ============
async def shutdown_pyrogram(listener):
    try:
        if listener.client:
            await listener.client.stop()
    except:
        pass
    try:
        if listener.loop:
            listener.loop.stop()
    except:
        pass


# ============ Handlers (simplified: pressed_button / text_message / file_upload) ============
# ... ضع جميع Handlers كما هي مع استبدال جميع edit_message_text بـ safe_edit(...) ...
# مثال لاستبدال:
# await q.edit_message_text("...", reply_markup=main_menu())
# استبدله بـ:
# await safe_edit(q, "...", main_menu())


# ============ تشغيل Webhook ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(pressed_button))
    app.add_handler(MessageHandler(filters.Document.ALL, file_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    last = get_last_session_row()
    if last:
        pyro_listener.start_with_session_row(last)
        logger.info("Pyrogram listener started at bot startup.")

    app.run_webhook(
        listen="0.0.0.0",
        port=10000,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        allowed_updates=None,
    )


if __name__ == "__main__":
    main()
