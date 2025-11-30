#!/usr/bin/env python3
import os, json, sqlite3, logging, hashlib, threading, time, re
from functools import wraps
from flask import Flask, request, render_template, redirect, url_for, session, jsonify, flash
from telegram import Bot, Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from pyrogram import Client, filters as pyro_filters
from pyrogram.errors import RPCError

# ---------------- Settings ----------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DB_FILE = os.path.join(BASE_DIR, "bot.db")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    cfg = json.load(f)

ADMIN_USER = cfg.get("admin_username", "admin")
ADMIN_PASS_SHA256 = cfg.get("admin_password_sha256", "")
BOT_TOKEN_ENV = cfg.get("bot_token_env_name", "BOT_TOKEN")
PORT = int(os.environ.get("PORT", cfg.get("listen_port", 10000)))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or cfg.get("webhook_url") or ""
FLASK_SECRET = os.environ.get("FLASK_SECRET") or os.urandom(24)

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

# ---------------- Database ----------------
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS channels(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 channel_name TEXT UNIQUE,
 bot_target TEXT,
 active INTEGER DEFAULT 0,
 created_at TEXT
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS sessions(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 api_id INTEGER,
 api_hash TEXT,
 session_name TEXT UNIQUE,
 created_at TEXT
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS logs(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 ts TEXT,
 source TEXT,
 original TEXT,
 cleaned TEXT,
 target TEXT,
 status TEXT
)
""")
conn.commit()

# in-memory caches
active_channels = {}
def load_active_from_db():
    active_channels.clear()
    cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
    for ch, bt in cursor.fetchall():
        active_channels[ch] = bt
load_active_from_db()

# ---------------- Flask ----------------
app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = FLASK_SECRET

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if session.get("logged_in"):
            return f(*a, **kw)
        return redirect(url_for("login"))
    return wrapper

def verify_admin(username, password):
    if username != ADMIN_USER:
        return False
    if not ADMIN_PASS_SHA256:
        return False
    return hashlib.sha256(password.encode("utf-8")).hexdigest() == ADMIN_PASS_SHA256

# ---------------- Cleaner ----------------
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'(https?://\S+|www\.\S+)', '', text)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'[^A-Za-z0-9 ]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

# ---------------- Webhook Bot Manager ----------------
class WebhookBot:
    def __init__(self):
        self.bot = None
        self.dispatcher = None
        self.running = False
        self.waiting_api = {}      # user_id -> True while expecting API creds
        self.waiting_channel = {}  # user_id -> state flow for adding channel

    def read_token(self):
        t = os.environ.get(BOT_TOKEN_ENV)
        if not t:
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    t = data.get("bot_token") or t
            except Exception:
                pass
        return t

    def get_webhook_info(self):
        if not self.bot:
            return {}
        try:
            return self.bot.get_webhook_info().to_dict()
        except Exception as e:
            return {"error": str(e)}

    def start(self):
        if self.running:
            return True
        token = self.read_token()
        if not token:
            logger.warning("No BOT token found (env or config.json)")
            return False
        try:
            self.bot = Bot(token=token)
            self.dispatcher = Dispatcher(self.bot, None, use_context=True)
        except Exception as e:
            logger.exception("Failed to init Bot: %s", e)
            return False

        # handlers
        self.dispatcher.add_handler(CommandHandler("start", self.cmd_start))
        self.dispatcher.add_handler(CommandHandler("setapi", self.cmd_setapi))
        self.dispatcher.add_handler(CallbackQueryHandler(self.on_callback))
        self.dispatcher.add_handler(MessageHandler(Filters.private & Filters.text, self.on_private))

        # set webhook if provided
        if WEBHOOK_URL:
            try:
                wh = WEBHOOK_URL.rstrip("/") + "/webhook"
                self.bot.set_webhook(wh)
                logger.info("Webhook set to %s", wh)
            except Exception as e:
                logger.exception("Failed to set webhook: %s", e)

        self.running = True
        logger.info("Webhook bot started")
        return True

    def stop(self):
        if not self.running:
            return True
        try:
            if self.bot and self.read_token():
                try:
                    self.bot.delete_webhook()
                except Exception:
                    pass
        finally:
            self.running = False
            self.bot = None
            self.dispatcher = None
            logger.info("Webhook bot stopped")
            return True

    # handlers
    def cmd_start(self, update, context):
        keyboard = [["🔧 الإدارة", "📡 الجلسات"], ["📺 القنوات", "🎯 الإرسال"], ["🧰 النظام"]]
        update.message.reply_text("مرحباً — اختر أحد الأقسام:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))

    def cmd_setapi(self, update, context):
        uid = update.effective_user.id
        self.waiting_api[uid] = True
        update.message.reply_text("أرسل API_ID و API_HASH مفصولين بمسافة واحدة.")


    def send_add_channel_button(self, chat_id):
        keyboard = [[InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="add_channel")]]
        reply = InlineKeyboardMarkup(keyboard)
        self.bot.send_message(chat_id=chat_id, text="اضغط لإضافة قناة:", reply_markup=reply)

    def on_callback(self, update, context):
        query = update.callback_query
        uid = query.from_user.id
        if query.data == "add_channel":
            self.waiting_channel[uid] = {"step": "channel_name"}
            query.answer()
            query.edit_message_text("أرسل اسم القناة (username بدون @).")


    def on_private(self, update, context):
        uid = update.effective_user.id
        text = (update.message.text or "").strip()
        uname = (update.effective_user.username or "").strip()

        # Only admin allowed to save API creds or add channels
        if uname.lower() != ADMIN_USER.lower():
            update.message.reply_text("غير مصرح لك.")
            return

        # receiving API creds
        if self.waiting_api.get(uid):
            parts = text.split()
            if len(parts) < 2:
                update.message.reply_text("خطأ في الصيغة. أرسل: API_ID API_HASH")
                return
            api_id, api_hash = parts[0], parts[1]
            try:
                cursor.execute("DELETE FROM sessions")
                cursor.execute("INSERT INTO sessions(api_id, api_hash, session_name, created_at) VALUES (?, ?, ?, datetime('now'))", (int(api_id), api_hash, 'listener'))
                conn.commit()
                update.message.reply_text("تم حفظ API_ID و API_HASH في قاعدة البيانات ✅")
                self.waiting_api.pop(uid, None)
            except Exception as e:
                update.message.reply_text(f"خطأ أثناء الحفظ: {e}")
            return

        # add-channel flow
        state = self.waiting_channel.get(uid)
        if state:
            step = state.get("step")
            if step == "channel_name":
                state["channel_name"] = text.lstrip("@")
                state["step"] = "bot_target"
                update.message.reply_text("أرسل الآن اسم البوت المستلم (بدون @).") 
                return
            elif step == "bot_target":
                channel_name = state.get("channel_name")
                bot_target = text.lstrip("@")
                try:
                    cursor.execute("INSERT OR REPLACE INTO channels(channel_name, bot_target, active, created_at) VALUES (?,?,1,datetime('now'))", (channel_name, bot_target))
                    conn.commit()
                    active_channels[channel_name] = bot_target
                    update.message.reply_text(f"تم إضافة القناة @{channel_name} وتفعيلها → ستُرسل الرسائل إلى @{bot_target}")
                except Exception as e:
                    update.message.reply_text(f"خطأ أثناء إضافة القناة: {e}")
                self.waiting_channel.pop(uid, None)
                return

        # simple commands via keyboard
        if text == "📺 القنوات": 
            rows = cursor.execute("SELECT channel_name, bot_target, active FROM channels ORDER BY id DESC").fetchall()
            if not rows:
                update.message.reply_text("لا توجد أي قنوات مضافة بعد.")
            else:
                msg = "القنوات المضافة:\n\n"
                for ch, target, active in rows:
                    status = "✅ مفعل" if active else "❌ متوقف"
                    msg += f"@{ch} → @{target} ({status})\n"
                update.message.reply_text(msg)
            return
        if text == "📡 الجلسات": 
            rows = cursor.execute("SELECT api_id, api_hash, session_name FROM sessions ORDER BY id DESC").fetchall()
            if not rows:
                update.message.reply_text("لا توجد جلسات مسجلة.")
            else:
                msg = "الجلسات:\n\n"
                for api_id, api_hash, session_name in rows:
                    msg += f"{session_name}: {api_id} / {api_hash}\n"
                update.message.reply_text(msg)
            return
        if text == "➕ إضافة قناة":
            self.waiting_channel[uid] = {"step":"channel_name"}
            update.message.reply_text("أرسل اسم القناة (username بدون @)")
            return
        if text == "⬅️ رجوع": 
            self.cmd_start(update, context)
            return

        update.message.reply_text("لم أفهم الرسالة. استخدم الأزرار.")

# instantiate bot manager
webhook_bot = WebhookBot()

# ---------------- Pyrogram Listener ----------------
class PyroListener:
    def __init__(self, session_name="listener"):
        self.session_basename = session_name
        self.session_path = os.path.join(SESSIONS_DIR, self.session_basename)
        self.client = None
        self.running = False
        self.lock = threading.Lock()

    def session_file_exists(self):
        return os.path.exists(self.session_path) or os.path.exists(self.session_path + ".session") or os.path.exists(os.path.join(SESSIONS_DIR, self.session_basename))

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
                logger.warning("لا توجد جلسة Pyrogram لإنشاء التنصت (upload listener.session into sessions/)")
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
                    cleaned = clean_text(text)
                    if not cleaned:
                        return
                    target = active_channels.get(username)
                    if not target:
                        return
                    send_to = target if str(target).startswith("@") else "@" + str(target)
                    try:
                        if webhook_bot.bot:
                            webhook_bot.bot.send_message(chat_id=send_to, text=cleaned)
                            status = "sent"
                        else:
                            status = "no-bot"
                    except Exception as e:
                        status = "error:" + str(e)
                    cursor.execute("INSERT INTO logs(ts, source, original, cleaned, target, status) VALUES (datetime('now'),?,?,?,?,?)", (username, text, cleaned, target, status))
                    conn.commit()
                except Exception:
                    logger.exception("on_channel_message error")


            def _run():
                try:
                    self.client.start()
                    self.running = True
                    logger.info("Pyrogram client started (session=%s)", self.session_path)
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

# ---------------- Flask routes ----------------
@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username","').strip()")
        # intentionally using provided verify_admin function below
    return render_template("login.html", error=None)

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/") 
@login_required
def index():
    cursor.execute("SELECT id, channel_name, bot_target, active, created_at FROM channels ORDER BY id DESC")
    rows = cursor.fetchall()
    bot_running = webhook_bot.running
    pyro_running = pyro_listener.running
    webhook_info = webhook_bot.get_webhook_info() if webhook_bot else {}
    return render_template("index.html", channels=rows, bot_running=bot_running, pyro_running=pyro_running, webhook_info=webhook_info)

@app.route("/logs") 
@login_required
def logs_page():
    cursor.execute("SELECT id, ts, source, cleaned, target, status FROM logs ORDER BY id DESC LIMIT 200")
    rows = cursor.fetchall()
    return render_template("logs.html", rows=rows)

# API endpoints
@app.route("/api/add_channel", methods=["POST"]) 
@login_required
def api_add_channel():
    channel = request.form.get("channel","').strip()")
    bot_target = request.form.get("target","').strip()")
    activate = 1 if request.form.get("activate") == "on" else 0
    if not channel or not bot_target:
        return jsonify({"ok": False, "error": "missing"}), 400
    try:
        cursor.execute("INSERT OR REPLACE INTO channels(channel_name, bot_target, active, created_at) VALUES (?,?,?,datetime('now'))", (channel, bot_target, activate))
        conn.commit()
        if activate:
            active_channels[channel] = bot_target
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/toggle_channel", methods=["POST"]) 
@login_required
def api_toggle_channel():
    channel = request.form.get("channel","').strip()") 
    action = request.form.get("action","start") 
    cursor.execute("SELECT bot_target, active FROM channels WHERE channel_name=?", (channel,)) 
    row = cursor.fetchone() 
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    bot_target, active = row
    if action == "start":
        cursor.execute("UPDATE channels SET active=1 WHERE channel_name=?", (channel,))
        conn.commit()
        active_channels[channel] = bot_target
        pyro_listener.start()
    else:
        cursor.execute("UPDATE channels SET active=0 WHERE channel_name=?", (channel,))
        conn.commit()
        active_channels.pop(channel, None)
    return jsonify({"ok": True})

@app.route("/api/set_api", methods=["POST"]) 
@login_required
def api_set_api():
    api_id = request.form.get("api_id","').strip()")
    api_hash = request.form.get("api_hash","').strip()")
    if not api_id or not api_hash:
        return jsonify({"ok": False, "error": "missing"}), 400
    cursor.execute("DELETE FROM sessions")
    cursor.execute("INSERT INTO sessions(api_id, api_hash, session_name) VALUES (?,?,?)", (int(api_id), api_hash, 'listener'))
    conn.commit()
    return jsonify({"ok": True})

@app.route("/webhook", methods=["POST"]) 
def webhook_route():
    data = request.get_json(force=True)
    if not data:
        return "no data", 400
    try:
        update = Update.de_json(data, webhook_bot.bot)
        webhook_bot.dispatcher.process_update(update)
        return "ok", 200
    except Exception:
        logger.exception("processing update")
        return "error", 500

# start services
def start_services():
    webhook_bot.start()
    load_active_from_db()
    if pyro_listener.session_file_exists():
        pyro_listener.start()

threading.Thread(target=start_services, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
