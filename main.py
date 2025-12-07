# main.py
#!/usr/bin/env python3
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

# Pyrogram user client (v2+)
from pyrogram import Client, filters as py_filters
from pyrogram.handlers import MessageHandler as PyroMessageHandler

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- Config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # optional (for webhook); if None the script will still run if you change to polling
ADMIN_ID = 1037850299  # keep your admin id
DB_FILE = "bot_data.db"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ---------------- DB helpers ----------------
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS apis (
        user_id INTEGER PRIMARY KEY,
        api_id TEXT,
        api_hash TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        channel_username TEXT,
        target_bot_username TEXT
    );
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        filename TEXT,
        data_b64 TEXT
    );
    """)
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
    ids.update([r[0] for r in cur.fetchall() if r[0]])
    cur.execute("SELECT DISTINCT user_id FROM channels")
    ids.update([r[0] for r in cur.fetchall() if r[0]])
    cur.execute("SELECT DISTINCT user_id FROM sessions")
    ids.update([r[0] for r in cur.fetchall() if r[0]])
    conn.close()
    return ids

def list_users_db() -> List[int]:
    return sorted(list(get_all_user_ids()))

# ---------------- Text filter (your rules) ----------------
def filter_text_preserve_rules(text: str) -> str:
    # remove Arabic
    text = re.sub(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', '', text)
    # remove "code" (case-insensitive)
    text = re.sub(r'(?i)code', '', text)
    # remove links (http, www, t.me, telegram.me)
    text = re.sub(r'(https?://\S+)|www\.\S+|t\.me/\S+|telegram\.me/\S+', '', text)
    # remove digits that are not adjacent to ascii letters
    text = re.sub(r'(?<![A-Za-z])\d+(?![A-Za-z])', '', text)
    # remove symbols except underscores and spaces and ascii letters/digits
    text = re.sub(r'[^\w\s]', '', text)
    # compress spaces
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        return "❌ لا يبقى نص قابل للإرسال بعد عملية الفلترة."
    return text

# ---------------- Pyrogram listener (single global) ----------------
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
        """
        Runs inside a dedicated thread with its own asyncio loop.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        # IMPORTANT: use first arg = session_path (pyrogram v2+)
        client = Client(session_path, api_id=api_id, api_hash=api_hash, workdir=SESSIONS_DIR)
        self.client = client
        self.session_user_id = session_user_id

        async def on_message(client_obj, message):
            try:
                chat = message.chat
                if not chat:
                    return
                # only channel messages that have username (we monitor by @username)
                if chat.type != "channel" and chat.type != "supergroup" and chat.type != "group":
                    return
                ch_username = getattr(chat, "username", None)
                if not ch_username:
                    return
                if not ch_username.startswith("@"):
                    ch_username = "@" + ch_username
                if ch_username not in self.monitored_channels:
                    return
                # ignore media (we only send text)
                if getattr(message, "media", None):
                    logger.debug("Ignoring media message from %s", ch_username)
                    return
                raw_text = message.text or message.caption
                if not raw_text:
                    return
                filtered = filter_text_preserve_rules(raw_text)
                if not filtered or filtered.startswith("❌"):
                    return
                # find target bot for that channel and session owner
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute(
                    "SELECT target_bot_username FROM channels WHERE user_id = ? AND channel_username = ? LIMIT 1",
                    (session_user_id, ch_username),
                )
                row = cur.fetchone()
                conn.close()
                if not row:
                    logger.info("No target configured for %s", ch_username)
                    return
                target_bot = row[0]
                if not target_bot.startswith("@"):
                    target_bot = "@" + target_bot
                # send as user via pyrogram to @target_bot
                try:
                    await client_obj.send_message(target_bot, filtered)
                    logger.info("Forwarded filtered text from %s to %s", ch_username, target_bot)
                except Exception as e:
                    logger.exception("Failed sending to target %s: %s", target_bot, e)
            except Exception:
                logger.exception("Error in on_message handler")

        # register handler
        client.add_handler(PyroMessageHandler(on_message, py_filters.all))

        try:
            loop.run_until_complete(client.start())
            self.running = True
            logger.info("Pyrogram client started (user_id=%s). Monitoring: %s", session_user_id, self.monitored_channels)
            loop.run_until_complete(client.idle())
        except Exception:
            logger.exception("Pyrogram client error")
        finally:
            try:
                loop.run_until_complete(client.stop())
            except Exception:
                pass
            self.running = False
            logger.info("Pyrogram client stopped.")

    def start_with_session_row(self, session_row) -> bool:
        """
        session_row: (id, user_id, filename, data_b64)
        """
        if not session_row:
            return False
        sid, user_id, filename, data_b64 = session_row
        api = get_api(user_id)
        if not api:
            logger.error("No api_id/api_hash for session owner %s", user_id)
            return False
        api_id, api_hash = api
        session_path = self._write_session_file(filename, data_b64)
        # stop running one first
        self.stop()
        # load monitored channels for this user
        rows = list_channels_db(user_id)
        mon = set()
        for r in rows:
            ch = r[1]
            if ch and not ch.startswith("@"):
                ch = "@" + ch
            mon.add(ch)
        self.monitored_channels = mon
        # launch thread
        t = threading.Thread(target=self._pyro_thread_target, args=(session_path, int(api_id), api_hash, user_id), daemon=True)
        t.start()
        self.thread = t
        logger.info("PyroListener: started thread for session user %s", user_id)
        return True

    def stop(self):
        if not self.thread or not self.running:
            # nothing to stop
            self.client = None
            self.loop = None
            self.thread = None
            self.running = False
            return
        try:
            if self.client and self.loop:
                fut = asyncio.run_coroutine_threadsafe(self.client.stop(), self.loop)
                fut.result(timeout=15)
        except Exception:
            logger.exception("Error stopping pyrogram client")
        finally:
            try:
                if self.loop and self.loop.is_running():
                    self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass
            try:
                if self.thread:
                    self.thread.join(timeout=5)
            except Exception:
                pass
            self.client = None
            self.loop = None
            self.thread = None
            self.running = False
            logger.info("PyroListener stopped and cleaned up.")

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
        logger.info("Reloaded monitored channels: %s", self.monitored_channels)

pyro_listener = PyroListener()

# ---------------- Telegram UI helpers ----------------
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع جلسة", callback_data="upload_session")],
        [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
        [InlineKeyboardButton("🗑️ حذف قناة", callback_data="delete_channel")],
        [InlineKeyboardButton("📜 عرض القنوات", callback_data="list_channels")],
        [InlineKeyboardButton("🔐 إضافة API (api_id api_hash)", callback_data="add_api")],
        [InlineKeyboardButton("👀 عرض API", callback_data="view_api")],
        [InlineKeyboardButton("📁 عرض الجلسات", callback_data="list_sessions")],
        [InlineKeyboardButton("🔁 إعادة تشغيل المستمع", callback_data="restart_listener")],
    ])

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 عرض المستخدمين", callback_data="admin_list_users")],
        [InlineKeyboardButton("🔐 عرض كل APIs", callback_data="admin_list_apis")],
        [InlineKeyboardButton("📜 عرض كل القنوات", callback_data="admin_list_channels")],
        [InlineKeyboardButton("📁 عرض كل الجلسات", callback_data="admin_list_sessions")],
        [InlineKeyboardButton("📢 بث رسالة", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats")],
    ])

# avoid BadRequest when editing same content
async def safe_edit(query, text, markup=None):
    try:
        old = query.message.text or ""
        if old.strip() == text.strip():
            return
        await query.edit_message_text(text, reply_markup=markup)
    except Exception as e:
        logger.debug("safe_edit error: %s", e)

# ---------------- Handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid == ADMIN_ID:
        await update.message.reply_text("أهلاً بالمشرف — القائمة:", reply_markup=admin_menu())
    else:
        await update.message.reply_text("أهلاً — القائمة:", reply_markup=main_menu())

# pressing buttons
async def pressed_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    # restart listener using latest session
    if data == "restart_listener":
        last = get_last_session_row()
        if not last:
            await safe_edit(q, "❌ لا توجد جلسة لتشغيل المستمع.", main_menu())
            return
        ok = pyro_listener.start_with_session_row(last)
        if ok:
            await safe_edit(q, "🔁 تم إعادة تشغيل المستمع باستخدام آخر جلسة.", main_menu())
        else:
            await safe_edit(q, "❌ فشل تشغيل المستمع — تأكد من api_id/api_hash لمالك الجلسة.", main_menu())
        return

    # upload session: set flag to expect document
    if data == "upload_session":
        context.user_data["awaiting_session"] = True
        await safe_edit(q, "📤 أرسل الآن ملف الجلسة (.session) كملف وثيقة.", main_menu())
        return

    # add channel: expect "channel target" text
    if data == "add_channel":
        context.user_data["awaiting_channel"] = True
        await safe_edit(q, "➕ أرسل اسم القناة واسم بوت الهدف مفصول بمسافة (مثال: @chan @target_bot).", main_menu())
        return

    # delete channel: show list with inline buttons
    if data == "delete_channel":
        rows = list_channels_db(uid)
        if not rows:
            await safe_edit(q, "❌ لا توجد قنوات لديك للحذف.", main_menu())
            return
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"{r[1]} -> {r[2]}", callback_data=f"del:{r[0]}")] for r in rows])
        await safe_edit(q, "اختر قناة للحذف:", kb)
        return

    if data.startswith("del:"):
        try:
            cid = int(data.split(":", 1)[1])
            delete_channel_db(cid)
            # reload list if running session belongs to same user
            pyro_listener.reload_monitored_channels_for_current_session()
            await safe_edit(q, "✔️ تم حذف القناة.", main_menu())
        except Exception:
            await safe_edit(q, "❌ خطأ أثناء الحذف.", main_menu())
        return

    # list channels
    if data == "list_channels":
        rows = list_channels_db(uid)
        if not rows:
            await safe_edit(q, "❌ لا توجد قنوات مسجلة لديك.", main_menu())
            return
        text = "📜 قنواتك:\n" + "\n".join([f"- id:{r[0]} {r[1]} -> {r[2]}" for r in rows])
        await safe_edit(q, text, main_menu())
        return

    # add api
    if data == "add_api":
        context.user_data["awaiting_api"] = True
        await safe_edit(q, "🔐 أرسل الآن: api_id api_hash", main_menu())
        return

    # view api
    if data == "view_api":
        api = get_api(uid)
        if not api:
            await safe_edit(q, "❌ لم تقم بتسجيل API بعد.", main_menu())
        else:
            await safe_edit(q, f"🔐 api_id: `{api[0]}`\napi_hash: `{api[1]}`", main_menu())
        return

    # list sessions
    if data == "list_sessions":
        rows = list_sessions_db(uid)
        if not rows:
            await safe_edit(q, "❌ لا توجد جلسات محفوظة.", main_menu())
            return
        text = "📁 جلساتك:\n" + "\n".join([f"- id:{r[0]} file:{r[1]}" for r in rows])
        await safe_edit(q, text, main_menu())
        return

    # Admin-only handlers
    if uid == ADMIN_ID:
        if data == "admin_list_users":
            users = list_users_db()
            if not users:
                await safe_edit(q, "❌ لا يوجد مستخدمون.", admin_menu())
                return
            await safe_edit(q, "👥 المستخدمون:\n" + "\n".join(map(str, users)), admin_menu())
            return

        if data == "admin_list_apis":
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT user_id, api_id, api_hash FROM apis")
            rows = cur.fetchall()
            conn.close()
            if not rows:
                await safe_edit(q, "❌ لا توجد APIs.", admin_menu())
                return
            text = "🔐 APIs:\n" + "\n".join([f"- {r[0]}: {r[1]} | {r[2]}" for r in rows])
            await safe_edit(q, text, admin_menu())
            return

        if data == "admin_list_channels":
            rows = list_all_channels_db()
            if not rows:
                await safe_edit(q, "❌ لا توجد قنوات.", admin_menu())
                return
            text = "📜 كل القنوات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} {r[2]} -> {r[3]}" for r in rows])
            await safe_edit(q, text, admin_menu())
            return

        if data == "admin_list_sessions":
            rows = list_all_sessions_db()
            if not rows:
                await safe_edit(q, "❌ لا توجد جلسات.", admin_menu())
                return
            text = "📁 كل الجلسات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} file:{r[2]}" for r in rows])
            await safe_edit(q, text, admin_menu())
            return

    # fallback
    await safe_edit(q, "تمت العملية.", main_menu())

# process incoming text/files depending on awaiting flags
async def text_and_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # upload session (document)
    if context.user_data.get("awaiting_session"):
        doc = update.message.document
        if not doc:
            await update.message.reply_text("❌ الرجاء إرسال الملف كوثيقة (.session).", reply_markup=main_menu())
            context.user_data["awaiting_session"] = False
            return
        file_obj = await doc.get_file()
        raw = await file_obj.download_as_bytearray()
        b64 = base64.b64encode(raw).decode()
        filename = doc.file_name
        save_session_db(uid, filename, b64)
        # optional local copy
        try:
            with open(os.path.join(SESSIONS_DIR, filename), "wb") as f:
                f.write(base64.b64decode(b64))
        except Exception:
            pass
        context.user_data["awaiting_session"] = False
        await update.message.reply_text("✔️ تم حفظ الجلسة.", reply_markup=main_menu())
        return

    # add channel: expect two parts
    if context.user_data.get("awaiting_channel"):
        text = (update.message.text or "").strip()
        parts = text.split(None, 1)
        if len(parts) != 2:
            await update.message.reply_text("❌ الصيغة خاطئة. أرسل: @channel @target_bot", reply_markup=main_menu())
            context.user_data["awaiting_channel"] = False
            return
        ch, target = parts
        if not ch.startswith("@"):
            ch = "@" + ch
        if not target.startswith("@"):
            target = "@" + target
        add_channel_db(uid, ch, target)
        context.user_data["awaiting_channel"] = False
        # if current listener belongs to this user, reload channels
        pyro_listener.reload_monitored_channels_for_current_session()
        await update.message.reply_text(f"✔️ تم إضافة القناة {ch} -> {target}", reply_markup=main_menu())
        return

    # add api: expect "api_id api_hash"
    if context.user_data.get("awaiting_api"):
        text = (update.message.text or "").strip()
        parts = text.split(None, 1)
        if len(parts) != 2:
            await update.message.reply_text("❌ الصيغة خاطئة. أرسل: api_id api_hash", reply_markup=main_menu())
            context.user_data["awaiting_api"] = False
            return
        api_id, api_hash = parts
        save_api(uid, api_id, api_hash)
        context.user_data["awaiting_api"] = False
        await update.message.reply_text("✔️ تم حفظ API_ID و API_HASH.", reply_markup=main_menu())
        return

    # default: show menu
    await update.message.reply_text("استخدم الأزرار للقيام بعمليات (رفع جلسة، إضافة قناة، ...).", reply_markup=main_menu())

# ----------------- Run bot -----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(pressed_button))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.TEXT & ~filters.COMMAND, text_and_files))

    # auto-start pyro listener if a session exists
    last = get_last_session_row()
    if last:
        ok = pyro_listener.start_with_session_row(last)
        if ok:
            logger.info("Started Pyrogram listener at startup using last session row.")
        else:
            logger.warning("Failed to start Pyrogram listener at startup (missing api id/hash?)")

    # choose run mode: webhook if WEBHOOK_URL provided else polling (Render typically uses webhook, but polling also works)
    if WEBHOOK_URL:
        logger.info("Starting webhook mode")
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            allowed_updates=None,
        )
    else:
        logger.info("Starting polling mode (WEBHOOK_URL not set)")
        app.run_polling()

if __name__ == "__main__":
    main()
