#!/usr/bin/env python3
import os, json, sqlite3, logging, hashlib, threading, time, re
from functools import wraps
from flask import Flask, render_template, request, jsonify, redirect, url_for, session
from telegram import Update
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext

# Basic config
BASE_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DB_FILE = os.path.join(BASE_DIR, "bot.db")

with open(CONFIG_FILE, "r", encoding="utf-8") as f:
    cfg = json.load(f)

ADMIN_USER = cfg.get("admin_username", "admin")
ADMIN_PASS_SHA256 = cfg.get("admin_password_sha256", "")
BOT_TOKEN_ENV = cfg.get("bot_token_env_name", "BOT_TOKEN")
PORT = int(os.environ.get("PORT", cfg.get("listen_port", 10000)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")

# Database init
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name TEXT UNIQUE,
    bot_target TEXT,
    active INTEGER DEFAULT 0,
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

# In-memory map of active channels (channel_username -> bot_target)
active_channels = {}
def load_active_from_db():
    cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
    for ch, bt in cursor.fetchall():
        active_channels[ch] = bt
load_active_from_db()

# Flask app
app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.environ.get("FLASK_SECRET", os.urandom(24))

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("logged_in"):
            return f(*args, **kwargs)
        return redirect(url_for("login"))
    return decorated

def verify_admin(username, password):
    if username != ADMIN_USER:
        return False
    h = hashlib.sha256(password.encode('utf-8')).hexdigest()
    return h == ADMIN_PASS_SHA256

@app.route("/login", methods=["GET","POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username","").strip()
        password = request.form.get("password","").strip()
        if verify_admin(username, password):
            session['logged_in'] = True
            session['username'] = username
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
    bot_running = bot_manager.is_running()
    return render_template("index.html", channels=rows, bot_running=bot_running)

@app.route("/logs")
@login_required
def logs():
    cursor.execute("SELECT id, ts, source, cleaned, target, status FROM logs ORDER BY id DESC LIMIT 200")
    rows = cursor.fetchall()
    return render_template("logs.html", rows=rows)

# AJAX endpoints
@app.route("/api/add_channel", methods=["POST"])
@login_required
def api_add_channel():
    channel = request.form.get("channel","").strip().lstrip("@")
    bot = request.form.get("bot","").strip().lstrip("@")
    if not channel or not bot:
        return jsonify({"ok":False, "error":"missing"}), 400
    try:
        cursor.execute("INSERT INTO channels(channel_name, bot_target, active, created_at) VALUES (?, ?, 0, datetime('now'))", (channel, bot))
        conn.commit()
        return jsonify({"ok":True, "channel":channel})
    except Exception as e:
        return jsonify({"ok":False, "error":str(e)}), 400

@app.route("/api/delete_channel", methods=["POST"])
@login_required
def api_delete_channel():
    channel = request.form.get("channel","").strip().lstrip("@")
    cursor.execute("DELETE FROM channels WHERE channel_name=?", (channel,))
    conn.commit()
    active_channels.pop(channel, None)
    return jsonify({"ok":True})

@app.route("/api/toggle_channel", methods=["POST"])
@login_required
def api_toggle_channel():
    channel = request.form.get("channel","").strip().lstrip("@")
    action = request.form.get("action","start")
    cursor.execute("SELECT bot_target, active FROM channels WHERE channel_name=?", (channel,))
    row = cursor.fetchone()
    if not row:
        return jsonify({"ok":False, "error":"not found"}), 404
    bot_target, active = row
    if action == "start":
        cursor.execute("UPDATE channels SET active=1 WHERE channel_name=?", (channel,))
        conn.commit()
        active_channels[channel] = bot_target
        return jsonify({"ok":True, "action":"started"})
    else:
        cursor.execute("UPDATE channels SET active=0 WHERE channel_name=?", (channel,))
        conn.commit()
        active_channels.pop(channel, None)
        return jsonify({"ok":True, "action":"stopped"})

@app.route("/api/set_bot_token", methods=["POST"])
@login_required
def api_set_bot_token():
    token = request.form.get("bot_token","").strip()
    if not token:
        return jsonify({"ok":False, "error":"missing"}), 400
    # save to config.json (avoid committing token to public repo)
    with open(CONFIG_FILE, "r+", encoding="utf-8") as f:
        data = json.load(f)
        data["bot_token"] = token
        f.seek(0); f.truncate(); json.dump(data, f, indent=4)
    return jsonify({"ok":True})

@app.route("/api/status")
@login_required
def api_status():
    return jsonify({"bot_running": bot_manager.is_running(), "active_channels": list(active_channels.keys())})

# Cleaning function
def clean_text(text: str) -> str:
    lines = text.splitlines()
    first_english_line = None
    for line in lines:
        if re.search(r'[A-Za-z0-9]', line) and not re.search(r'[\u0600-\u06FF]', line):
            first_english_line = line
            break
    text = first_english_line or ""
    text = re.sub(r'\\bcode\\b', "", text, flags=re.IGNORECASE)
    text = re.sub(r'(https?://\\S+|www\\.\\S+|\\S+\\.\\S+)', "", text)
    text = re.sub(r'@\\w+', "", text)
    text = re.sub(r'#\\w+', "", text)
    text = re.sub(r'([\\u0600-\\u06FF])\\d+', r'\\1', text)
    text = re.sub(r'([\\u0600-\\u06FF])\\s+\\d+', r'\\1', text)
    cleaned = re.sub(r'[^A-Za-z0-9 ]+', '', text)
    cleaned = re.sub(r'\\s+', ' ', cleaned).strip()
    return cleaned

# Bot manager using Bot API (python-telegram-bot)
class BotManager:
    def __init__(self):
        self.updater = None
        self.bot = None
        self.running = False

    def is_running(self):
        return self.running

    def start(self, token=None):
        if self.running:
            return True
        token = token or os.environ.get(BOT_TOKEN_ENV) or self._read_token_from_config()
        if not token:
            logging.warning("No BOT token set. Use dashboard to set bot token or set BOT_TOKEN env var.")
            return False
        self.updater = Updater(token, use_context=True, workers=8)
        self.bot = self.updater.bot
        dp = self.updater.dispatcher
        dp.add_handler(CommandHandler("start", self.cmd_start))
        dp.add_handler(CommandHandler("status", self.cmd_status))
        dp.add_handler(MessageHandler(Filters.chat_type.channel, self.channel_post))
        dp.add_handler(MessageHandler(Filters.private & Filters.text, self.private_text))
        self.updater.start_polling()
        self.running = True
        logging.info("BotManager started polling")
        return True

    def stop(self):
        if not self.running:
            return
        try:
            self.updater.stop()
        except Exception:
            pass
        self.running = False

    def _read_token_from_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get("bot_token")
        except:
            return None

    # Bot command handlers
    def cmd_start(self, update: Update, context: CallbackContext):
        update.message.reply_text("البوت يعمل. استخدم لوحة التحكم لإدارة القنوات.")

    def cmd_status(self, update: Update, context: CallbackContext):
        s = "running" if self.running else "stopped"
        update.message.reply_text(f"Bot status: {{s}}\\nActive: {{', '.join(active_channels.keys())}}")

    def private_text(self, update: Update, context: CallbackContext):
        if update.effective_user and str(update.effective_user.username or "").lower() == ADMIN_USER.lower():
            update.message.reply_text("أوامر الإدارة متاحة عبر اللوحة.")
        else:
            update.message.reply_text("استخدم لوحة التحكم لإدارة البوت.")

    def channel_post(self, update: Update, context: CallbackContext):
        try:
            chat = update.effective_chat
            if not chat or not chat.username:
                return
            src = chat.username
            if src not in active_channels:
                return
            text = update.effective_message.text or update.effective_message.caption or ""
            cleaned = clean_text(text)
            if not cleaned:
                return
            target = active_channels.get(src)
            if not target:
                return
            try:
                self.bot.send_message(chat_id=(target if str(target).startswith('@') else '@'+str(target)), text=cleaned)
                status = "sent"
            except Exception as e:
                status = "error:"+str(e)
            cur = conn.cursor()
            cur.execute("INSERT INTO logs(ts, source, original, cleaned, target, status) VALUES (datetime('now'),?,?,?,?,?)",
                        (src, text, cleaned, target, status))
            conn.commit()
        except Exception:
            logging.exception("channel_post error")

bot_manager = BotManager()

def start_services():
    bot_manager.start()
    load_active_from_db()

if __name__ == "__main__":
    threading.Thread(target=start_services, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
