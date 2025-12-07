#!/usr/bin/env python3
# main.py — Final integrated Telegram control bot + Pyrogram listener (SQLite)
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

BOT_TOKEN = os.getenv("BOT_TOKEN")  # ensure set in env
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # optional
PORT = int(os.environ.get("PORT", 10000))

ADMIN_ID = 1037850299  # update if needed

# paths
DB_FILE = "bot_data.db"
SESSIONS_DIR = "/opt/render/project/src/sessions"  # fixed directory per your request
os.makedirs(SESSIONS_DIR, exist_ok=True)
SESSION_FILE = os.path.join(SESSIONS_DIR, "listener.session")  # final session path used by listener

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

def list_all_sessions_db() -> List[Tuple[int, int, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, filename FROM sessions")
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

# ---------------- Filtering ----------------
def filter_text_preserve_rules(text: str) -> str:
    # remove Arabic letters
    text = re.sub(r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]', '', text)
    # remove 'code' case-insensitive
    text = re.sub(r'(?i)code', '', text)
    # remove links
    text = re.sub(r'(https?://\S+)|www\.\S+|t\.me/\S+|telegram\.me/\S+', '', text)
    # remove numbers not adjacent to ascii letters
    text = re.sub(r'(?<![A-Za-z])\d+(?![A-Za-z])', '', text)
    # remove symbols (keep underscore, alnum, whitespace)
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

    def _write_session_file(self, filename: str, b64data: str) -> str:
        # write original under sessions/filename and also write final listener.session
        dest = os.path.join(SESSIONS_DIR, filename)
        tmp_dest = dest + ".tmp"
        with open(tmp_dest, "wb") as f:
            f.write(base64.b64decode(b64data))
        os.replace(tmp_dest, dest)
        # also write/replace final SESSION_FILE
        tmp_final = SESSION_FILE + ".tmp"
        with open(tmp_final, "wb") as f:
            f.write(base64.b64decode(b64data))
        os.replace(tmp_final, SESSION_FILE)
        return SESSION_FILE

    def _pyro_thread_target(self, session_path: str, api_id: int, api_hash: str, session_user_id: int):
        """
        Run Pyrogram client in separate thread + event loop
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self.session_user_id = session_user_id

        # create client; pass session path as first positional arg (works across pyrogram versions)
        client = Client(session_path, api_id=api_id, api_hash=api_hash, workdir=SESSIONS_DIR)
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
                # ignore media (per your rule)
                if getattr(m, "photo", None) or getattr(m, "video", None) or getattr(m, "document", None) or getattr(m, "audio", None) or getattr(m, "animation", None) or getattr(m, "voice", None) or getattr(m, "sticker", None):
                    logger.debug("Ignoring media from %s", ch_username)
                    return
                raw_text = m.text or m.caption
                if not raw_text:
                    return
                filtered = filter_text_preserve_rules(raw_text)
                if filtered.startswith("❌"):
                    return
                # lookup target bot for this channel and session owner
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute("SELECT target_bot_username FROM channels WHERE user_id = ? AND channel_username = ? LIMIT 1", (session_user_id, ch_username))
                row = cur.fetchone()
                conn.close()
                if not row:
                    logger.info("No target configured for %s", ch_username)
                    return
                target_bot = row[0]
                if not target_bot.startswith("@"):
                    target_bot = "@" + target_bot
                try:
                    await c.send_message(target_bot, filtered)
                    logger.info("Forwarded filtered text from %s to %s", ch_username, target_bot)
                except Exception:
                    logger.exception("Failed to send message to target bot %s", target_bot)
            except Exception:
                logger.exception("on_message handler error")

        client.add_handler(PyroMessageHandler(on_message, py_filters.all))

        try:
            loop.run_until_complete(client.start())
            self.running = True
            logger.info("Pyrogram client started for user %s monitoring %s", session_user_id, self.monitored_channels)
            loop.run_until_complete(client.idle())
        except EOFError:
            logger.error("Pyrogram attempted interactive authorization (asked for phone/token). Session invalid or incomplete.")
        except py_errors.RPCError as rpc_e:
            logger.exception("Pyrogram RPC error while starting: %s", rpc_e)
        except sqlite3.OperationalError as sql_e:
            logger.exception("SQLite OperationalError while Pyrogram opening session DB: %s", sql_e)
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
        session_row: (id,user_id,filename,data_b64)
        writes session to disk (listener.session) and starts listener if api exists.
        """
        if not session_row:
            return False
        sid, user_id, filename, data_b64 = session_row
        api = get_api(user_id)
        if not api:
            logger.error("No API credentials for session owner %s", user_id)
            return False
        api_id, api_hash = api
        # write session file(s)
        session_path = self._write_session_file(filename, data_b64)
        # start thread
        return self.start_with_session_file(session_path, int(api_id), api_hash, user_id)

    def start_with_session_file(self, session_path: str, api_id: int, api_hash: str, session_user_id: int):
        # stop existing
        self.stop()
        # load monitored channels for that user
        rows = list_channels_db(session_user_id)
        mon = set()
        for r in rows:
            ch = r[1]
            if ch and not ch.startswith("@"):
                ch = "@" + ch
            if ch:
                mon.add(ch)
        self.monitored_channels = mon
        self.session_user_id = session_user_id
        # spawn thread
        t = threading.Thread(target=self._pyro_thread_target, args=(session_path, api_id, api_hash, session_user_id), daemon=True)
        t.start()
        self.thread = t
        logger.info("Started PyroListener thread for user %s", session_user_id)
        return True

    def stop(self):
        if self.client and self.loop:
            try:
                fut = asyncio.run_coroutine_threadsafe(self.client.stop(), self.loop)
                fut.result(timeout=15)
            except Exception:
                logger.exception("Error stopping pyrogram client")
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
            if ch:
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
        [InlineKeyboardButton("📜 عرض قنواتي", callback_data="list_channels")],
        [InlineKeyboardButton("🔐 إضافة API", callback_data="add_api")],
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
    ])

async def safe_edit(query, text, markup=None):
    try:
        old = ""
        try:
            old = (query.message.text or "").strip()
        except Exception:
            old = ""
        if old == text.strip():
            return
        await query.edit_message_text(text, reply_markup=markup)
    except Exception as e:
        logger.debug("safe_edit failed: %s", e)

# ---------------- Handlers ----------------
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    menu = admin_menu() if uid == ADMIN_ID else main_menu()
    await update.message.reply_text("مرحباً — اختر من القائمة:", reply_markup=menu)

async def pressed_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data_cb = q.data

    # upload session start
    if data_cb == "upload_session":
        context.user_data["awaiting_session"] = True
        await safe_edit(q, "📤 أرسل الآن ملف الجلسة (.session) كوثيقة.", main_menu())
        return

    # add channel: user will send "<@channel> <@target_bot>"
    if data_cb == "add_channel":
        context.user_data["awaiting_channel"] = True
        await safe_edit(q, "➕ أرسل الآن: @channel_username @target_bot_username", main_menu())
        return

    # delete channel (list user's channels)
    if data_cb == "delete_channel":
        rows = list_channels_db(uid)
        if not rows:
            await safe_edit(q, "❌ لا توجد قنوات للحذف.", main_menu())
            return
        buttons = [[InlineKeyboardButton(f"{r[1]} → {r[2]}", callback_data=f"del:{r[0]}")] for r in rows]
        await safe_edit(q, "اختر القناة للحذف:", InlineKeyboardMarkup(buttons))
        return

    if data_cb and data_cb.startswith("del:"):
        try:
            cid = int(data_cb.split(":",1)[1])
            delete_channel_db(cid)
            pyro_listener.reload_monitored_channels_for_current_session()
            await safe_edit(q, "✔️ تم حذف القناة.", main_menu())
        except Exception:
            await safe_edit(q, "❌ خطأ أثناء الحذف.", main_menu())
        return

    # list channels
    if data_cb == "list_channels":
        rows = list_channels_db(uid)
        if not rows:
            await safe_edit(q, "❌ لا توجد قنوات مسجلة لديك.", main_menu())
            return
        text = "📜 قنواتك:\n" + "\n".join([f"- id:{r[0]} {r[1]} -> {r[2]}" for r in rows])
        await safe_edit(q, text, main_menu())
        return

    # add api
    if data_cb == "add_api":
        context.user_data["awaiting_api"] = True
        await safe_edit(q, "🔐 أرسل الآن: api_id api_hash", main_menu())
        return

    # view api
    if data_cb == "view_api":
        api = get_api(uid)
        if not api:
            await safe_edit(q, "❌ لم تُسجّل API بعد.", main_menu())
        else:
            await safe_edit(q, f"🔐 api_id: `{api[0]}`\napi_hash: `{api[1]}`", main_menu())
        return

    # list sessions (user's sessions)
    if data_cb == "list_sessions":
        rows = list_sessions_db(uid)
        if not rows:
            await safe_edit(q, "❌ لا توجد جلسات محفوظة.", main_menu())
            return
        text = "📁 جلساتك:\n" + "\n".join([f"- id:{r[0]} file:{r[1]}" for r in rows])
        await safe_edit(q, text, main_menu())
        return

    # restart listener using last session (if api exists)
    if data_cb == "restart_listener":
        last = get_last_session_row()
        if not last:
            await safe_edit(q, "❌ لا توجد جلسة لتشغيل المستمع.", main_menu())
            return
        api = get_api(last[1])
        if not api:
            await safe_edit(q, "❌ لا توجد API مسجلة لصاحب الجلسة.", main_menu())
            return
        ok = pyro_listener.start_with_session_row(last)
        if ok:
            await safe_edit(q, "🔁 تم إعادة تشغيل المستمع باستخدام آخر جلسة.", main_menu())
        else:
            await safe_edit(q, "❌ فشل تشغيل المستمع.", main_menu())
        return

    # admin shortcuts
    if uid == ADMIN_ID:
        if data_cb == "admin_list_users":
            users = list_users_db()
            await safe_edit(q, "👥 المستخدمون:\n" + ("\n".join(map(str,users)) if users else "لا يوجد"), admin_menu())
            return
        if data_cb == "admin_list_apis":
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT user_id, api_id, api_hash FROM apis")
            rows = cur.fetchall()
            conn.close()
            text = "🔐 APIs:\n" + ("\n".join([f"- {r[0]}: {r[1]} | {r[2]}" for r in rows]) if rows else "لا يوجد")
            await safe_edit(q, text, admin_menu())
            return
        if data_cb == "admin_list_channels":
            rows = list_all_channels_db()
            text = "📜 جميع القنوات:\n" + ("\n".join([f"- id:{r[0]} user:{r[1]} {r[2]} -> {r[3]}" for r in rows]) if rows else "لا يوجد")
            await safe_edit(q, text, admin_menu())
            return
        if data_cb == "admin_list_sessions":
            rows = list_all_sessions_db()
            text = "📁 جميع الجلسات:\n" + ("\n".join([f"- id:{r[0]} user:{r[1]} file:{r[2]}" for r in rows]) if rows else "لا يوجد")
            await safe_edit(q, text, admin_menu())
            return

    await safe_edit(q, "تمّت العملية.", main_menu())

# ---------------- Message handler (flows) ----------------
async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg_text = (update.message.text or "").strip()

    # awaiting upload session (document)
    if context.user_data.get("awaiting_session"):
        doc = update.message.document
        if not doc:
            await update.message.reply_text("❌ أرسل ملف الجلسة كوثيقة (.session).", reply_markup=main_menu())
            context.user_data["awaiting_session"] = False
            return
        file_obj = await doc.get_file()
        raw = await file_obj.download_as_bytearray()
        b64 = base64.b64encode(raw).decode()
        filename = doc.file_name or "listener.session"
        save_session_db(uid, filename, b64)
        # write both original filename copy and final listener.session (atomic)
        try:
            pyro_listener._write_session_file(filename, b64)
        except Exception:
            logger.exception("Failed to write session files.")
            await update.message.reply_text("❌ فشل حفظ الملف محلياً.", reply_markup=main_menu())
            context.user_data["awaiting_session"] = False
            return
        context.user_data["awaiting_session"] = False
        await update.message.reply_text("✔️ تم حفظ الجلسة في قاعدة البيانات وتم وضعها في مسار sessions/.", reply_markup=main_menu())
        # auto-start if api exists for uploader
        api = get_api(uid)
        last = get_last_session_row()
        if api and last:
            started = pyro_listener.start_with_session_row(last)
            if started:
                await update.message.reply_text("🔁 تم تشغيل المستمع باستخدام الجلسة المرفوعة.", reply_markup=main_menu())
            else:
                await update.message.reply_text("⚠️ لم أتمكن من تشغيل المستمع: تحقق من صلاحية الجلسة وبيانات API.", reply_markup=main_menu())
        return

    # awaiting add channel
    if context.user_data.get("awaiting_channel"):
        parts = msg_text.split(None, 1)
        if len(parts) != 2:
            await update.message.reply_text("❌ الصيغة خاطئة. أرسل: @channel_username @target_bot_username", reply_markup=main_menu())
            context.user_data["awaiting_channel"] = False
            return
        channel, target = parts
        if not channel.startswith("@"):
            channel = "@" + channel
        if not target.startswith("@"):
            target = "@" + target
        add_channel_db(uid, channel, target)
        context.user_data["awaiting_channel"] = False
        pyro_listener.reload_monitored_channels_for_current_session()
        await update.message.reply_text(f"✔️ تم إضافة القناة {channel} -> {target}", reply_markup=main_menu())
        return

    # awaiting api
    if context.user_data.get("awaiting_api"):
        parts = msg_text.split(None, 1)
        if len(parts) != 2:
            await update.message.reply_text("❌ الصيغة خاطئة. أرسل: api_id api_hash", reply_markup=main_menu())
            context.user_data["awaiting_api"] = False
            return
        api_id, api_hash = parts
        save_api(uid, api_id, api_hash)
        context.user_data["awaiting_api"] = False
        await update.message.reply_text("✔️ تم حفظ API_ID و API_HASH.", reply_markup=main_menu())
        return

    # default fallback
    await update.message.reply_text("استخدم الأزرار للتنقل أو /start لعرض القائمة.", reply_markup=main_menu())

# ----------------- Start bot -----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(pressed_button))
    app.add_handler(MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), text_message))

    # try to start listener using last saved session (if api exists)
    last = get_last_session_row()
    if last:
        api = get_api(last[1])
        if api:
            try:
                started = pyro_listener.start_with_session_row(last)
                if started:
                    logger.info("Started Pyrogram listener at startup using last session.")
            except Exception:
                logger.exception("Failed to start PyroListener at startup.")

    # choose webhook or polling
    if WEBHOOK_URL:
        logger.info("Starting webhook...")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
            allowed_updates=None,
        )
    else:
        logger.info("Starting polling...")
        app.run_polling()

if __name__ == "__main__":
    main()
