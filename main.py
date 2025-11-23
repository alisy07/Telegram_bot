import json, sqlite3, asyncio, logging, re, os
from telethon import TelegramClient, events, Button

logging.basicConfig(level=logging.INFO, format='%(asctime)s — %(levelname)s — %(message)s')

CONFIG_FILE = "config.json"
DB_PATH = "bot.db"

# ======= تحميل أو إنشاء config.json =======
if not os.path.exists(CONFIG_FILE):
    config = {"api_id":0,"api_hash":"","bot_token":"","session_name":"session","owner_id":0}
    with open(CONFIG_FILE,"w",encoding="utf-8") as f:
        json.dump(config,f,indent=4)
else:
    with open(CONFIG_FILE,"r",encoding="utf-8") as f:
        config = json.load(f)

# ======= إنشاء TelegramClient (قبل أي @client.on) =======
def create_client():
    if config["api_id"] and config["api_hash"] and config["bot_token"]:
        logging.info("🟢 تشغيل البوت ببيانات API الحقيقية")
        return TelegramClient(config["session_name"], config["api_id"], config["api_hash"]).start(bot_token=config["bot_token"])
    else:
        logging.warning("🔴 لا توجد بيانات API — يجب إدخالها عبر /setapi")
        return TelegramClient("temp_session", 11111, "temp_hash").start(bot_token=config["bot_token"])

client = create_client()   # <<< مهم جداً يكون هنا

# ======= قاعدة البيانات =======
conn = sqlite3.connect(DB_PATH)
cursor = conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS channels(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name TEXT NOT NULL,
    bot_target TEXT NOT NULL)
""")
conn.commit()

# ======= دالة حفظ config =======
def save_config():
    with open(CONFIG_FILE,"w",encoding="utf-8") as f:
        json.dump(config,f,indent=4)

# ======= تنظيف الرسائل =======
def clean_text(text:str)->str:
    lines = text.splitlines()
    first_english_line = None

    for line in lines:
        if re.search(r'[A-Za-z0-9]',line) and not re.search(r'[\u0600-\u06FF]',line):
            first_english_line = line
            break

    text = first_english_line if first_english_line else ""
    text = re.sub(r'\bcode\b',"",text,flags=re.IGNORECASE)
    text = re.sub(r'(https?://\S+|www\.\S+|\S+\.\S+)',"",text)
    text = re.sub(r'@\w+',"",text)
    text = re.sub(r'#\w+',"",text)
    text = re.sub(r'([\u0600-\u06FF])\d+',r'\1',text)
    text = re.sub(r'([\u0600-\u06FF])\s+\d+',r'\1',text)
    cleaned = re.sub(r'[^A-Za-z0-9 ]+','',text)
    cleaned = re.sub(r'\s+',' ',cleaned).strip()
    return cleaned

# ======= إرسال الرسالة للبوت الهدف =======
async def send_to_target(text, bot_target):
    if not bot_target:
        return "⚠ الهدف غير محدد"
    if not bot_target.startswith("@"):
        bot_target = f"@{bot_target}"
    try:
        await client.send_message(bot_target, text)
        return "تم الإرسال"
    except Exception as e:
        logging.exception("خطأ أثناء الإرسال")
        return f"خطأ: {e}"

# ======= /setapi — إدخال api_id و api_hash =======
@client.on(events.NewMessage(pattern="/setapi"))
async def set_api(event):
    user_id = event.sender_id
    config["owner_id"] = user_id

    await event.respond("💬 أدخل api_id:")
    async with client.conversation(user_id) as conv:
        msg1 = await conv.get_response()
        config["api_id"] = int(msg1.text.strip())

        await event.respond("💬 أدخل api_hash:")
        msg2 = await conv.get_response()
        config["api_hash"] = msg2.text.strip()

        await event.respond("💬 أدخل bot_token:")
        msg3 = await conv.get_response()
        config["bot_token"] = msg3.text.strip()

        save_config()
        await event.respond("✅ تم حفظ الإعدادات بنجاح.\n♻ أعد تشغيل البوت على Render الآن.")

# ======= /start =======
@client.on(events.NewMessage(pattern="/start"))
async def start(event):
    buttons = [[Button.inline("New", b"new")]]
    cursor.execute("SELECT channel_name FROM channels")
    for (ch,) in cursor.fetchall():
        buttons.append([Button.inline(ch, ch.encode())])

    await event.respond("🔽 اختر قناة أو أنشئ واحدة:", buttons=buttons)

# ======= إضافة قناة جديدة =======
@client.on(events.CallbackQuery(data=b"new"))
async def new_channel(event):
    await event.respond("💬 أدخل اسم القناة:")
    async with client.conversation(event.sender_id) as conv:
        ch = await conv.get_response()
        channel_name = ch.text.strip()

        await event.respond("🤖 أدخل اسم البوت الهدف:")
        bot = await conv.get_response()
        bot_target = bot.text.strip()

        cursor.execute(
            "INSERT INTO channels(channel_name, bot_target) VALUES(?,?)",
            (channel_name, bot_target)
        )
        conn.commit()

        await event.respond(f"✅ تم حفظ القناة {channel_name} مع البوت {bot_target}")

# ======= مراقبة القنوات =======
active_channels = {}

@client.on(events.CallbackQuery)
async def start_watch(event):
    channel_name = event.data.decode()

    cursor.execute("SELECT bot_target FROM channels WHERE channel_name=?", (channel_name,))
    row = cursor.fetchone()

    if not row:
        return

    active_channels[channel_name] = row[0]
    await event.answer(f"🚀 بدأ مراقبة {channel_name}")

# ======= استقبال الرسائل من القنوات =======
@client.on(events.NewMessage())
async def watcher(event):
    if not event.chat or not event.chat.username:
        return

    source = event.chat.username

    if source not in active_channels:
        return

    cleaned = clean_text(event.raw_text)
    if not cleaned:
        return

    await send_to_target(cleaned, active_channels[source])

print("🚀 البوت يعمل...")
client.run_until_disconnected()
