# main.py
#!/usr/bin/env python3
import os, json, sqlite3, logging, threading, time, re
from flask import Flask, request, render_template, jsonify
from telegram import Bot, Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from pyrogram import Client, filters as pyro_filters

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

# ----------------- DB -----------------
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
conn.commit()

# in-memory cache
active_channels = {}
def load_active_channels():
    active_channels.clear()
    cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
    for ch, bt in cursor.fetchall():
        active_channels[ch] = bt
load_active_channels()

# ----------------- Flask (read-only) -----------------
app = Flask(__name__, template_folder="templates", static_folder="static")

def clean_text(text):
    if not text:
        return ""
    text = re.sub(r'(https?://\S+|www\.\S+)', '', text)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'[^A-Za-z0-9 ]+', '', text)
    return re.sub(r'\s+', ' ', text).strip()

# ----------------- Bot manager -----------------
class BotManager:
    def __init__(self):
        self.bot = None
        self.dispatcher = None
        self.running = False
        # states: waiting for input from admin via private chat
        self.waiting_api = {}      # user_id -> True
        self.waiting_channel = {}  # user_id -> {"step":..., "channel_name":...}

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
        logger.info("BotManager started")
        return True

    # --- command handlers ---
    def cmd_start(self, update, context):
        user = update.effective_user
        if user and user.id == ADMIN_TELEGRAM_ID:
            keyboard = [["📺 القنوات", "📡 الجلسات"], ["📝 السجلات", "➕ إضافة قناة"]]
            update.message.reply_text("مرحبا مدير النظام — اختر:", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
        else:
            update.message.reply_text("غير مصرح لك.")

    def cmd_setapi(self, update, context):
        user = update.effective_user
        if not (user and user.id == ADMIN_TELEGRAM_ID):
            update.message.reply_text("غير مصرح لك.")
            return
        self.waiting_api[user.id] = True
        update.message.reply_text("أرسل API_ID و API_HASH مفصولين بمسافة واحدة (مثال: 123456 abcdef1234).")

    def send_add_channel_button(self, chat_id):
        kb = [[InlineKeyboardButton("➕ إضافة قناة جديدة", callback_data="add_channel")]]
        self.bot.send_message(chat_id=chat_id, text="اضغط لإضافة قناة:", reply_markup=InlineKeyboardMarkup(kb))

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
        username = (update.effective_user.username or "").strip()

        if uid != ADMIN_TELEGRAM_ID:
            update.message.reply_text("غير مصرح لك.")
            return

        # API saving flow
        if self.waiting_api.get(uid):
            parts = text.split()
            if len(parts) < 2:
                update.message.reply_text("خطأ في الصيغة. أرسل: API_ID API_HASH")
                return
            api_id, api_hash = parts[0], parts[1]
            try:
                cursor.execute("DELETE FROM sessions")
                cursor.execute("INSERT INTO sessions(api_id, api_hash, session_name, created_at) VALUES (?, ?, ?, datetime('now'))",
                               (int(api_id), api_hash, "listener"))
                conn.commit()
                update.message.reply_text("تم حفظ API_ID و API_HASH في قاعدة البيانات ✅\nالخطوة التالية: أنشئ الجلسة محلياً باستخدام create_session.py ثم ارفع ملف listener.session إلى مجلد sessions/")
                self.waiting_api.pop(uid, None)
            except Exception as e:
                update.message.reply_text(f"خطأ أثناء الحفظ: {e}")
            return

        # add-channel flow (multi-step)
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

        # keyboard commands
        if text == "📺 القنوات":
            rows = cursor.execute("SELECT channel_name, bot_target, active FROM channels ORDER BY id DESC").fetchall()
            if not rows:
                update.message.reply_text("لا توجد قنوات مضافة.")
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

        if text == "📝 السجلات":
            rows = cursor.execute("SELECT ts, source, cleaned, target, status FROM logs ORDER BY id DESC LIMIT 20").fetchall()
            if not rows:
                update.message.reply_text("لا توجد سجلات بعد.")
            else:
                msg = "آخر السجلات:\n\n"
                for ts, source, cleaned, target, status in rows:
                    msg += f"{ts} | @{source} → @{target} | {status}\n{cleaned}\n\n"
                update.message.reply_text(msg)
            return

        if text == "➕ إضافة قناة":
            self.waiting_channel[uid] = {"step": "channel_name"}
            update.message.reply_text("أرسل اسم القناة (username بدون @)")
            return

        update.message.reply_text("استخدم الأزرار أو الأوامر المتاحة (مثل /setapi).")

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
        # check multiple possible session file patterns
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
                    cleaned = clean_text(text)
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
    # start bot manager
    bot_manager.start()
    # load channels from DB into memory
    load_active_channels()
    # start pyrogram if session file exists
    if pyro_listener.session_file_exists():
        pyro_listener.start()

threading.Thread(target=start_services, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
