"""
Telegram Forwarder with Web Dashboard (Bot-token-first flow)

- Uses python-telegram-bot (for bot commands) so the bot works with BOT_TOKEN only.
- When user provides api_id and api_hash via /setapi, a Telethon client is started to monitor channels.
- Flask provides a simple web dashboard to add/delete/start/stop channels.
- SQLite stores channels and their active state.

Instructions:
- Set BOT_TOKEN environment variable on Render.
- Deploy project and access web dashboard via Render URL.
- Use the Telegram bot to send /setapi to enter api_id and api_hash (from my.telegram.org).
"""

import os, json, sqlite3, logging, re, threading, asyncio
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for

# python-telegram-bot for bot commands (works with BOT_TOKEN only)
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, ConversationHandler, CallbackQueryHandler

# Telethon for monitoring once api_id/api_hash provided
from telethon import TelegramClient, events

logging.basicConfig(level=logging.INFO, format='%(asctime)s — %(levelname)s — %(message)s')

CONFIG_FILE = "config.json"
DB_PATH = "bot.db"
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", "10000"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    logging.warning("BOT_TOKEN not set in environment variables. The bot won't start without it.")

# ======= load/create config.json =======
if not os.path.exists(CONFIG_FILE):
    config = {"api_id": 0, "api_hash": "", "session_name": "session", "owner_id": 0}
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
else:
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

# ======= DB setup =======
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name TEXT NOT NULL UNIQUE,
    bot_target TEXT NOT NULL,
    active INTEGER DEFAULT 0,
    created_at TEXT
)
""")
conn.commit()

# Active channels in memory
active_channels = {}
cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
for ch, bt in cursor.fetchall():
    active_channels[ch] = bt

# Telethon client placeholder
tele_client = None
tele_loop = None

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

def clean_text(text: str) -> str:
    lines = text.splitlines()
    first_english_line = None
    for line in lines:
        if re.search(r'[A-Za-z0-9]', line) and not re.search(r'[\u0600-\u06FF]', line):
            first_english_line = line
            break
    text = first_english_line or ""
    text = re.sub(r'\bcode\b', "", text, flags=re.IGNORECASE)
    text = re.sub(r'(https?://\S+|www\.\S+|\S+\.\S+)', "", text)
    text = re.sub(r'@\w+', "", text)
    text = re.sub(r'#\w+', "", text)
    text = re.sub(r'([\u0600-\u06FF])\d+', r'\1', text)
    text = re.sub(r'([\u0600-\u06FF])\s+\d+', r'\1', text)
    cleaned = re.sub(r'[^A-Za-z0-9 ]+', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

# ========= Flask dashboard ==========
app = Flask(__name__)
TEMPLATE = """
<!doctype html>
<title>Telegram Forward Bot Dashboard</title>
<h2>Channels</h2>
<form action="{{ url_for('add') }}" method="post">
  <input name="channel" placeholder="channel (without @)" required>
  <input name="bot" placeholder="bot target (without @)" required>
  <button type="submit">Add</button>
</form>
<table border=1 cellpadding=6>
<tr><th>ID</th><th>Channel</th><th>Target Bot</th><th>Active</th><th>Actions</th></tr>
{% for row in rows %}
<tr>
  <td>{{ row[0] }}</td>
  <td>{{ row[1] }}</td>
  <td>{{ row[2] }}</td>
  <td>{{ 'Yes' if row[3] else 'No' }}</td>
  <td>
    <a href="{{ url_for('toggle', id=row[0]) }}">{{ 'Stop' if row[3] else 'Start' }}</a> |
    <a href="{{ url_for('delete', id=row[0]) }}" onclick="return confirm('Delete?')">Delete</a>
  </td>
</tr>
{% endfor %}
</table>
<hr>
<p>Use the Telegram bot to set api_id/api_hash via <code>/setapi</code>. BOT_TOKEN must be set as an environment variable.</p>
"""

@app.route("/")
def index():
    cur = conn.cursor()
    cur.execute("SELECT id, channel_name, bot_target, active FROM channels ORDER BY id DESC")
    rows = cur.fetchall()
    return render_template_string(TEMPLATE, rows=rows)

@app.route("/add", methods=["POST"])
def add():
    channel = request.form["channel"].strip()
    bot = request.form["bot"].strip()
    try:
        cursor.execute("INSERT INTO channels(channel_name, bot_target, active, created_at) VALUES (?, ?, 0, ?)",
                       (channel, bot, datetime.utcnow().isoformat()))
        conn.commit()
    except sqlite3.IntegrityError:
        pass
    return redirect(url_for("index"))

@app.route("/delete/<int:id>")
def delete(id):
    cursor.execute("SELECT channel_name FROM channels WHERE id=?", (id,))
    row = cursor.fetchone()
    if row:
        active_channels.pop(row[0], None)
    cursor.execute("DELETE FROM channels WHERE id=?", (id,))
    conn.commit()
    return redirect(url_for("index"))

@app.route("/toggle/<int:id>")
def toggle(id):
    cursor.execute("SELECT channel_name, bot_target, active FROM channels WHERE id=?", (id,))
    row = cursor.fetchone()
    if not row:
        return redirect(url_for("index"))
    name, bot, active = row
    if active:
        active_channels.pop(name, None)
        cursor.execute("UPDATE channels SET active=0 WHERE id=?", (id,))
    else:
        active_channels[name] = bot
        cursor.execute("UPDATE channels SET active=1 WHERE id=?", (id,))
    conn.commit()
    return redirect(url_for("index"))

# ========= Bot (python-telegram-bot) handlers ==========
SETAPI_APIID, SETAPI_APIHASH = range(2)

def start_command(update: Update, context: CallbackContext):
    kb = [[InlineKeyboardButton("New", callback_data="new")]]
    # add channels as buttons
    cursor.execute("SELECT channel_name FROM channels ORDER BY id DESC")
    for (ch,) in cursor.fetchall():
        kb.append([InlineKeyboardButton(ch, callback_data=ch)])
    update.message.reply_text("Choose channel or create new:", reply_markup=InlineKeyboardMarkup(kb))

def setapi_start(update: Update, context: CallbackContext):
    update.message.reply_text("Send api_id (number):")
    return SETAPI_APIID

def setapi_apiid(update: Update, context: CallbackContext):
    try:
        api_id = int(update.message.text.strip())
    except:
        update.message.reply_text("api_id must be a number. Send /setapi to try again.")
        return ConversationHandler.END
    context.user_data["api_id"] = api_id
    update.message.reply_text("Send api_hash:")
    return SETAPI_APIHASH

def setapi_apihash(update: Update, context: CallbackContext):
    api_hash = update.message.text.strip()
    context.user_data["api_hash"] = api_hash
    # save to config and start telethon
    config["api_id"] = context.user_data["api_id"]
    config["api_hash"] = context.user_data["api_hash"]
    save_config()
    update.message.reply_text("Saved api_id and api_hash. Attempting to start Telethon client...")
    # start telethon client in background
    threading.Thread(target=start_telethon_background, daemon=True).start()
    return ConversationHandler.END

def button_callback(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    query.answer()
    if data == "new":
        query.message.reply_text("Send channel username (without @):")
        context.user_data["adding_channel"] = True
    else:
        # toggle active
        cursor.execute("SELECT bot_target, active FROM channels WHERE channel_name=?", (data,))
        row = cursor.fetchone()
        if not row:
            query.message.reply_text("Channel not found.")
            return
        bot_target, active = row
        if active:
            active_channels.pop(data, None)
            cursor.execute("UPDATE channels SET active=0 WHERE channel_name=?", (data,))
            conn.commit()
            query.message.reply_text(f"Stopped watching {data}")
        else:
            active_channels[data] = bot_target
            cursor.execute("UPDATE channels SET active=1 WHERE channel_name=?", (data,))
            conn.commit()
            query.message.reply_text(f"Started watching {data}")

def text_message_handler(update: Update, context: CallbackContext):
    # used for adding channel after pressing New
    if context.user_data.get("adding_channel"):
        channel = update.message.text.strip()
        context.user_data["adding_channel"] = False
        update.message.reply_text("Send target bot username (without @):")
        context.user_data["adding_channel_channel"] = channel
        context.user_data["expect_target"] = True
        return
    if context.user_data.get("expect_target"):
        target = update.message.text.strip()
        channel = context.user_data.pop("adding_channel_channel", None)
        context.user_data["expect_target"] = False
        try:
            cursor.execute("INSERT INTO channels(channel_name, bot_target, active, created_at) VALUES (?, ?, 0, ?)",
                           (channel, target, datetime.utcnow().isoformat()))
            conn.commit()
            update.message.reply_text(f"Saved {channel} -> {target}")
        except sqlite3.IntegrityError:
            update.message.reply_text("Channel already exists")

# ========= Start Telethon client (background) =========
def start_telethon_background():
    global tele_client, tele_loop
    if not config.get("api_id") or not config.get("api_hash"):
        logging.error("api_id/api_hash missing; cannot start Telethon.")
        return
    try:
        tele_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(tele_loop)
        tele_client = TelegramClient(config["session_name"], config["api_id"], config["api_hash"])
        tele_loop.run_until_complete(tele_client.start(bot_token=BOT_TOKEN))
        logging.info("Telethon started. Registering handlers...")
        # register message handler
        @tele_client.on(events.NewMessage())
        async def watcher(event):
            if not event.chat or not getattr(event.chat, "username", None):
                return
            src = event.chat.username
            if src not in active_channels:
                return
            cleaned = clean_text(event.raw_text or "")
            if not cleaned:
                return
            try:
                await tele_client.send_message(active_channels[src] if active_channels[src].startswith("@") else f"@{active_channels[src]}", cleaned)
            except Exception:
                logging.exception("Forward error")

        tele_loop.run_forever()
    except Exception:
        logging.exception("Failed to start Telethon client")

# ========= Run Flask and Bot ==========
def run_flask():
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True)

def run_telegram_bot():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN missing; telegram bot not starting.")
        return
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start_command))
    conv = ConversationHandler(
        entry_points=[CommandHandler("setapi", setapi_start)],
        states={
            SETAPI_APIID: [MessageHandler(Filters.text & ~Filters.command, setapi_apiid)],
            SETAPI_APIHASH: [MessageHandler(Filters.text & ~Filters.command, setapi_apihash)],
        },
        fallbacks=[]
    )
    dp.add_handler(conv)
    dp.add_handler(CallbackQueryHandler(button_callback))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_message_handler))

    updater.start_polling()
    updater.idle()

# ========= Main entry ==========
if __name__ == "__main__":
    # start flask in thread
    threading.Thread(target=run_flask, daemon=True).start()
    # start telegram bot (polling)
    threading.Thread(target=run_telegram_bot, daemon=True).start()
    print("Dashboard + Telegram bot started. Use /setapi in Telegram to provide api_id/api_hash.")
    # keep main thread alive
    while True:
        try:
            threading.Event().wait(3600)
        except KeyboardInterrupt:
            break
