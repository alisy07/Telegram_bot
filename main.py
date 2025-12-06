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

# ADMIN ID (ضعه مسبقاً كما أعطيت)
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
    # return last id for convenience
    return conn


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
    """
    Return latest session row as (id, user_id, filename, data_b64) or None
    """
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
    # from apis
    cur.execute("SELECT user_id FROM apis")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    # from channels
    cur.execute("SELECT DISTINCT user_id FROM channels")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    # from sessions
    cur.execute("SELECT DISTINCT user_id FROM sessions")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    conn.close()
    return ids


def list_users_db() -> List[int]:
    return sorted(list(get_all_user_ids()))


# ============ فلتر النصوص (طبق الشروط التي طلبتها) ============
def filter_text_preserve_rules(text: str) -> str:
    """
    طبق الفلاتر:
    - تجاهل الوسائط (وهو خارج هذه الدالة)
    - حذف الأحرف العربية
    - حذف كلمة 'code' بصرف النظر عن الحالة
    - حذف الروابط
    - حذف الأرقام باستثناء الحالات: رقم يلي/يسبق حرف إنجليزي (لا نحذف تلك الأرقام)
    - حذف كل الرموز (A: حذف كل الرموز)
    """

    original = text

    # 1) احذف الأحرف العربية (نطاقات Unicode الشائعة)
    arabic_pattern = re.compile(
        r'[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]'
    )
    text = arabic_pattern.sub("", text)

    # 2) احذف كلمة 'code' (case-insensitive)
    text = re.sub(r"(?i)code", "", text)

    # 3) احذف الروابط (http, https, www., t.me, telegram.me, بدون بروتوكول أيضاً)
    link_pattern = re.compile(
        r'(https?://\S+)|www\.\S+|t\.me/\S+|telegram\.me/\S+|\bhttps?:\S+'
    )
    text = link_pattern.sub("", text)

    # 4) حذف الأرقام إلا إذا كانت جزءًا من نمط letter-digit أو digit-letter حيث letter إنجليزي
    # طريقة: نستبدل بالأحرف التي نريد حذفها فقط:
    # نحذف أي سلسلة أرقام (\d+) التي لا يسبقها حرف إنجليزي ولا يليها حرف إنجليزي
    text = re.sub(r'(?<![A-Za-z])\d+(?![A-Za-z])', '', text)

    # 5) حذف كل الرموز (خيار A: حذف كل الرموز)
    # سنبقي الحروف الإنجليزية والأرقام والمسافات والـ underscore
    # لذلك نحذف أي حرف ليس حرفًا أو رقمًا أو مساحة أو underscore
    text = re.sub(r'[^\w\s]', '', text)

    # 6) تنظيف المسافات المتكررة
    text = re.sub(r'\s+', ' ', text).strip()

    # إن كانت النتيجة فارغة، نعيد رسالة إيضاحية
    if not text:
        return "❌ لا يبقى نص قابل للإرسال بعد عملية الفلترة."

    return text


# ============ Pyrogram Listener (خيار A: مستمع واحد باستخدام آخر جلسة) ============
class PyroListener:
    def __init__(self):
        self.thread: Optional[threading.Thread] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.client: Optional[Client] = None
        self.running = False
        self.session_user_id: Optional[int] = None
        self.monitored_channels: Set[str] = set()  # set of channel usernames (with or without @)

    def _write_session_file(self, filename: str, b64data: str) -> str:
        path = os.path.join(SESSIONS_DIR, filename)
        # نكتب الملف الثنائي
        with open(path, "wb") as f:
            f.write(base64.b64decode(b64data))
        return path

    def _pyro_thread_target(self, session_path: str, api_id: int, api_hash: str, session_user_id: int):
        """
        سيعمل داخل ثريد منفصل؛ ينشئ حلقة asyncio خاصة به.
        """
        # كل شيء داخل حلقة جديدة
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop

        client = Client(
            session_path,  # path to .session file (pyrogram يدعم ذلك)
            api_id=api_id,
            api_hash=api_hash,
            workdir=SESSIONS_DIR  # للتخزين المؤقت إن لزم
        )
        self.client = client
        self.session_user_id = session_user_id

        logger.info("PyroListener: starting pyrogram client ...")

        # المعالج للرسائل الواردة
        async def on_message(client_obj, message):
            try:
                # تجاهل الرسائل من بوتات و غير القنوات (نريد رسائل من القنوات)
                # messages from channels often have message.chat.type == "channel"
                chat = message.chat
                if not chat:
                    return

                # Accept only messages from channels or sender_chat
                if chat.type != "channel" and chat.type != "supergroup" and chat.type != "group":
                    return

                # Determine channel username if exists
                ch_username = None
                if getattr(chat, "username", None):
                    ch_username = chat.username
                else:
                    # sometimes chat.title present only; skip if no username
                    # we only monitor by username, so ignore if no username
                    return

                # standardize with leading @
                if ch_username and not ch_username.startswith("@"):
                    ch_username = "@" + ch_username

                if ch_username not in self.monitored_channels:
                    return  # ليس ضمن القنوات التي نراقبها لهذا الحساب

                # ignore any message that contains media (user wanted only text)
                if message.photo or message.video or message.document or message.audio or message.animation or message.voice or message.sticker:
                    logger.debug("PyroListener: Ignoring media message from %s", ch_username)
                    return

                # get text (prefers text or caption)
                raw_text = message.text or message.caption
                if not raw_text:
                    return

                # apply filters
                filtered = filter_text_preserve_rules(raw_text)
                if not filtered or filtered.startswith("❌ لا يبقى"):
                    # don't send empty/invalid
                    logger.debug("PyroListener: filtered message empty/invalid, skipping.")
                    return

                # get target bot for this channel from DB (channel_username -> target_bot_username)
                conn = sqlite3.connect(DB_FILE)
                cur = conn.cursor()
                cur.execute(
                    "SELECT target_bot_username FROM channels WHERE user_id = ? AND channel_username = ? LIMIT 1",
                    (session_user_id, ch_username),
                )
                row = cur.fetchone()
                conn.close()
                if not row:
                    logger.info("No target bot configured for %s", ch_username)
                    return
                target_bot = row[0]
                if not target_bot:
                    return

                # ensure starts with @
                if not target_bot.startswith("@"):
                    target_bot = "@" + target_bot

                # send message to target bot as the user account
                try:
                    await client_obj.send_message(target_bot, filtered)
                    logger.info("Forwarded filtered text from %s to %s", ch_username, target_bot)
                except Exception as e:
                    logger.exception("Failed to send message to target bot %s: %s", target_bot, e)

            except Exception:
                logger.exception("Error in on_message handler")

        # أضف المعالج للـ client
        client.add_handler(PyroMessageHandler(on_message, py_filters.all))

        try:
            loop.run_until_complete(client.start())
            self.running = True
            logger.info("Pyrogram client started. Monitoring channels: %s", self.monitored_channels)
            loop.run_until_complete(client.idle())  # يبقى شغال حتى يتوقف
        except Exception as e:
            logger.exception("Pyrogram client error: %s", e)
        finally:
            try:
                loop.run_until_complete(client.stop())
            except Exception:
                pass
            self.running = False
            logger.info("Pyrogram client stopped.")

    def start_with_session_row(self, session_row):
        """
        session_row: (id, user_id, filename, data_b64)
        """
        if not session_row:
            logger.info("No session row to start.")
            return False

        sid, user_id, filename, data_b64 = session_row
        api = get_api(user_id)
        if not api:
            logger.error("No API_ID/API_HASH found for session owner user_id=%s. Can't start Pyrogram.", user_id)
            return False
        api_id, api_hash = api
        # write session file
        session_path = self._write_session_file(filename, data_b64)
        # stop existing if any
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

        # start thread
        t = threading.Thread(target=self._pyro_thread_target, args=(session_path, int(api_id), api_hash, user_id), daemon=True)
        t.start()
        self.thread = t
        logger.info("PyroListener: started thread for session user %s", user_id)
        return True

    def stop(self):
        if not self.thread or not self.running:
            # nothing running
            return
        try:
            # try to stop client gracefully
            if self.client and self.loop:
                fut = asyncio.run_coroutine_threadsafe(self.client.stop(), self.loop)
                fut.result(timeout=10)
        except Exception:
            logger.exception("Error stopping pyrogram client")
        finally:
            # attempt to stop loop
            try:
                if self.loop and self.loop.is_running():
                    self.loop.call_soon_threadsafe(self.loop.stop)
            except Exception:
                pass
            # join thread
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
        """
        قم بتحديث قائمة القنوات التي يملكها صاحب الجلسة الجاري من قاعدة البيانات.
        """
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
        logger.info("PyroListener: reloaded monitored channels: %s", self.monitored_channels)


# single global listener
pyro_listener = PyroListener()


# ============ واجهة المستخدم (زراريَّة) ============
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
            [InlineKeyboardButton("🔁 إعادة تشغيل المستمع", callback_data="restart_listener")],  # مفيد للتجربة
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


# ============ Handlers (Telegram bot) ============
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً! اختر من القائمة:", reply_markup=main_menu())


async def pressed_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id

    # admin callbacks...
    if q.data.startswith("admin_"):
        if user_id != ADMIN_ID:
            await q.edit_message_text("❌ هذا القسم مخصّص للمشرف فقط.")
            return
        # handle admin actions (same as سابقاً)...
        if q.data == "admin_list_users":
            users = list_users_db()
            if not users:
                await q.edit_message_text("لا يوجد مستخدمون مسجلون.", reply_markup=admin_menu())
                return
            text = "👥 المستخدمون (user_id):\n" + "\n".join(str(u) for u in users)
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

        if q.data == "admin_list_apis":
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT user_id, api_id, api_hash FROM apis")
            rows = cur.fetchall()
            conn.close()
            if not rows:
                await q.edit_message_text("لا توجد APIs مسجلة.", reply_markup=admin_menu())
                return
            text = "🔐 APIs:\n" + "\n".join([f"- {r[0]}: {r[1]} | {r[2]}" for r in rows])
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

        if q.data == "admin_list_channels":
            rows = list_all_channels_db()
            if not rows:
                await q.edit_message_text("لا توجد قنوات مسجلة.", reply_markup=admin_menu())
                return
            text = "📜 جميع القنوات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} {r[2]} -> {r[3]}" for r in rows])
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

        if q.data == "admin_list_sessions":
            rows = list_all_sessions_db()
            if not rows:
                await q.edit_message_text("لا توجد جلسات محفوظة.", reply_markup=admin_menu())
                return
            text = "📁 جميع الجلسات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} file:{r[2]}" for r in rows])
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

        if q.data == "admin_broadcast":
            context.user_data["mode"] = "admin_broadcast_wait"
            await q.edit_message_text("📢 أرسل الآن نص الرسالة التي تريد إرسالها إلى كل المستخدمين.")
            return

        if q.data == "admin_stats":
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT user_id) FROM (SELECT user_id FROM apis UNION SELECT user_id FROM channels UNION SELECT user_id FROM sessions)")
            users_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM channels")
            channels_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM sessions")
            sessions_count = cur.fetchone()[0]
            conn.close()
            text = f"📊 إحصائيات:\n- مستخدمون مميزون: {users_count}\n- قنوات: {channels_count}\n- جلسات محفوظة: {sessions_count}"
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

    # admin panel open (shortcut)
    if q.data == "open_admin_panel":
        if user_id != ADMIN_ID:
            await q.edit_message_text("❌ هذا القسم مخصّص للمشرف فقط.")
            return
        await q.edit_message_text("لوحة المشرف:", reply_markup=admin_menu())
        return

    # confirmation deletion flow
    if q.data.startswith("confirm_del:"):
        try:
            chid = int(q.data.split(":", 1)[1])
        except Exception:
            await q.edit_message_text("خطأ: معرّف القناة غير صالح.", reply_markup=main_menu())
            return
        delete_channel_db(chid)
        # reload monitored channels if needed
        pyro_listener.reload_monitored_channels_for_current_session()
        await q.edit_message_text("✔️ تم حذف القناة.", reply_markup=main_menu())
        return

    if q.data == "cancel_del":
        await q.edit_message_text("❌ تم إلغاء الحذف.", reply_markup=main_menu())
        return

    if q.data.startswith("del:"):
        try:
            chid = int(q.data.split(":", 1)[1])
        except Exception:
            await q.edit_message_text("خطأ: معرّف القناة غير صحيح.", reply_markup=main_menu())
            return
        confirm_keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✔️ نعم، احذف", callback_data=f"confirm_del:{chid}")],
                [InlineKeyboardButton("❌ لا، إلغاء", callback_data="cancel_del")],
            ]
        )
        await q.edit_message_text("هل أنت متأكد من حذف هذه القناة؟", reply_markup=confirm_keyboard)
        return

    # user actions
    if q.data == "upload_session":
        context.user_data["mode"] = "upload_session"
        await q.edit_message_text("🟦 أرسل الآن ملف الجلسة (ملف .session أو ما لديك).")
        return

    if q.data == "add_channel":
        context.user_data["mode"] = "add_channel_wait_channel"
        await q.edit_message_text("أرسل معرف القناة مثل @example (أو اسم القناة بدون @).")
        return

    if q.data == "delete_channel":
        channels = list_channels_db(user_id)
        if not channels:
            await q.edit_message_text("❌ لا توجد قنوات لديك للحذف.", reply_markup=main_menu())
            return
        buttons = [
            [InlineKeyboardButton(f"{ch[1]} → {ch[2]}", callback_data=f"del:{ch[0]}")]
            for ch in channels
        ]
        await q.edit_message_text("اختر القناة لحذفها:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if q.data == "list_channels":
        channels = list_channels_db(user_id)
        if not channels:
            await q.edit_message_text("لا توجد قنوات.", reply_markup=main_menu())
            return
        text = "📜 قنواتك:\n" + "\n".join([f"- {c[1]}  (to: {c[2]}) [id:{c[0]}]" for c in channels])
        await q.edit_message_text(text, reply_markup=main_menu())
        return

    if q.data == "add_api":
        context.user_data["mode"] = "add_api_wait_id"
        await q.edit_message_text("أرسل الآن `api_id` كرسالة (أرسل الرقم فقط).")
        return

    if q.data == "view_api":
        row = get_api(user_id)
        if not row:
            await q.edit_message_text("❌ لم تسجّل API_ID / API_HASH بعد.", reply_markup=main_menu())
            return
        api_id, api_hash = row
        await q.edit_message_text(f"🔐 API الخاص بك:\napi_id: `{api_id}`\napi_hash: `{api_hash}`", reply_markup=main_menu())
        return

    if q.data == "list_sessions":
        rows = list_sessions_db(user_id)
        if not rows:
            await q.edit_message_text("لا توجد جلسات لديك.", reply_markup=main_menu())
            return
        text = "📁 جلساتك:\n" + "\n".join([f"- id:{r[0]} file:{r[1]}" for r in rows])
        await q.edit_message_text(text, reply_markup=main_menu())
        return

    if q.data == "restart_listener":
        # restart using last session
        last = get_last_session_row()
        if not last:
            await q.edit_message_text("❌ لا توجد جلسة لتشغيل المستمع.", reply_markup=main_menu())
            return
        started = pyro_listener.start_with_session_row(last)
        if started:
            await q.edit_message_text("🔁 تم إعادة تشغيل المستمع باستخدام آخر جلسة.", reply_markup=main_menu())
        else:
            await q.edit_message_text("❌ فشل تشغيل المستمع — تأكد من وجود api_id/api_hash لمالك الجلسة.", reply_markup=main_menu())
        return

    # unknown fallback
    await q.edit_message_text("تمّت العملية.", reply_markup=main_menu())


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    text = update.message.text.strip()

    # ------------- فلترة كلمة code (حذفت في المستمع أيضاً) -------------
    if re.search(r"(?i)code", text):
        # نحذفها مبكراً لأن المستخدم قد يكتبها هنا أثناء إدخال بيانات
        text = re.sub(r"(?i)code", "", text)

    mode = context.user_data.get("mode")

    # admin broadcast flow
    if mode == "admin_broadcast_wait":
        if user_id != ADMIN_ID:
            context.user_data["mode"] = None
            await update.message.reply_text("❌ غير مصرح لك.")
            return
        broadcast_text = text
        context.user_data["mode"] = None
        user_ids = list(get_all_user_ids())
        if not user_ids:
            await update.message.reply_text("لا يوجد مستخدمون للإرسال إليهم.", reply_markup=admin_menu())
            return
        sent = 0
        failed = 0
        await update.message.reply_text(f"♻️ جارٍ إرسال الرسالة إلى {len(user_ids)} مستخدماً ...")
        for uid in user_ids:
            try:
                await context.bot.send_message(uid, broadcast_text)
                sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        await update.message.reply_text(f"✅ انتهى البث. تم الإرسال: {sent}. فشل: {failed}", reply_markup=admin_menu())
        return

    # add API flow
    if mode == "add_api_wait_id":
        context.user_data["tmp_api_id"] = text
        context.user_data["mode"] = "add_api_wait_hash"
        await update.message.reply_text("حسناً. الآن أرسل `api_hash` (السلسلة).")
        return

    if mode == "add_api_wait_hash":
        api_id = context.user_data.get("tmp_api_id")
        api_hash = text
        if not api_id:
            await update.message.reply_text("خطأ داخلي: لم يتم العثور على api_id. أعد العملية بالضغط على زر إضافة API.")
            context.user_data["mode"] = None
            return
        save_api(user_id, api_id, api_hash)
        context.user_data.pop("tmp_api_id", None)
        context.user_data["mode"] = None
        await update.message.reply_text("✔️ تم حفظ API_ID و API_HASH بنجاح.", reply_markup=main_menu())
        return

    # add channel: step 1
    if mode == "add_channel_wait_channel":
        channel = text
        if not channel.startswith("@"):
            channel = "@" + channel
        context.user_data["tmp_channel"] = channel
        context.user_data["mode"] = "add_channel_wait_target"
        await update.message.reply_text("حسناً. الآن أرسل اسم بوت الهدف (مثال: @target_bot).")
        return

    # add channel: step 2
    if mode == "add_channel_wait_target":
        target = text
        if not target.startswith("@"):
            target = "@" + target
        channel = context.user_data.get("tmp_channel")
        if not channel:
            await update.message.reply_text("خطأ: لم يتم العثور على اسم القناة. أعد العملية.")
            context.user_data["mode"] = None
            return
        add_channel_db(user_id, channel, target)
        context.user_data.pop("tmp_channel", None)
        context.user_data["mode"] = None
        # reload monitored channels if the running listener belongs to this user
        pyro_listener.reload_monitored_channels_for_current_session()
        await update.message.reply_text(f"✔️ تم إضافة القناة {channel} مع بوت الهدف {target}.", reply_markup=main_menu())
        return

    # upload session
    if mode == "upload_session":
        await update.message.reply_text("❌ الرجاء إرفاق الملف كوثيقة (Document).")
        return

    # default
    await update.message.reply_text("استخدم الأزرار للتنقل أو /start لعرض القائمة.", reply_markup=main_menu())


async def file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if context.user_data.get("mode") != "upload_session":
        return
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ لم يتم العثور على ملف.")
        return
    file_obj = await doc.get_file()
    raw = await file_obj.download_as_bytearray()
    b64 = base64.b64encode(raw).decode()
    filename = doc.file_name
    save_session_db(user_id, filename, b64)

    # حفظ نسخة محلية اختيارية أيضاً
    try:
        with open(os.path.join(SESSIONS_DIR, filename), "wb") as f:
            f.write(base64.b64decode(b64))
    except Exception:
        pass

    context.user_data["mode"] = None
    await update.message.reply_text("✔️ تم حفظ الجلسة في قاعدة البيانات.", reply_markup=main_menu())

    # بعد رفع الجلسة نعيد تشغيل المستمع باستخدام آخر جلسة (سلوك الخيار A)
    last = get_last_session_row()
    if last:
        started = pyro_listener.start_with_session_row(last)
        if started:
            logger.info("Pyrogram listener restarted after new session upload.")
        else:
            logger.error("Failed to start pyrogram listener after session upload.")


# ============ Admin text commands ============
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    await update.message.reply_text("لوحة المشرف:", reply_markup=admin_menu())


async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    users = list_users_db()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون مسجلون.")
        return
    text = "👥 المستخدمون (user_id):\n" + "\n".join(str(u) for u in users)
    await update.message.reply_text(text)


async def list_apis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, api_id, api_hash FROM apis")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("لا توجد APIs مسجلة.")
        return
    text = "🔐 APIs:\n" + "\n".join([f"- {r[0]}: {r[1]} | {r[2]}" for r in rows])
    await update.message.reply_text(text)


async def list_channels_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    rows = list_all_channels_db()
    if not rows:
        await update.message.reply_text("لا توجد قنوات مسجلة.")
        return
    text = "📜 جميع القنوات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} {r[2]} -> {r[3]}" for r in rows])
    await update.message.reply_text(text)


async def list_sessions_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    rows = list_all_sessions_db()
    if not rows:
        await update.message.reply_text("لا توجد جلسات محفوظة.")
        return
    text = "📁 جميع الجلسات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} file:{r[2]}" for r in rows])
    await update.message.reply_text(text)


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM (SELECT user_id FROM apis UNION SELECT user_id FROM channels UNION SELECT user_id FROM sessions)")
    users_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM channels")
    channels_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM sessions")
    sessions_count = cur.fetchone()[0]
    conn.close()
    text = f"📊 إحصائيات:\n- مستخدمون مميزون: {users_count}\n- قنوات: {channels_count}\n- جلسات محفوظة: {sessions_count}"
    await update.message.reply_text(text)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    context.user_data["mode"] = "admin_broadcast_wait"
    await update.message.reply_text("📢 أرسل الآن نص الرسالة التي تريد إرسالها إلى كل المستخدمين.")


# ============ تشغيل Webhook وتهيئة كل شيء ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # مثال: https://myapp.onrender.com

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CallbackQueryHandler(pressed_button))

    # admin text commands
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("users", list_users_command))
    app.add_handler(CommandHandler("list_apis", list_apis_command))
    app.add_handler(CommandHandler("list_channels_all", list_channels_all_command))
    app.add_handler(CommandHandler("list_sessions_all", list_sessions_all_command))
    app.add_handler(CommandHandler("admin_stats", admin_stats_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    # message handlers
    app.add_handler(MessageHandler(filters.Document.ALL, file_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    # Start the Pyrogram listener automatically using latest session (if exists)
    last = get_last_session_row()
    if last:
        started = pyro_listener.start_with_session_row(last)
        if started:
            logger.info("Pyrogram listener started at bot startup with last session.")
        else:
            logger.warning("Pyrogram listener did not start (missing API credentials?).")

    # Webhook (مهيأ للعمل على Render)
    app.run_webhook(
        listen="0.0.0.0",
        port=10000,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        allowed_updates=None,  # كل الأنواع
    )


if __name__ == "__main__":
    main()
