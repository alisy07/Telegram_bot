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
    cur.execute("INSERT INTO sessions(user_id, filename, data_b64) VALUES(?,?,?)",
                (user_id, filename, data_b64))
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
    cur.execute(
        "INSERT INTO channels(user_id, channel_username, target_bot_username) VALUES(?,?,?)",
        (user_id, channel, target_bot),
    )
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


# ---------- Filtering ----------
def filter_text_preserve_rules(text: str) -> str:
    text = re.sub(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]", "", text)
    text = re.sub(r"(?i)code", "", text)
    text = re.sub(r"(https?://\S+)|www\.\S+|t\.me/\S+|telegram\.me/\S+", "", text)
    text = re.sub(r"(?<![A-Za-z])\d+(?![A-Za-z])", "", text)
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return "❌ لا يبقى نص قابل للإرسال بعد الفلترة."
    return text


# ---------- Pyrogram Listener ----------
class PyroListener:
    def __init__(self):
        self.thread = None
        self.loop = None
        self.client = None
        self.running = False
        self.monitored_channels = set()
        self.session_user_id = None

    def _write_session_file(self, filename: str, b64data: str) -> str:
        path = os.path.join(SESSIONS_DIR, filename)
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64data))
        return filename  # MUST return only the name

    def start_with_session_row(self, row) -> bool:
        if not row:
            return False
        session_id, user_id, filename, data_b64 = row
        api = get_api(user_id)
        if not api:
            logger.error("API missing for user %s", user_id)
            return False
        api_id, api_hash = api
        name = self._write_session_file(filename, data_b64)
        return self.start_with_session_file(name, int(api_id), api_hash, user_id)

    def start_with_session_file(self, session_name: str, api_id: int, api_hash: str, user_id: int):
        self.stop()

        rows = list_channels_db(user_id)
        mon = set()
        for r in rows:
            ch = r[1]
            if ch and not ch.startswith("@"):
                ch = "@" + ch
            mon.add(ch)

        self.monitored_channels = mon
        self.session_user_id = user_id

        t = threading.Thread(
            target=self._thread_target,
            args=(session_name, api_id, api_hash, user_id),
            daemon=True,
        )
        t.start()
        self.thread = t
        return True

    def _thread_target(self, session_name, api_id, api_hash, user_id):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

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
                username = getattr(chat, "username", None)
                if not username:
                    return
                if not username.startswith("@"):
                    username = "@" + username
                if username not in self.monitored_channels:
                    return
                raw = m.text or m.caption
                if not raw:
                    return
                filtered = filter_text_preserve_rules(raw)
                if filtered.startswith("❌"):
                    return
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute(
                    "SELECT target_bot_username FROM channels WHERE user_id=? AND channel_username=? LIMIT 1",
                    (user_id, username),
                )
                row = cur.fetchone()
                conn.close()
                if not row:
                    return
                target = row[0]
                if not target.startswith("@"):
                    target = "@" + target
                await c.send_message(target, filtered)
            except Exception:
                logger.exception("error in on_message")

        client.add_handler(PyroMessageHandler(on_message, py_filters.all))

        try:
            loop.run_until_complete(client.start())
            self.running = True
            loop.run_until_complete(client.idle())
        finally:
            try:
                loop.run_until_complete(client.stop())
            except:
                pass
            self.running = False

    def reload_monitored_channels_for_current_session(self):
        if not self.session_user_id:
            return
        rows = list_channels_db(self.session_user_id)
        mon = set()
        for _, ch, _ in rows:
            if ch and not ch.startswith("@"):
                ch = "@" + ch
            mon.add(ch)
        self.monitored_channels = mon

    def stop(self):
        if self.client and self.loop:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.client.stop(), self.loop)
                fut.result(timeout=10)
            except:
                pass
        self.client = None
        self.loop = None
        self.thread = None
        self.running = False


pyro_listener = PyroListener()


# ---------- UI ----------
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


# ---------- Handlers ----------
async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً 👋\nاختر من القائمة:", reply_markup=main_menu())


async def pressed_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "upload_session":
        await q.edit_message_text("📤 أرسل ملف الجلسة (.session)")
        ctx.user_data["awaiting"] = "session_file"

    elif q.data == "add_api":
        await q.edit_message_text("🔐 أرسل API_ID و API_HASH بهذا الشكل:\n12345:abcd1234")
        ctx.user_data["awaiting"] = "api_data"

    elif q.data == "list_channels":
        chs = list_channels_db(q.from_user.id)
        if not chs:
            await q.edit_message_text("لا توجد قنوات.")
        else:
            txt = "📜 القنوات:\n\n"
            for cid, ch, bot in chs:
                txt += f"🆔 {cid}\nقناة: {ch}\nبوت: {bot}\n\n"
            await q.edit_message_text(txt)

    elif q.data == "add_channel":
        await q.edit_message_text("➕ أرسل البيانات هكذا:\n@channel @bot")
        ctx.user_data["awaiting"] = "add_channel"

    elif q.data == "delete_channel":
        chs = list_channels_db(q.from_user.id)
        if not chs:
            await q.edit_message_text("لا توجد قنوات.")
            return
        buttons = [
            [InlineKeyboardButton(f"{cid} - {ch}", callback_data=f"delch:{cid}")]
            for cid, ch, _ in chs
        ]
        await q.edit_message_text("اختر قناة للحذف:", reply_markup=InlineKeyboardMarkup(buttons))

    elif q.data.startswith("delch:"):
        cid = int(q.data.split(":")[1])
        delete_channel_db(cid)
        pyro_listener.reload_monitored_channels_for_current_session()
        await q.edit_message_text("🚮 تم حذف القناة.")

    elif q.data == "view_api":
        api = get_api(q.from_user.id)
        if not api:
            await q.edit_message_text("لا يوجد API.")
        else:
            await q.edit_message_text(f"API_ID: {api[0]}\nAPI_HASH: {api[1]}")

    elif q.data == "restart_listener":
        row = get_last_session_row()
        if not row:
            await q.edit_message_text("لا توجد جلسة.")
            return
        ok = pyro_listener.start_with_session_row(row)
        if ok:
            await q.edit_message_text("🔁 تم إعادة تشغيل المستمع.")
        else:
            await q.edit_message_text("❌ فشل تشغيل المستمع.")


async def text_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    awaiting = ctx.user_data.get("awaiting")
    user = update.message.from_user.id

    # Upload session file
    if awaiting == "session_file" and update.message.document:
        f = update.message.document
        if not f.file_name.endswith(".session"):
            await update.message.reply_text("❌ ملف غير صالح.")
            return
        file = await f.get_file()
        b = await file.download_as_bytearray()
        b64 = base64.b64encode(b).decode()
        save_session_db(user, f.file_name, b64)
        ok = pyro_listener.start_with_session_row(get_last_session_row())
        if ok:
            await update.message.reply_text("تم تشغيل الجلسة ✔️", reply_markup=main_menu())
        ctx.user_data["awaiting"] = None
        return

    # Add API
    if awaiting == "api_data":
        if ":" not in update.message.text:
            await update.message.reply_text("❌ التنسيق خاطئ")
            return
        api_id, api_hash = update.message.text.split(":", 1)
        save_api(user, api_id.strip(), api_hash.strip())
        await update.message.reply_text("تم حفظ API ✔️", reply_markup=main_menu())
        ctx.user_data["awaiting"] = None
        return

    # Add channel
    if awaiting == "add_channel":
        parts = update.message.text.split()
        if len(parts) != 2:
            await update.message.reply_text("❌ أرسل: @channel @bot")
            return
        ch, bot = parts
        add_channel_db(user, ch, bot)
        pyro_listener.reload_monitored_channels_for_current_session()
        await update.message.reply_text("تمت الإضافة ✔️", reply_markup=main_menu())
        ctx.user_data["awaiting"] = None
        return

    await update.message.reply_text("اختر من القائمة:", reply_markup=main_menu())


# ---------- MAIN (webhook) ----------
def main():
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CallbackQueryHandler(pressed_button))
    application.add_handler(MessageHandler(filters.ALL, text_message))

    last = get_last_session_row()
    if last:
        try:
            pyro_listener.start_with_session_row(last)
        except:
            logger.exception("listener failed")

    if WEBHOOK_URL:
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        )
    else:
        application.run_polling()


if __name__ == "__main__":
    main()
