import json, sqlite3, asyncio, logging, re, os, threading
from telethon import TelegramClient, events, Button
from flask import Flask, render_template_string, request, redirect, url_for
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s — %(levelname)s — %(message)s')

CONFIG_FILE = "config.json"
DB_PATH = "bot.db"
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", 10000))

# ======= load/create config.json =======
if not os.path.exists(CONFIG_FILE):
    config = {
        "api_id": 0,
        "api_hash": "",
        "bot_token": "",
        "session_name": "session",
        "owner_id": 0
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
else:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)

# ======= DB setup =======
os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
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

# ======= create client safely =======
def create_client():
    if config.get("api_id") and config.get("api_hash") and config.get("bot_token"):
        logging.info("Starting real Telegram client")
        return TelegramClient(config["session_name"], config["api_id"], config["api_hash"]).start(bot_token=config["bot_token"])
    logging.warning("No API credentials: running in setup-only mode")
    client = TelegramClient("temp_session", 11111, "temp_hash")
    client.start = lambda *a, **k: client
    return client

client = create_client()

# ======= helper functions =======
def save_config():
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
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

async def send_to_target(text, bot_target):
    if not bot_target:
        return "no target"
    if not bot_target.startswith("@"):
        bot_target = f"@{bot_target}"
    try:
        await client.send_message(bot_target, text)
        return "ok"
    except Exception as e:
        logging.exception("send error")
        return str(e)

# ======= active channels in-memory =======
active_channels = {}  # channel_name -> bot_target

# restore active state from DB
cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
rows = cursor.fetchall()
for ch, bt in rows:
    active_channels[ch] = bt

# ======= Telethon handlers =======
@client.on(events.NewMessage(pattern="/setapi"))
async def handle_setapi(event):
    user_id = event.sender_id
    config["owner_id"] = user_id
    await event.respond("Send api_id")
    async with client.conversation(user_id) as conv:
        m1 = await conv.get_response()
        try:
            config["api_id"] = int(m1.text.strip())
        except:
            await event.respond("api_id must be an integer")
            return
        await event.respond("Send api_hash")
        m2 = await conv.get_response()
        config["api_hash"] = m2.text.strip()
        await event.respond("Send bot_token")
        m3 = await conv.get_response()
        config["bot_token"] = m3.text.strip()
        save_config()
        await event.respond("Saved config. Please restart service to apply real client.")

@client.on(events.NewMessage(pattern="/start"))
async def handle_start(event):
    user_id = event.sender_id
    config["owner_id"] = user_id
    save_config()
    buttons = [[Button.inline("New", b"new")]]
    cursor.execute("SELECT channel_name FROM channels ORDER BY id DESC")
    for (ch,) in cursor.fetchall():
        buttons.append([Button.inline(ch, ch.encode())])
    await event.respond("Choose channel or create new:", buttons=buttons)

@client.on(events.CallbackQuery(data=b"new"))
async def handle_new_cb(event):
    await event.respond("Enter channel username (without @)")
    async with client.conversation(event.sender_id) as conv:
        ch = await conv.get_response()
        channel_name = ch.text.strip()
        await event.respond("Enter target bot username (without @)")
        bt = await conv.get_response()
        bot_target = bt.text.strip()
        try:
            cursor.execute("INSERT INTO channels(channel_name, bot_target, active, created_at) VALUES (?, ?, 0, ?)",
                           (channel_name, bot_target, datetime.utcnow().isoformat()))
            conn.commit()
            await event.respond(f"Saved {channel_name} -> {bot_target}")
        except sqlite3.IntegrityError:
            await event.respond("Channel already exists")

@client.on(events.CallbackQuery)
async def handle_channel_cb(event):
    data = event.data
    try:
        name = data.decode()
    except:
        await event.answer("Unknown action")
        return
    cursor.execute("SELECT bot_target, active FROM channels WHERE channel_name=?", (name,))
    row = cursor.fetchone()
    if not row:
        await event.answer("Not found")
        return
    bot_target, active = row
    if name in active_channels:
        active_channels.pop(name, None)
        cursor.execute("UPDATE channels SET active=0 WHERE channel_name=?", (name,))
        conn.commit()
        await event.answer(f"Stopped watching {name}")
    else:
        active_channels[name] = bot_target
        cursor.execute("UPDATE channels SET active=1 WHERE channel_name=?", (name,))
        conn.commit()
        await event.answer(f"Started watching {name}")

@client.on(events.NewMessage())
async def watcher(event):
    if not event.chat or not getattr(event.chat, "username", None):
        return
    src = event.chat.username
    if src not in active_channels:
        return
    text = (event.raw_text or "").strip()
    if not text:
        return
    cleaned = clean_text(text)
    if not cleaned:
        return
    await send_to_target(cleaned, active_channels[src])

# ======= Flask web UI =======
app = Flask(name)
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
<p>Config saved to <code>config.json</code>. Use /setapi in Telegram to set credentials or edit config.json directly and restart service.</p>
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
        name = row[0]
        active_channels.pop(name, None)
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

def run_flask():
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True)

flask_thread = threading.Thread(target=run_flask, daemon=True)
flask_thread.start()

print("Bot + dashboard starting...")
client.run_until_disconnected()
