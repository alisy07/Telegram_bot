#!/usr/bin/env python3
import os, json, sqlite3, logging, hashlib, threading, time, re
from flask import Flask, request, render_template, redirect, url_for, session, jsonify
from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters
from pyrogram import Client

# ----------------- Settings -----------------
BASE_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DB_FILE = os.path.join(BASE_DIR, "bot.db")

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    cfg = json.load(f)

ADMIN_USER = cfg.get("admin_username")
ADMIN_PASS_SHA256 = cfg.get("admin_password_sha256")
BOT_TOKEN_ENV = cfg.get("bot_token_env_name", "BOT_TOKEN")
PORT = int(os.environ.get("PORT", cfg.get("listen_port", 10000)))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")

logging.basicConfig(level=logging.INFO)

# ----------------- DB -----------------
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

active_channels = {}
active_clients = {}  # Pyrogram clients


def load_active_from_db():
    cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
    for ch, bt in cursor.fetchall():
        active_channels[ch] = bt

load_active_from_db()

# ----------------- Flask -----------------
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24))


def login_required(f):
    from functools import wraps
    @wraps(f)
    def decorated(*a, **kw):
        if session.get("logged_in"):
            return f(*a, **kw)
        return redirect(url_for("login"))
    return decorated


def verify_admin(username, password):
    if username != ADMIN_USER:
        return False
    h = hashlib.sha256(password.encode()).hexdigest()
    return h == ADMIN_PASS_SHA256

# ----------------- Keyboards -----------------
def main_menu():
    keyboard = [["🔧 الإدارة", "📡 الجلسات"],
                ["📺 القنوات", "🎯 الإرسال"],
                ["🧰 النظام"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def channels_menu():
    keyboard = [["➕ إضافة قناة", "📃 عرض القنوات"],
                ["🔄 تبديل حالة قناة", "🗑️ حذف قناة"],
                ["⬅️ رجوع"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ----------------- Cleaner -----------------
def clean_text(text):
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'@\w+', '', text)
    text = re.sub(r'[^A-Za-z0-9 ]+', '', text)
    return text.strip()

# ----------------- Bot Manager -----------------
class BotManager:
    def __init__(self):
        self.bot = None
        self.dispatcher = None
        self.running = False
        self.token = None

    def start(self):
        if self.running:
            return True

        token = os.environ.get(BOT_TOKEN_ENV)
        if not token:
            logging.error("BOT TOKEN NOT FOUND!")
            return False

        self.token = token
        self.bot = Bot(token=token)

        self.dispatcher = Dispatcher(self.bot, None, workers=4, use_context=True)
        self.dispatcher.add_handler(CommandHandler("start", self.cmd_start))
        self.dispatcher.add_handler(MessageHandler(Filters.text & Filters.private, self.private_text))

        if not WEBHOOK_URL:
            logging.error("WEBHOOK_URL is not set!")
            return False

        webhook_url = WEBHOOK_URL.rstrip("/") + "/webhook"
        try:
            self.bot.set_webhook(webhook_url)
            logging.info("Webhook set to: %s", webhook_url)
        except Exception as e:
            logging.error("Cannot set webhook: %s", e)
            return False

        self.running = True
        return True

    def cmd_start(self, update, context):
        update.message.reply_text(
            "مرحباً بك في لوحة الإدارة 👋\nاختر أحد الأقسام:",
            reply_markup=main_menu()
        )

    def private_text(self, update, context):
        msg = update.message.text

        if msg == "📺 القنوات":
            update.message.reply_text("القنوات:", reply_markup=channels_menu())
        elif msg == "➕ إضافة قناة":
            update.message.reply_text("أرسل اسم القناة مع البوت الهدف بالصيغة:\nchannel_name bot_target")
        elif " " in msg and msg.startswith("@"):
            # assume channel + bot
            ch, target = msg.split(" ",1)
            try:
                cursor.execute("INSERT INTO channels(channel_name, bot_target, active, created_at) VALUES (?,?,1,datetime('now'))",(ch,target))
                conn.commit()
                active_channels[ch] = target
                start_listening(ch)
                update.message.reply_text(f"تم إضافة {ch} وتفعيل التنصت على البوت {target}")
            except Exception as e:
                update.message.reply_text(f"خطأ: {e}")
        elif msg == "⬅️ رجوع":
            update.message.reply_text("القائمة الرئيسية:", reply_markup=main_menu())
        else:
            update.message.reply_text(f"الأمر: {msg}")

bot_manager = BotManager()

# ----------------- Pyrogram Listening -----------------
def start_listening(channel_name):
    # سيستخدم أول جلسة موجودة (يمكن تعديل لإختيار الجلسة)
    cursor.execute("SELECT api_id, api_hash, session_name FROM sessions ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    if not row:
        logging.warning("لا توجد جلسة Pyrogram لإنشاء التنصت")
        return
    api_id, api_hash, session_name = row
    if session_name in active_clients:
        client = active_clients[session_name]
    else:
        client = Client(session_name, api_id=api_id, api_hash=api_hash)
        client.start()
        active_clients[session_name] = client

    @client.on_message()
    def handle_message(client, message):
        if not hasattr(message.chat, "username") or message.chat.username != channel_name.lstrip("@"):
            return
        cleaned = clean_text(message.text or message.caption or "")
        if not cleaned:
            return
        target = active_channels.get(channel_name)
        if not target:
            return
        try:
            bot_manager.bot.send_message(chat_id=target, text=cleaned)
            status = "sent"
        except Exception as e:
            status = f"error:{e}"
        cursor.execute("INSERT INTO logs(ts, source, original, cleaned, target, status) VALUES (datetime('now'),?,?,?,?,?)",
                       (channel_name, message.text, cleaned, target, status))
        conn.commit()

# ----------------- Webhook -----------------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot_manager.bot)
    bot_manager.dispatcher.process_update(update)
    return "ok"

# ----------------- Start services -----------------
threading.Thread(target=bot_manager.start, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
