#!/usr/bin/env python3
import os, json, sqlite3, logging, hashlib, threading, time, re
from flask import Flask, request, render_template, redirect, url_for, session, jsonify
from telegram import Bot, Update
from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters

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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")   # very important for Render

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


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        u = request.form.get("username","").strip()
        p = request.form.get("password","").strip()
        if verify_admin(u, p):
            session["logged_in"] = True
            session["username"] = u
            return redirect(url_for("index"))
        error = "بيانات خاطئة"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    cursor.execute("SELECT id, channel_name, bot_target, active, created_at FROM channels ORDER BY id DESC")
    rows = cursor.fetchall()
    running = bot_manager.running
    webhook = bot_manager.get_webhook_info()
    return render_template("index.html", channels=rows, bot_running=running, webhook_info=webhook)

# ----------------- API -----------------

@app.route("/api/status")
@login_required
def api_status():
    return jsonify({
        "bot_running": bot_manager.running,
        "active_channels": list(active_channels.keys())
    })

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

    def get_webhook_info(self):
        if not self.bot:
            return {}
        try:
            return self.bot.get_webhook_info().to_dict()
        except:
            return {}

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
        self.dispatcher.add_handler(CommandHandler("setapi", self.cmd_setapi))
        self.dispatcher.add_handler(MessageHandler(Filters.text & Filters.private, self.private_text))

        # Webhook URL
        if not WEBHOOK_URL:
            logging.error("WEBHOOK_URL is not set in Render environment!")
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

    # Telegram handlers

    def cmd_start(self, update: Update, context):
        update.message.reply_text("Bot is running via Webhook on Render!")

    def cmd_setapi(self, update: Update, context):
        update.message.reply_text("Send API ID and API HASH separated by space.")

    def private_text(self, update: Update, context):
        user = update.effective_user
        text = update.message.text.strip()
        update.message.reply_text(f"Received: {text}")


bot_manager = BotManager()

# ----------------- Webhook route -----------------

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, bot_manager.bot)
    bot_manager.dispatcher.process_update(update)
    return "ok"

# ----------------- Start services -----------------

def start_bot():
    bot_manager.start()

threading.Thread(target=start_bot, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
