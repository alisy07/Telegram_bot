import os, json, sqlite3, logging, re, threading
from datetime import datetime
from telethon import TelegramClient, events, Button
from flask import Flask, render_template_string, request, redirect, url_for

logging.basicConfig(level=logging.INFO, format='%(asctime)s — %(levelname)s — %(message)s')

CONFIG_FILE = "config.json"
DB_PATH = "bot.db"
WEB_HOST = "0.0.0.0"
WEB_PORT = int(os.environ.get("PORT", 10000))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    logging.warning("⚠ BOT_TOKEN غير موجود في Environment Variables")

# ====== load/create config.json ======
if not os.path.exists(CONFIG_FILE):
    config = {"api_id": 0, "api_hash": "", "owner_id": 0, "session_name": "session"}
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)
else:
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)

# ====== SQLite DB ======
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

# ====== Telegram client ======
def create_client(api_id=None, api_hash=None):
    try:
        if api_id and api_hash:
            logging.info("🟢 تشغيل البوت ببيانات API الحقيقية")
            return TelegramClient(config["session_name"], api_id, api_hash).start(bot_token=BOT_TOKEN)
        else:
            logging.info("🔹 تشغيل بوت مؤقت بدون api_id/api_hash")
            return None  # لا ننشئ client مؤقت لأنه يسبب خطأ على Render
    except Exception as e:
        logging.exception("خطأ عند إنشاء العميل")
        return None

client = create_client(config["api_id"], config["api_hash"])

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

# ====== تنظيف الرسائل ======
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

# ====== active channels ======
active_channels = {}
cursor.execute("SELECT channel_name, bot_target FROM channels WHERE active=1")
for ch, bt in cursor.fetchall():
    active_channels[ch] = bt

# ====== Telethon handlers ======
if client:

    @client.on(events.NewMessage(pattern="/setapi"))
    async def handle_setapi(event):
        global client
        user_id = event.sender_id
        config["owner_id"] = user_id
        await event.respond("💬 أدخل api_id (رقم فقط):")
        api_id_msg = await client.wait_for(events.NewMessage(from_users=user_id))
        config["api_id"] = int(api_id_msg.text.strip())

        await event.respond("💬 أدخل api_hash:")
        api_hash_msg = await client.wait_for(events.NewMessage(from_users=user_id))
        config["api_hash"] = api_hash_msg.text.strip()

        save_config()
        await event.respond("✅ تم حفظ api_id و api_hash. سيتم إعادة تشغيل البوت الآن.")

        if client.is_connected():
            await client.disconnect()
        client = create_client(config["api_id"], config["api_hash"])
        print("🔹 Telegram client recreated with real API credentials.")

    @client.on(events.NewMessage(pattern="/start"))
    async def handle_start(event):
        user_id = event.sender_id
        config["owner_id"] = user_id
        save_config()
        buttons = [[Button.inline("New", b"new")]]
        cursor.execute("SELECT channel_name FROM channels ORDER BY id DESC")
        for (ch,) in cursor.fetchall():
            buttons.append([Button.inline(ch, ch.encode())])
        await event.respond("اختر قناة أو أنشئ واحدة:", buttons=buttons)

    @client.on(events.CallbackQuery(data=b"new"))
    async def handle_new_cb(event):
        await event.respond("💬 أدخل اسم القناة:")
        api_msg = await client.wait_for(events.NewMessage(from_users=event.sender_id))
        channel_name = api_msg.text.strip()
        await event.respond("🤖 أدخل اسم البوت الهدف:")
        bot_msg = await client.wait_for(events.NewMessage(from_users=event.sender_id))
        bot_target = bot_msg.text.strip()
        try:
            cursor.execute("INSERT INTO channels(channel_name, bot_target, active, created_at) VALUES (?, ?, 0, ?)",
                           (channel_name, bot_target, datetime.utcnow().isoformat()))
            conn.commit()
            await event.respond(f"✅ تم حفظ {channel_name} -> {bot_target}")
        except sqlite3.IntegrityError:
            await event.respond("القناة موجودة بالفعل")

    @client.on(events.CallbackQuery)
    async def handle_channel_cb(event):
        name = event.data.decode()
        cursor.execute("SELECT bot_target, active FROM channels WHERE channel_name=?", (name,))
        row = cursor.fetchone()
        if not row:
            return
        bot_target, active = row
        if name in active_channels:
            active_channels.pop(name, None)
            cursor.execute("UPDATE channels SET active=0 WHERE channel_name=?", (name,))
            conn.commit()
            await event.answer(f"أوقف مراقبة {name}")
        else:
            active_channels[name] = bot_target
            cursor.execute("UPDATE channels SET active=1 WHERE channel_name=?", (name,))
            conn.commit()
            await event.answer(f"بدأ مراقبة {name}")

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

# ====== Flask web UI ======
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
<p>ادخل api_id و api_hash عبر /setapi في Telegram بعد رفع BOT_TOKEN في Environment Variables.</p>
"""

@app.route("/")
def index():
    cursor.execute("SELECT id, channel_name, bot_target, active FROM channels ORDER BY id DESC")
    rows = cursor.fetchall()
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

def run_flask():
    app.run(host=WEB_HOST, port=WEB_PORT, threaded=True)

threading.Thread(target=run_flask, daemon=True).start()

print("🚀 Bot + Dashboard ready!")
if client:
    client.run_until_disconnected()
else:
    print("⚠ Telegram client not started — أدخل api_id و api_hash عبر /setapi بعد رفع BOT_TOKEN")
