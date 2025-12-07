#!/usr/bin/env python3
# main.py — Integrated Telegram control bot + Pyrogram listener with interactive session creation
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

BOT_TOKEN = os.getenv("BOT_TOKEN")  # required
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # optional
PORT = int(os.environ.get("PORT", 10000))

ADMIN_ID = 1037850299  # change if needed

# paths
DB_FILE = "bot_data.db"
SESSIONS_DIR = "/opt/render/project/src/sessions"  # fixed path for sessions
os.makedirs(SESSIONS_DIR, exist_ok=True)
SESSION_FILE = os.path.join(SESSIONS_DIR, "listener.session")

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
            try:
                chat = m.chat
                if not chat:
                    return
                ch_username = getattr(chat, "username", None)
                if not ch_username or not ch_username.startswith("@"):
                    return
                if ch_username not in self.monitored_channels:
                    return
                # ignore media
                if getattr(m, "photo", None) or getattr(m, "video", None) or getattr(m, "document", None) or getattr(m, "audio", None) or getattr(m, "animation", None) or getattr(m, "voice", None) or getattr(m, "sticker", None):
                    logger.debug("Ignoring media from %s", ch_username)
                    return
                raw_text = m.text or m.caption
                if not raw_text:
                    return
                filtered = filter_text_preserve_rules(raw_text)
                if filtered.startswith("❌"):
                    return
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute("SELECT target_bot_username FROM channels WHERE user_id = ? AND channel_username = ? LIMIT 1", (session_user_id, ch_username))
                row = cur.fetchone()
                conn.close()
                if not row:
                    return
                target_bot = row[0]
                if not target_bot.startswith("@"):
                    target_bot = "@" + target_bot
                try:
                    await c.send_message(target_bot, filtered)
                    logger.info("Forwarded from %s -> %s", ch_username, target_bot)
                except Exception:
                    logger.exception("Failed to send to target bot")
            except Exception:
                logger.exception("on_message handler error")

        client.add_handler(PyroMessageHandler(on_message, py_filters.all))

        try:
            loop.run_until_complete(client.start())
            self.running = True
            logger.info("Pyrogram client started for user %s monitoring %s", session_user_id, self.monitored_channels)
            loop.run_until_complete(client.idle())
        except EOFError:
            logger.error("Pyrogram attempted interactive authorization (asked for phone/token). Session is probably invalid or incomplete. Aborting start.")
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

    def start_with_session_file(self, session_path: str, api_id: int, api_hash: str, session_user_id: int):
        self.stop()
        t = threading.Thread(target=self._pyro_thread_target, args=(session_path, api_id, api_hash, session_user_id), daemon=True)
        t.start()
        self.thread = t
        # reload channels for that user
        rows = list_channels_db(session_user_id)
        mon = set()
        for r in rows:
            ch = r[1]
            if ch and not ch.startswith("@"):
                ch = "@" + ch
            mon.add(ch)
        self.monitored_channels = mon
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

pyro_listener = PyroListener()

# ---------------- Telegram UI helpers ----------------
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

def admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 عرض المستخدمين", callback_data="admin_list_users")],
        [InlineKeyboardButton("🔐 عرض كل APIs", callback_data="admin_list_apis")],
        [InlineKeyboardButton("📜 عرض كل القنوات", callback_data="admin_list_channels")],
        [InlineKeyboardButton("📁 عرض كل الجلسات", callback_data="admin_list_sessions")],
        [InlineKeyboardButton("📢 بث رسالة", callback_data="admin_broadcast")],
        [InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats")],
    ])

async def safe_edit(query, text, markup=None):
    try:
        old = (query.message.text or "").strip()
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

    if data_cb == "create_session":
        context.user_data["awaiting_api"] = True
        await safe_edit(q, "🔑 أرسل api_id و api_hash مفصولين بمسافة.", main_menu())
        return

    if data_cb == "upload_session":
        context.user_data["awaiting_session"] = True
        await safe_edit(q, "📤 أرسل الآن ملف الجلسة (.session) كوثيقة.", main_menu())
        return

    if data_cb == "add_channel":
        context.user_data["awaiting_channel"] = True
        await safe_edit(q, "➕ أرسل الآن: @channel_username @target_bot_username", main_menu())
        return

    if data_cb == "list_channels":
        rows = list_channels_db(uid)
        if not rows:
            await safe_edit(q, "❌ لا توجد قنوات مسجلة لديك.", main_menu())
            return
        text = "📜 قنواتك:\n" + "\n".join([f"- id:{r[0]} {r[1]} -> {r[2]}" for r in rows])
        await safe_edit(q, text, main_menu())
        return

    if data_cb == "restart_listener":
        api = get_api(uid)
        last = get_last_session_row()
        if last and api:
            started = pyro_listener.start_with_session_file(SESSION_FILE, int(api[0]), api[1], uid)
            if started:
                await safe_edit(q, "🔁 تم إعادة تشغيل المستمع.", main_menu())
            else:
                await safe_edit(q, "❌ فشل تشغيل المستمع.", main_menu())
        else:
            await safe_edit(q, "❌ لا توجد جلسة أو API صالح.", main_menu())
        return

    # admin shortcuts
    if uid == ADMIN_ID and data_cb == "admin_list_channels":
        rows = list_all_channels_db()
        text = "📜 جميع القنوات:\n" + ("\n".join([f"- id:{r[0]} user:{r[1]} {r[2]} -> {r[3]}" for r in rows]) if rows else "لا يوجد")
        await safe_edit(q, text, admin_menu())
        return

    await safe_edit(q, "تمّت العملية.", main_menu())

# ---------------- Message handler (flows) ----------------
async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    msg_text = (update.message.text or "").strip()

    # awaiting session (document)
    if context.user_data.get("awaiting_session"):
        doc = update.message.document
        if not doc:
            await update.message.reply_text("❌ أرسل ملف الجلسة كوثيقة (.session).", reply_markup=main_menu())
            context.user_data["awaiting_session"] = False
            return
        file_obj = await doc.get_file()
        raw = await file_obj.download_as_bytearray()
        b64 = base64.b64encode(raw).decode()
        filename = doc.file_name
        save_session_db(uid, filename, b64)
        try:
            with open(os.path.join(SESSIONS_DIR, filename), "wb") as f:
                f.write(base64.b64decode(b64))
        except Exception:
            pass
        context.user_data["awaiting_session"] = False
        await update.message.reply_text("✔️ تم حفظ الجلسة في قاعدة البيانات.", reply_markup=main_menu())
        last = get_last_session_row()
        if last:
            api = get_api(last[1])
            if api:
                pyro_listener.start_with_session_file(os.path.join(SESSIONS_DIR, filename), int(api[0]), api[1], last[1])
                await update.message.reply_text("🔁 تم تشغيل المستمع باستخدام الجلسة المرفوعة.", reply_markup=main_menu())
        return

    # awaiting channel
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

    # awaiting api (start create session flow)
    if context.user_data.get("awaiting_api"):
        parts = msg_text.split(None, 1)
        if len(parts) != 2:
            await update.message.reply_text("❌ الصيغة خاطئة. أرسل: api_id api_hash", reply_markup=main_menu())
            context.user_data["awaiting_api"] = False
            return
        api_id, api_hash = parts
        save_api(uid, api_id, api_hash)
        context.user_data["api_id"] = api_id
        context.user_data["api_hash"] = api_hash
        context.user_data["awaiting_api"] = False
        context.user_data["awaiting_phone"] = True
        await update.message.reply_text("📱 الآن أرسل رقم الهاتف بالصيغة الدولية (مثال: +9647712345678)", reply_markup=main_menu())
        return

    # awaiting phone (we send code)
    if context.user_data.get("awaiting_phone"):
        phone = msg_text
        context.user_data["phone"] = phone
        context.user_data["awaiting_phone"] = False
        # start async task to send code
        asyncio.create_task(_send_pyro_code_task(update, context))
        return

    # awaiting code
    if context.user_data.get("awaiting_code"):
        code = msg_text.strip()
        context.user_data["code"] = code
        # continue sign-in
        asyncio.create_task(_complete_pyro_login_task(update, context))
        return

    # awaiting 2FA password
    if context.user_data.get("awaiting_2fa"):
        pwd = msg_text.strip()
        context.user_data["2fa_pwd"] = pwd
        asyncio.create_task(_complete_pyro_password_task(update, context))
        return

    # default
    await update.message.reply_text("استخدم الأزرار للتنقل أو /start لعرض القائمة.", reply_markup=main_menu())

# ---------------- Pyrogram login helpers (fixed phone_code_hash flow) ----------------
async def _send_pyro_code_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    api_id = int(context.user_data.get("api_id"))
    api_hash = context.user_data.get("api_hash")
    phone = context.user_data.get("phone")

    # create a temp client name "temp" (will create temp.session)
    temp_name = "temp"
    client = Client(temp_name, api_id=api_id, api_hash=api_hash, workdir=SESSIONS_DIR)
    try:
        await client.connect()
        sent = await client.send_code(phone)  # returns object with phone_code_hash
        phone_code_hash = getattr(sent, "phone_code_hash", None)
        if not phone_code_hash:
            # fallback: sometimes structure differs
            phone_code_hash = sent.phone_code_hash if hasattr(sent, "phone_code_hash") else None
        context.user_data["phone_code_hash"] = phone_code_hash
        context.user_data["login_client"] = client  # keep client for later sign_in
        context.user_data["awaiting_code"] = True
        await update.message.reply_text("📨 تم إرسال الكود إلى هاتفك. أرسل الكود هنا.")
    except Exception as e:
        logger.exception("Error while sending code")
        try:
            await client.disconnect()
        except Exception:
            pass
        await update.message.reply_text(f"❌ خطأ أثناء إرسال الكود: {e}")

async def _complete_pyro_login_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    client: Client = context.user_data.get("login_client")
    code = context.user_data.get("code")
    phone = context.user_data.get("phone")
    phone_code_hash = context.user_data.get("phone_code_hash")

    if not client or not code or not phone_code_hash:
        await update.message.reply_text("❌ بيانات غير مكتملة لتسجيل الدخول. أعد العملية.")
        return

    try:
        # IMPORTANT: pass phone_code_hash together with phone_code
        # using keyword args to be explicit
        await client.sign_in(phone_number=phone, phone_code_hash=phone_code_hash, phone_code=code)
    except py_errors.SessionPasswordNeeded:
        # account has two-step verification enabled
        context.user_data["awaiting_2fa"] = True
        await update.message.reply_text("🔒 حسابك محمي بكلمة مرور ثانية (2FA). أرسل كلمة المرور الآن.")
        return
    except Exception as e:
        logger.exception("sign_in failed")
        try:
            await client.disconnect()
        except Exception:
            pass
        await update.message.reply_text(f"❌ فشل تسجيل الدخول: {e}")
        return

    # successful sign in -> move temp.session to listener.session
    try:
        await client.disconnect()
    except Exception:
        pass

    src = os.path.join(SESSIONS_DIR, "temp.session")
    if not os.path.exists(src):
        # sometimes Pyrogram names file 'temp' without extension or with .session
        alt = os.path.join(SESSIONS_DIR, "temp")
        if os.path.exists(alt):
            src = alt
    try:
        os.replace(src, SESSION_FILE)
    except Exception as e:
        logger.exception("Failed to move session file: %s", e)
        await update.message.reply_text(f"⚠️ إنشاء الجلسة نجح لكن لم أتمكن من تحريك الملف: {e}")
        return

    await update.message.reply_text("✔️ تم إنشاء الجلسة بنجاح! وتشغيل المستمع الآن.")
    # start pyro listener with this user's api
    api_id = int(context.user_data.get("api_id"))
    api_hash = context.user_data.get("api_hash")
    pyro_listener.start_with_session_file(SESSION_FILE, api_id, api_hash, uid)

async def _complete_pyro_password_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    client: Client = context.user_data.get("login_client")
    pwd = context.user_data.get("2fa_pwd")
    if not client or not pwd:
        await update.message.reply_text("❌ خطأ داخلي: لا يوجد عميل تسجيل دخول أو كلمة مرور.")
        return
    try:
        # check_password returns user if ok
        await client.check_password(pwd)
    except Exception as e:
        logger.exception("2FA password failed")
        await update.message.reply_text(f"❌ كلمة المرور خاطئة أو حدث خطأ: {e}")
        return

    # on success, move session file same as above
    try:
        await client.disconnect()
    except Exception:
        pass

    src = os.path.join(SESSIONS_DIR, "temp.session")
    if not os.path.exists(src):
        alt = os.path.join(SESSIONS_DIR, "temp")
        if os.path.exists(alt):
            src = alt
    try:
        os.replace(src, SESSION_FILE)
    except Exception as e:
        logger.exception("Failed to move session after 2fa: %s", e)
        await update.message.reply_text(f"⚠️ تم تسجيل الدخول لكن فشل نقل الملف: {e}")
        return

    await update.message.reply_text("✔️ تم التحقق (2FA) وإنشاء الجلسة بنجاح. المستمع يعمل الآن.")
    api_id = int(context.user_data.get("api_id"))
    api_hash = context.user_data.get("api_hash")
    pyro_listener.start_with_session_file(SESSION_FILE, api_id, api_hash, update.effective_user.id)

# ----------------- Start bot -----------------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(pressed_button))
    app.add_handler(MessageHandler(filters.Document.ALL | (filters.TEXT & ~filters.COMMAND), text_message))

    # if last session exists attempt to start (requires api in DB)
    last = get_last_session_row()
    if last:
        api = get_api(last[1])
        if api:
            try:
                pyro_listener.start_with_session_file(SESSION_FILE, int(api[0]), api[1], last[1])
                logger.info("Started Pyrogram listener from existing session at startup.")
            except Exception:
                logger.exception("Failed to start PyroListener at startup.")

    # choose webhook or polling depending on env
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
