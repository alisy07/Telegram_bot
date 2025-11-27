#!/usr/bin/env python3
# app.py -- مصححة ومنظمة لتشغيل Flask + صفحة /login + إدارة قنوات بسيطة + webhook bot manager

import os
import json
import sqlite3
import logging
import hashlib
import threading
import re
import atexit
from functools import wraps
from flask import (
    Flask, request, render_template, redirect, url_for, session, jsonify, flash
)

# Optional: telegram imports; guard in case المكتبة غير مثبتة
try:
    from telegram import Bot, Update
    from telegram.ext import Dispatcher, CommandHandler, MessageHandler, Filters
    TELEGRAM_AVAILABLE = True
except Exception:
    TELEGRAM_AVAILABLE = False

# ----------------- Settings -----------------

BASE_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
DB_FILE = os.path.join(BASE_DIR, "bot.db")

# Load config (safe default if file missing)
cfg = {}
if os.path.exists(CONFIG_FILE):
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        try:
            cfg = json.load(f)
        except Exception:
            cfg = {}

ADMIN_USER = cfg.get("admin_username", "admin")
ADMIN_PASS_SHA256 = cfg.get("admin_password_sha256", "")  # expected hex digest
BOT_TOKEN_ENV = cfg.get("bot_token_env_name", "BOT_TOKEN")
PORT = int(os.environ.get("PORT", cfg.get("listen_port", 10000)))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL") or cfg.get("webhook_url") or ""

# Flask app
app = Flask(__name__, static_folder="static", template_folder="templates")
# Secret key: from env or config or random fallback (in production set env FLASK_SECRET)
app.secret_key = os.environ.get("FLASK_SECRET") or cfg.get("flask_secret") or os.urandom(24)

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)

# ----------------- DB -----------------
# Ensure DB dir exists (if DB path in subdir)
db_dir = os.path.dirname(DB_FILE)
if db_dir and not os.path.exists(db_dir):
    os.makedirs(db_dir, exist_ok=True)

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

# in-memory active channels
active_channels = {}  # channel_name -> bot_target

def load_active_from_db():
    cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
    rows = cursor.fetchall()
    active_channels.clear()
    for ch, bt in rows:
        active_channels[ch] = bt

load_active_from_db()

# ----------------- Utilities -----------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get("logged_in"):
            return f(*args, **kwargs)
        return redirect(url_for("login"))
    return decorated

def verify_admin(username: str, password: str) -> bool:
    """Verify username and plaintext password against ADMIN_USER and ADMIN_PASS_SHA256."""
    if not username:
        return False
    if username != ADMIN_USER:
        return False
    if not ADMIN_PASS_SHA256:
        # no password set in config -> deny
        return False
    h = hashlib.sha256(password.encode("utf-8")).hexdigest()
    return h == ADMIN_PASS_SHA256

# ----------------- Routes: auth / pages -----------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = (request.form.get("password") or "").strip()
        if verify_admin(username, password):
            session['logged_in'] = True
            session['username'] = username
            flash("تم تسجيل الدخول", "success")
            return redirect(url_for("index"))
        error = "بيانات خاطئة"
        flash(error, "danger")
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    flash("تم تسجيل الخروج", "info")
    return redirect(url_for("login"))

@app.route("/")
@login_required
def index():
    cursor.execute("SELECT id, channel_name, bot_target, active, created_at FROM channels ORDER BY id DESC")
    rows = cursor.fetchall()
    bot_running = bot_manager.is_running()
    webhook_info = bot_manager.get_webhook_info() or {}
    return render_template("index.html", channels=rows, bot_running=bot_running, webhook_info=webhook_info)

@app.route("/logs")
@login_required
def logs():
    cursor.execute("SELECT id, ts, source, cleaned, target, status FROM logs ORDER BY id DESC LIMIT 200")
    rows = cursor.fetchall()
    return render_template("logs.html", rows=rows)

# ----------------- API endpoints -----------------

@app.route("/api/add_channel", methods=["POST"])
@login_required
def api_add_channel():
    channel = (request.form.get("channel") or "").strip().lstrip("@")
    bot = (request.form.get("bot") or "").strip().lstrip("@")
    if not channel or not bot:
        return jsonify({"ok": False, "error": "missing"}), 400
    try:
        cursor.execute(
            "INSERT INTO channels(channel_name, bot_target, active, created_at) VALUES (?, ?, 0, datetime('now'))",
            (channel, bot)
        )
        conn.commit()
        return jsonify({"ok": True, "channel": channel})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/delete_channel", methods=["POST"])
@login_required
def api_delete_channel():
    channel = (request.form.get("channel") or "").strip().lstrip("@")
    cursor.execute("DELETE FROM channels WHERE channel_name=?", (channel,))
    conn.commit()
    active_channels.pop(channel, None)
    return jsonify({"ok": True})

@app.route("/api/toggle_channel", methods=["POST"])
@login_required
def api_toggle_channel():
    channel = (request.form.get("channel") or "").strip().lstrip("@")
    action = (request.form.get("action") or "start").strip()
    cursor.execute("SELECT bot_target, active FROM channels WHERE channel_name=?", (channel,))
    row = cursor.fetchone()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    bot_target, active = row
    if action == "start":
        cursor.execute("UPDATE channels SET active=1 WHERE channel_name=?", (channel,))
        conn.commit()
        active_channels[channel] = bot_target
        return jsonify({"ok": True, "action": "started"})
    else:
        cursor.execute("UPDATE channels SET active=0 WHERE channel_name=?", (channel,))
        conn.commit()
        active_channels.pop(channel, None)
        return jsonify({"ok": True, "action": "stopped"})

@app.route("/api/set_bot_token", methods=["POST"])
@login_required
def api_set_bot_token():
    token = (request.form.get("bot_token") or "").strip()
    if not token:
        return jsonify({"ok": False, "error": "missing"}), 400
    # persist to config.json
    try:
        data = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["bot_token"] = token
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        # restart manager to pick up token
        threading.Thread(target=bot_manager.restart, daemon=True).start()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/set_api", methods=["POST"])
@login_required
def api_set_api():
    api_id = (request.form.get("api_id") or "").strip()
    api_hash = (request.form.get("api_hash") or "").strip()
    if not api_id or not api_hash:
        return jsonify({"ok": False, "error": "missing"}), 400
    try:
        data = {}
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        data["api_id"] = int(api_id)
        data["api_hash"] = api_hash
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/status")
@login_required
def api_status():
    return jsonify({"bot_running": bot_manager.is_running(), "active_channels": list(active_channels.keys())})

# ----------------- cleaning -----------------

def clean_text(text: str) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    first_english_line = None
    for line in lines:
        if re.search(r'[A-Za-z0-9]', line) and not re.search(r'[\u0600-\u06FF]', line):
            first_english_line = line
            break
    text = first_english_line or " ".join(lines)
    text = re.sub(r'\bcode\b', "", text, flags=re.IGNORECASE)
    text = re.sub(r'(https?://\S+|www\.\S+|\S+\.\S+)', "", text)
    text = re.sub(r'@\w+', "", text)
    text = re.sub(r'#\w+', "", text)
    text = re.sub(r'([\u0600-\u06FF])\d+', r'\1', text)
    text = re.sub(r'([\u0600-\u06FF])\s+\d+', r'\1', text)
    cleaned = re.sub(r'[^A-Za-z0-9 ]+', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

# ----------------- Bot Manager (webhook) -----------------

class BotManager:
    def __init__(self):
        self.bot = None
        self.dispatcher = None
        self.running = False
        self.lock = threading.Lock()
        self.token = None
        self.webhook_path = None

    def is_running(self):
        return self.running

    def _read_token(self):
        token = os.environ.get(BOT_TOKEN_ENV)
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    token = token or data.get("bot_token")
        except Exception:
            pass
        return token

    def get_webhook_info(self):
        if not self.bot:
            return {}
        try:
            if TELEGRAM_AVAILABLE:
                return self.bot.get_webhook_info().to_dict()
            return {}
        except Exception as e:
            return {"error": str(e)}

    def restart(self):
        with self.lock:
            try:
                if self.bot and self.token and TELEGRAM_AVAILABLE:
                    try:
                        self.bot.delete_webhook()
                    except Exception:
                        pass
            except Exception:
                pass
            self.bot = None
            self.dispatcher = None
            self.running = False
            # start fresh
            self.start()

    def start(self):
        with self.lock:
            if self.running:
                logger.info("Webhook bot already running")
                return True
            token = self._read_token()
            if not token:
                logger.warning("No BOT token found (env or config.json)")
                # mark running False but allow dashboard to set token later
                self.running = False
                return False
            self.token = token
            if not TELEGRAM_AVAILABLE:
                logger.warning("python-telegram-bot not available; bot functions disabled.")
                self.running = False
                return False

            try:
                self.bot = Bot(token=token)
                # Dispatcher for handling updates
                self.dispatcher = Dispatcher(self.bot, None, use_context=True)
                # add handlers
                self.dispatcher.add_handler(CommandHandler("start", self.cmd_start))
                self.dispatcher.add_handler(CommandHandler("status", self.cmd_status))
                self.dispatcher.add_handler(CommandHandler("setapi", self.cmd_setapi))
                self.dispatcher.add_handler(MessageHandler(Filters.text & Filters.private, self.private_text))
                self.dispatcher.add_handler(MessageHandler(Filters.chat_type.channel, self.channel_post))

                # set webhook to WEBHOOK_URL + /hook/<tokenprefix>
                base_url = os.environ.get("WEBHOOK_URL") or WEBHOOK_URL
                if not base_url:
                    logger.warning("No WEBHOOK_URL set; cannot register webhook automatically. Use dashboard to set bot token and set webhook manually.")
                    self.running = True
                    return True
                self.webhook_path = "/hook/" + self.token.split(":")[0]
                full_url = base_url.rstrip("/") + self.webhook_path
                try:
                    self.bot.set_webhook(url=full_url)
                    logger.info("Webhook set to %s", full_url)
                except Exception as e:
                    logger.exception("Failed to set webhook: %s", e)
                self.running = True
                return True
            except Exception as e:
                logger.exception("Failed to start bot: %s", e)
                self.running = False
                return False

    # handlers
    def cmd_start(self, update: Update, context):
        try:
            update.message.reply_text("Bot is running. Use dashboard to manage channels.")
        except Exception:
            pass

    def cmd_status(self, update: Update, context):
        try:
            s = "running" if self.running else "stopped"
            update.message.reply_text(f"Bot status: {s}")
        except Exception:
            pass

    def cmd_setapi(self, update: Update, context):
        user = update.effective_user
        if user and user.username and user.username.lower() == ADMIN_USER.lower():
            update.message.reply_text("Send API as: api_id api_hash")
        else:
            update.message.reply_text("Only admin can set API via bot.")

    def private_text(self, update: Update, context):
        user = update.effective_user
        text = (update.message.text or "").strip()
        if user and user.username and user.username.lower() == ADMIN_USER.lower():
            if " " in text:
                try:
                    api_id, api_hash = text.split(" ", 1)
                    with open(CONFIG_FILE, "r+", encoding="utf-8") as f:
                        data = json.load(f)
                        data["api_id"] = int(api_id)
                        data["api_hash"] = api_hash
                        f.seek(0); f.truncate(); json.dump(data, f, indent=4)
                    update.message.reply_text("Saved API in config.json")
                except Exception as e:
                    update.message.reply_text(f"Error: {e}")
            else:
                update.message.reply_text("Invalid format. Use: api_id api_hash")
        else:
            update.message.reply_text("Not authorized.")

    def channel_post(self, update: Update, context):
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
                send_to = target if str(target).startswith("@") else "@" + str(target)
                self.bot.send_message(chat_id=send_to, text=cleaned)
                status = "sent"
            except Exception as e:
                status = "error:" + str(e)
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO logs(ts, source, original, cleaned, target, status) VALUES (datetime('now'),?,?,?,?,?)",
                (src, text, cleaned, target, status)
            )
            conn.commit()
        except Exception:
            logger.exception("channel_post error")


bot_manager = BotManager()

# webhook endpoint to receive updates (token prefix route)
@app.route("/hook/<token_prefix>", methods=["POST"])
def webhook_handler(token_prefix):
    # Token prefix in path should match current token prefix
    data = request.get_json(force=True)
    if not data:
        return "no data", 400
    if not bot_manager.bot:
        return "bot not initialized", 500
    try:
        update = Update.de_json(data, bot_manager.bot)
        bot_manager.dispatcher.process_update(update)
    except Exception:
        logger.exception("processing update")
        return "error", 500
    return "ok", 200

# ----------------- Start services -----------------

def start_services():
    # start bot manager (non-blocking)
    bot_manager.start()
    load_active_from_db()

# Ensure clean shutdown
def shutdown():
    try:
        if bot_manager.is_running():
            bot_manager.restart()  # will call delete_webhook and stop
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass

atexit.register(shutdown)

# Start background services on import if running as script
if __name__ == "__main__":
    # start services in background
    t = threading.Thread(target=start_services, daemon=True)
    t.start()
    # run flask
    host = os.environ.get("FLASK_HOST", "0.0.0.0")
    port = int(os.environ.get("FLASK_PORT", PORT))
    debug = os.environ.get("FLASK_DEBUG", "False").lower() == "true"
    logger.info("Starting Flask on %s:%s (debug=%s)", host, port, debug)
    app.run(host=host, port=port, debug=debug)
