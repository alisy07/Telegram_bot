#!/usr/bin/env python3
import os
import re
import json
import sqlite3
import logging
import threading
import time
import asyncio
from flask import Flask, request, render_template, jsonify
from telegram import Bot, Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from pyrogram import Client, filters as pyro_filters
from telethon import TelegramClient, errors as telethon_errors

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DB_FILE = os.path.join(BASE_DIR, "bot.db")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

# load config
with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    cfg = json.load(f)

ADMIN_TELEGRAM_ID = int(cfg.get("admin_telegram_id", 0))
BOT_TOKEN_ENV = cfg.get("bot_token_env_name", "BOT_TOKEN")
PORT = int(os.environ.get("PORT", cfg.get("listen_port", 10000)))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or cfg.get("webhook_url") or ""

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

# ----------------- DB init -----------------
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""CREATE TABLE IF NOT EXISTS channels(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 channel_name TEXT UNIQUE,
 bot_target TEXT,
 active INTEGER DEFAULT 0,
 created_at TEXT
)""")
cursor.execute("""CREATE TABLE IF NOT EXISTS sessions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 api_id INTEGER,
 api_hash TEXT,
 session_name TEXT UNIQUE,
 created_at TEXT
)""")
cursor.execute("""CREATE TABLE IF NOT EXISTS logs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ts TEXT,
 source TEXT,
 original TEXT,
 cleaned TEXT,
 target TEXT,
 status TEXT
)""")
# temp storage for phone_code_hash
cursor.execute("""CREATE TABLE IF NOT EXISTS temp_codes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT,
  phone_code_hash TEXT,
  created_at TEXT
)""")
conn.commit()

# in-memory
active_channels = {}
def load_active_channels():
    active_channels.clear()
    cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
    for ch, bt in cursor.fetchall():
        active_channels[ch] = bt
load_active_channels()

# ----------------- Flask (read-only dashboard) -----------------
app = Flask(__name__, template_folder="templates", static_folder="static")

# ----------------- Filtering utilities -----------------
_LINK_RE = re.compile(r"https?://\S+|www\.\S+")
_HASHTAG_RE = re.compile(r"#\w+")
_CODE_RE = re.compile(r"\bcode\b", re.IGNORECASE)
_ARABIC_RE = re.compile(r"[\u0600-\u06FF]+")
_SYMBOLS_RE = re.compile(r"[^\w\s]")  # keep underscores and alnum and whitespace

def remove_links(text: str) -> str:
    return _LINK_RE.sub(" ", text)

def remove_hashtags(text: str) -> str:
    return _HASHTAG_RE.sub(" ", text)

def remove_code_word(text: str) -> str:
    return _CODE_RE.sub(" ", text)

def clean_symbols(text: str) -> str:
    return _SYMBOLS_RE.sub(" ", text)

def extract_english_parts(text: str) -> str:
    parts = re.findall(r"[A-Za-z0-9_\-]+(?:[A-Za-z0-9_\-]*)", text)
    return " ".join(parts).strip()

def smart_remove_numbers(text: str) -> str:
    result_chars = []
    L = len(text)
    for i, ch in enumerate(text):
        if ch.isdigit():
            prev_c = text[i-1] if i > 0 else ""
            next_c = text[i+1] if i < L-1 else ""
            keep = False
            if prev_c and re.match(r"[A-Za-z]", prev_c):
                keep = True
            if next_c and re.match(r"[A-Za-z]", next_c):
                keep = True
            if keep:
                result_chars.append(ch)
            else:
                pass
        else:
            result_chars.append(ch)
    return "".join(result_chars)

def ready_processing(text: str) -> str:
    """
    Full processing pipeline:
    - remove links, hashtags, the word 'code'
    - remove punctuation/symbols
    - if message contains latin parts -> extract only latin parts
    - else remove Arabic script
    - remove standalone numbers (keep numbers attached to latin letters, e.g., A1 or 1A)
    - normalize spaces
    """
    if not text:
        return ""
    text = text.strip()
    text = remove_links(text)
    text = remove_hashtags(text)
    text = remove_code_word(text)
    text = clean_symbols(text)
    eng = extract_english_parts(text)
    if eng:
        text = eng
    else:
        text = _ARABIC_RE.sub(" ", text)
    text = smart_remove_numbers(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

# ----------------- Bot Manager -----------------
class BotManager:
    def __init__(self):
        self.bot = None
        self.dispatcher = None
        self.running = False
        self.waiting_api = {}
        self.waiting_channel = {}
        self.waiting_session = {}

    def read_token(self):
        token = os.environ.get(BOT_TOKEN_ENV)
        if not token:
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    token = data.get("bot_token") or token
            except Exception:
                pass
        return token

    def start(self):
        if self.running:
            return True
        token = self.read_token()
        if not token:
            logger.warning("No BOT token found (env or config.json)")
            return False
        self.bot = Bot(token=token)
        self.dispatcher = Dispatcher(self.bot, None, use_context=True)
        self.dispatcher.add_handler(CommandHandler("start", self.cmd_start))
        self.dispatcher.add_handler(CommandHandler("setapi", self.cmd_setapi))
        self.dispatcher.add_handler(CommandHandler("create_session", self.cmd_create_session))
        self.dispatcher.add_handler(CommandHandler("start_listener", self.cmd_start_listener))
        self.dispatcher.add_handler(CommandHandler("stop_listener", self.cmd_stop_listener))
        self.dispatcher.add_handler(CommandHandler("cancel", self.cmd_cancel))
        self.dispatcher.add_handler(CallbackQueryHandler(self.on_callback))
        self.dispatcher.add_handler(MessageHandler(Filters.private & Filters.text, self.on_private))
        if WEBHOOK_URL:
            try:
                wh = WEBHOOK_URL.rstrip("/") + "/webhook"
                self.bot.set_webhook(wh)
                logger.info("Webhook set to %s", wh)
            except Exception as e:
                logger.exception("Failed to set webhook: %s", e)
        self.running = True
        logger.info("BotManager started")
        return True

    def cmd_start(self, update, context):
        user = update.effective_user
        if user and user.id == ADMIN_TELEGRAM_ID:
            keyboard = [["📺 القنوات", "📡 الجلسات"], ["📝 السجلات", "➕ إضافة قناة"], ["🔁 إنشاء جلسة", "▶️ تشغيل المستمع"]]
            update.message.reply_text("مرحبا — اختر:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        else:
            update.message.reply_text("غير مصرح لك.")

    def cmd_setapi(self, update, context):
        user = update.effective_user
        if not user or user.id != ADMIN_TELEGRAM_ID:
            update.message.reply_text("غير مصرح لك.")
            return
        self.waiting_api[user.id] = True
        update.message.reply_text("أرسل API_ID و API_HASH مفصولين بمسافة واحدة (مثال: 123456 abcdef1234).")

    def cmd_create_session(self, update, context):
        user = update.effective_user
        if not user or user.id != ADMIN_TELEGRAM_ID:
            update.message.reply_text("غير مصرح لك.")
            return
        self.waiting_session[user.id] = {"step": "phone"}
        update.message.reply_text("أرسل رقم هاتفك (مثال: +20100xxxxxxx). سيتم إرسال كود للتحقق.")

    def cmd_start_listener(self, update, context):
        user = update.effective_user
        if not user or user.id != ADMIN_TELEGRAM_ID:
            update.message.reply_text("غير مصرح لك.")
            return
        if pyro_listener.start():
            update.message.reply_text("تم تشغيل مستمع Pyrogram ✅")
        else:
            update.message.reply_text("فشل تشغيل المستمع — تأكد من وجود listener session وبيانات API (استخدم /setapi و /create_session).")

    def cmd_stop_listener(self, update, context):
        user = update.effective_user
        if not user or user.id != ADMIN_TELEGRAM_ID:
            update.message.reply_text("غير مصرح لك.")
            return
        if pyro_listener.stop():
            update.message.reply_text("تم إيقاف مستمع Pyrogram ✅")
        else:
            update.message.reply_text("فشل إيقاف المستمع.")

    def cmd_cancel(self, update, context):
        uid = update.effective_user.id
        self.waiting_api.pop(uid, None)
        self.waiting_channel.pop(uid, None)
        self.waiting_session.pop(uid, None)
        update.message.reply_text("تم إلغاء العملية.")

    def on_callback(self, update, context):
        query = update.callback_query
        uid = query.from_user.id
        if query.data == "add_channel":
            self.waiting_channel[uid] = {"step": "channel_name"}
            query.answer()
            query.edit_message_text("أرسل اسم القناة (username بدون @)")

    def on_private(self, update, context):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        if uid != ADMIN_TELEGRAM_ID:
            update.message.reply_text("غير مصرح لك.")
            return

        session_state = self.waiting_session.get(uid)
        if session_state:
            step = session_state.get("step")
            if step == "phone":
                phone = text
                session_state["phone"] = phone
                update.message.reply_text("جارٍ إرسال كود التحقق... الرجاء الانتظار.")
                cursor.execute("SELECT api_id, api_hash FROM sessions ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if not row:
                    update.message.reply_text("لا توجد بيانات API_ID/API_HASH في قاعدة البيانات. استخدم /setapi أولاً.")
                    self.waiting_session.pop(uid, None)
                    return
                api_id, api_hash = int(row[0]), str(row[1])
                try:
                    res = self._telethon_send_code(api_id, api_hash, phone)
                    if not res.get("ok"):
                        update.message.reply_text(f"خطأ أثناء إرسال الكود: {res.get('error')}")
                        self.waiting_session.pop(uid, None)
                        return
                    phone_code_hash = res.get("phone_code_hash")
                    try:
                        cursor.execute("INSERT INTO temp_codes(phone, phone_code_hash, created_at) VALUES (?, ?, datetime('now'))", (phone, phone_code_hash))
                        conn.commit()
                    except:
                        pass
                    session_state["step"] = "code"
                    update.message.reply_text("تم إرسال الكود. أرسله هنا.")
                except Exception as e:
                    update.message.reply_text(f"خطأ: {e}")
                    self.waiting_session.pop(uid, None)
                return

            if step == "code":
                code = text
                phone = session_state.get("phone")
                # تأكد أن لدينا API creds
                cursor.execute("SELECT api_id, api_hash FROM sessions ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if not row:
                    update.message.reply_text("بيانات API غير متوفرة. استخدم /setapi.")
                    self.waiting_session.pop(uid, None)
                    return
                api_id, api_hash = int(row[0]), str(row[1])
            
                # جلب آخر phone_code_hash المرتبط بالهاتف من temp_codes
                cursor.execute("SELECT phone_code_hash FROM temp_codes WHERE phone=? ORDER BY id DESC LIMIT 1", (phone,))
                r2 = cursor.fetchone()
                phone_code_hash = r2[0] if r2 else None
            
                update.message.reply_text("جاري تسجيل الدخول وحفظ الجلسة — لا تغلق الرسائل...")
                res = self._telethon_sign_in_and_save(api_id, api_hash, phone, code=code, phone_code_hash=phone_code_hash)
                if res.get("ok"):
                    update.message.reply_text("✅ تم إنشاء الجلسة وحفظها على الخادم (sessions/listener.session).")
                    # تشغيل المستمع تلقائياً إن أمكن
                    if pyro_listener.start():
                        update.message.reply_text("🔄 تم تشغيل مستمع Pyrogram تلقائياً.")
                else:
                    if res.get("password_needed"):
                        session_state["step"] = "password"
                        update.message.reply_text("الحساب يطلب كلمة مرور 2FA. أرسل كلمة المرور الآن.")
                        return
                    # إظهار رسالة الخطأ المفصّلة
                    update.message.reply_text(f"فشل إنشاء الجلسة: {res.get('error')}")
                self.waiting_session.pop(uid, None)
                return
                
            if step == "password":
                password = text
                phone = session_state.get("phone")
                cursor.execute("SELECT api_id, api_hash FROM sessions ORDER BY id DESC LIMIT 1")
                row = cursor.fetchone()
                if not row:
                    update.message.reply_text("بيانات API غير متوفرة. استخدم /setapi.")
                    self.waiting_session.pop(uid, None)
                    return
                api_id, api_hash = int(row[0]), str(row[1])
                update.message.reply_text("جاري محاولة تسجيل الدخول باستخدام كلمة المرور...")
                res = self._telethon_sign_in_and_save(api_id, api_hash, phone, password=password)
                if res.get("ok"):
                    update.message.reply_text("✅ تم إنشاء الجلسة وحفظها على الخادم.")
                    if pyro_listener.start():
                        update.message.reply_text("🔄 تم تشغيل مستمع Pyrogram تلقائياً.")
                else:
                    update.message.reply_text(f"فشل: {res.get('error')}")
                self.waiting_session.pop(uid, None)
                return

        if self.waiting_api.get(uid):
            parts = text.split()
            if len(parts) < 2:
                update.message.reply_text("خطأ في الصيغة. أرسل: API_ID API_HASH")
                return
            api_id, api_hash = parts[0], parts[1]
            try:
                cursor.execute("DELETE FROM sessions")
                cursor.execute(
                    "INSERT INTO sessions(api_id, api_hash, session_name, created_at) VALUES (?, ?, ?, datetime('now'))",
                    (int(api_id), api_hash, "listener"))
                conn.commit()
                update.message.reply_text("تم حفظ API_ID و API_HASH في قاعدة البيانات ✅\\nاستخدم /create_session لإنشاء session عبر البوت أو شغل create_session.py محلياً.")
                self.waiting_api.pop(uid, None)
            except Exception as e:
                update.message.reply_text(f"خطأ أثناء الحفظ: {e}")
            return

        state = self.waiting_channel.get(uid)
        if state:
            step = state.get("step")
            if step == "channel_name":
                state["channel_name"] = text.lstrip("@")
                state["step"] = "bot_target"
                update.message.reply_text("أرسل الآن اسم البوت الهدف (username بدون @)")
                return
            elif step == "bot_target":
                ch = state.get("channel_name")
                target = text.lstrip("@")
                try:
                    cursor.execute("INSERT OR REPLACE INTO channels(channel_name, bot_target, active, created_at) VALUES (?,?,1,datetime('now'))",
                                   (ch, target))
                    conn.commit()
                    active_channels[ch] = target
                    update.message.reply_text(f"تم إضافة @{ch} → @{target} ✅")
                except Exception as e:
                    update.message.reply_text(f"خطأ أثناء الإضافة: {e}")
                self.waiting_channel.pop(uid, None)
                return

        if text == "📺 القنوات":
            rows = cursor.execute("SELECT channel_name, bot_target, active FROM channels ORDER BY id DESC").fetchall()
            if not rows:
                update.message.reply_text("لا توجد قنوات مضافة.")
            else:
                msg = "القنوات المضافة:\\n\\n"
                for ch, target, active in rows:
                    status = "✅ مفعل" if active else "❌ متوقف"
                    msg += f"@{ch} → @{target} ({status})\\n"
                update.message.reply_text(msg)
            return

        if text == "📡 الجلسات":
            rows = cursor.execute("SELECT api_id, api_hash, session_name FROM sessions ORDER BY id DESC").fetchall()
            if not rows:
                update.message.reply_text("لا توجد جلسات مسجلة.")
            else:
                msg = "الجلسات:\\n\\n"
                for api_id, api_hash, session_name in rows:
                    msg += f"{session_name}: {api_id} / {api_hash}\\n"
                update.message.reply_text(msg)
            return

        if text == "📝 السجلات":
            rows = cursor.execute("SELECT ts, source, cleaned, target, status FROM logs ORDER BY id DESC LIMIT 20").fetchall()
            if not rows:
                update.message.reply_text("لا توجد سجلات بعد.")
            else:
                msg = "آخر السجلات:\\n\\n"
                for ts, source, cleaned, target, status in rows:
                    msg += f"{ts} | @{source} → @{target} | {status}\\n{cleaned}\\n\\n"
                update.message.reply_text(msg)
            return

        if text == "➕ إضافة قناة":
            self.waiting_channel[uid] = {"step": "channel_name"}
            update.message.reply_text("أرسل اسم القناة (username بدون @)")
            return

        update.message.reply_text("استخدم الأزرار أو الأوامر المتاحة (مثل /setapi أو /create_session).")

    def _telethon_send_code(self, api_id, api_hash, phone):
    async def _send():
        # نستخدم session مؤقت لطلب الكود
        client = TelegramClient(os.path.join(SESSIONS_DIR, "tmp_send"), api_id, api_hash)
        await client.connect()
        try:
            sent = await client.send_code_request(phone)
            phone_code_hash = None
            # Telethon قد يُعيد phone_code_hash كـ attribute
            if hasattr(sent, 'phone_code_hash'):
                phone_code_hash = sent.phone_code_hash
            # بعض نسخ Telethon قد تعيد sent.phone_code.hash أو sent.phone_code.hash.value
            # نتحرّى أيضاً داخل الخصائص المتاحة
            elif hasattr(sent, 'phone_code') and hasattr(sent.phone_code, 'phone_code_hash'):
                phone_code_hash = sent.phone_code.phone_code_hash
            await client.disconnect()
            return {"ok": True, "phone_code_hash": phone_code_hash}
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return {"ok": False, "error": str(e)}
    return asyncio.run(_send())

    def _telethon_sign_in_and_save(self, api_id, api_hash, phone, code=None, phone_code_hash=None, password=None):
    async def _signin():
        session_path = os.path.join(SESSIONS_DIR, "listener")
        client = TelegramClient(session_path, api_id, api_hash)
        await client.connect()
        try:
            if code:
                try:
                    # إذا توافر phone_code_hash نمرّره (بعض نسخ Telethon تدعمه)
                    if phone_code_hash:
                        # Telethon قد يقبل sign_in(phone=..., code=..., phone_code_hash=...)
                        # نحاول تمرير الـ hash لتجنب الخطأ "need phone_code_hash"
                        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
                    else:
                        await client.sign_in(phone=phone, code=code)
                except telethon_errors.SessionPasswordNeededError:
                    await client.disconnect()
                    return {"ok": False, "password_needed": True}
                except Exception as e:
                    # في حالة فشل sign_in حاول إظهار الخطأ الكامل
                    await client.disconnect()
                    return {"ok": False, "error": str(e)}
            elif password:
                try:
                    await client.sign_in(password=password)
                except Exception as e:
                    await client.disconnect()
                    return {"ok": False, "error": str(e)}
            else:
                await client.disconnect()
                return {"ok": False, "error": "no_code_or_password"}
            await client.disconnect()
            return {"ok": True}
        except Exception as e:
            try:
                await client.disconnect()
            except:
                pass
            return {"ok": False, "error": str(e)}
    return asyncio.run(_signin())
bot_manager = BotManager()

# ----------------- Pyrogram Listener -----------------
class PyroListener:
    def __init__(self, session_name="listener"):
        self.session_basename = session_name
        self.session_path = os.path.join(SESSIONS_DIR, self.session_basename)
        self.client = None
        self.running = False
        self.lock = threading.Lock()

    def session_file_exists(self):
        return (os.path.exists(self.session_path) or
                os.path.exists(self.session_path + ".session") or
                os.path.exists(os.path.join(SESSIONS_DIR, self.session_basename)))

    def read_api_creds(self):
        try:
            cursor.execute("SELECT api_id, api_hash FROM sessions ORDER BY id DESC LIMIT 1")
            row = cursor.fetchone()
            if row:
                return int(row[0]), str(row[1])
        except Exception as e:
            logger.debug("read_api_creds error: %s", e)
        return None, None

    def start(self):
        with self.lock:
            if self.running:
                return True
            if not self.session_file_exists():
                logger.warning("لا توجد جلسة Pyrogram في sessions/ — ضع listener.session هنا")
                return False
            api_id, api_hash = self.read_api_creds()
            if not api_id or not api_hash:
                logger.warning("API_ID/API_HASH not found in DB. Use /setapi via bot to store them.")
                return False
            try:
                self.client = Client(name=self.session_path, api_id=api_id, api_hash=api_hash, workdir=SESSIONS_DIR)
            except Exception as e:
                logger.exception("Failed to create Pyrogram client: %s", e)
                return False

            @self.client.on_message(pyro_filters.channel)
            def on_channel_message(client, message):
                try:
                    chat = message.chat
                    username = (chat.username or "").lstrip("@")
                    if not username:
                        return
                    if username not in active_channels:
                        return
                    text = message.text or message.caption or ""
                    cleaned = ready_processing(text)
                    if not cleaned:
                        return
                    target = active_channels.get(username)
                    if not target:
                        return
                    send_to = target if str(target).startswith("@") else "@" + str(target)
                    try:
                        if bot_manager.bot:
                            bot_manager.bot.send_message(chat_id=send_to, text=cleaned)
                            status = "sent"
                        else:
                            status = "no-bot"
                    except Exception as e:
                        status = "error:" + str(e)
                    cursor.execute("INSERT INTO logs(ts, source, original, cleaned, target, status) VALUES (datetime('now'),?,?,?,?,?)",
                                   (username, text, cleaned, target, status))
                    conn.commit()
                except Exception:
                    logger.exception("on_channel_message error")

            def _run():
                try:
                    self.client.start()
                    self.running = True
                    logger.info("Pyrogram client started")
                    while self.running:
                        time.sleep(1)
                except Exception:
                    logger.exception("Pyrogram run error")
                finally:
                    try:
                        self.client.stop()
                    except:
                        pass
                    self.running = False

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            return True

    def stop(self):
        with self.lock:
            if not self.running:
                return True
            try:
                self.running = False
                if self.client:
                    self.client.stop()
                logger.info("Pyrogram stopped")
                return True
            except Exception:
                logger.exception("Failed to stop Pyrogram")
                return False

pyro_listener = PyroListener(session_name="listener")

# ----------------- Flask routes (read-only) -----------------
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/channels")
def api_channels():
    cursor.execute("SELECT channel_name, bot_target, active FROM channels ORDER BY id DESC")
    rows = cursor.fetchall()
    return jsonify([{"channel_name": r[0], "bot_target": r[1], "active": r[2]} for r in rows])

@app.route("/api/logs")
def api_logs():
    cursor.execute("SELECT ts, source, cleaned, target, status FROM logs ORDER BY id DESC LIMIT 200")
    rows = cursor.fetchall()
    return jsonify([{"ts": r[0], "source": r[1], "cleaned": r[2], "target": r[3], "status": r[4]} for r in rows])

@app.route("/webhook", methods=["POST"])
def webhook_route():
    data = request.get_json(force=True)
    if not data:
        return "no data", 400
    try:
        update = Update.de_json(data, bot_manager.bot)
        bot_manager.dispatcher.process_update(update)
        return "ok", 200
    except Exception:
        logger.exception("processing update")
        return "error", 500

# ----------------- start services -----------------
def start_services():
    bot_manager.start()
    load_active_channels()
    try:
        if pyro_listener.session_file_exists():
            pyro_listener.start()
    except Exception:
        logger.exception("auto-start pyro failed")

threading.Thread(target=start_services, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
