
import json
import asyncio
from telethon import TelegramClient, events, errors
import re
import logging
from datetime import datetime

def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

def save_config(cfg):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=4, ensure_ascii=False)

config = load_config()

api_id = config["api_id"]
api_hash = config["api_hash"]
session_name = "main_session"
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s — %(levelname)s — %(message)s'
)

client = TelegramClient(session_name, api_id, api_hash).start(bot_token=BOT_TOKEN)

def clean_text(text: str) -> str:
    lines = text.splitlines()
    first_english_line = None
    for line in lines:
        if re.search(r'[A-Za-z0-9]', line) and not re.search(r'[؀-\u06FF]', line):
            first_english_line = line
            break
    text = first_english_line if first_english_line else ""
    text = re.sub(r'\bcode\b', "", text, flags=re.IGNORECASE)
    text = re.sub(r'(https?://\S+|www\.\S+|\S+\.\S+)', "", text)
    text = re.sub(r'@\w+', "", text)
    text = re.sub(r'#\w+', "", text)
    text = re.sub(r'([\u0600-\u06FF])\d+', r'\1', text)
    text = re.sub(r'([\u0600-\u06FF])\s+\d+', r'\1', text)
    cleaned = re.sub(r'[^A-Za-z0-9 ]+', '', text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

async def send_to_target_bot(text):
    bot_username = config["bot_target"]
    if not bot_username:
        return "⚠ الهدف غير محدد"
    if not bot_username.startswith("@"):
        bot_username = f"@{bot_username}"
    try:
        await client.send_message(bot_username, text)
        return "تم الإرسال"
    except Exception as e:
        logging.exception("خطأ أثناء الإرسال")
        return f"خطأ: {e}"

@client.on(events.NewMessage(pattern="/start"))
async def start(event):
    user_id = event.sender_id
    config["owner_id"] = user_id
    save_config(config)
    await event.respond(
        "👋 أهلاً بك!\n"
        "هذا بوت التحكم.\n\n"
        "🔧 استخدم الأوامر التالية:\n"
        "/setchannel — تعيين القناة\n"
        "/setbot — تعيين البوت الهدف\n"
        "/status — عرض الإعدادات الحالية"
    )

@client.on(events.NewMessage(pattern="/setchannel"))
async def set_channel(event):
    if event.sender_id != config["owner_id"]:
        return
    await event.respond("💬 أرسل الآن اسم القناة (بدون @)")
    async with client.conversation(event.chat_id) as conv:
        msg = await conv.get_response()
        config["channel"] = msg.text.strip()
        save_config(config)
        await event.respond("✅ تم حفظ اسم القناة!")

@client.on(events.NewMessage(pattern="/setbot"))
async def set_bot(event):
    if event.sender_id != config["owner_id"]:
        return
    await event.respond("🤖 أرسل اسم البوت الهدف")
    async with client.conversation(event.chat_id) as conv:
        msg = await conv.get_response()
        config["bot_target"] = msg.text.strip()
        save_config(config)
        await event.respond("✅ تم حفظ اسم البوت!")

@client.on(events.NewMessage(pattern="/status"))
async def status(event):
    if event.sender_id != config["owner_id"]:
        return
    await event.respond(
        f"📌 **الإعدادات الحالية:**\n"
        f"القناة: `{config['channel']}`\n"
        f"البوت الهدف: `{config['bot_target']}`"
    )

@client.on(events.NewMessage())
async def watcher(event):
    if not config["channel"]:
        return
    if event.chat.username != config["channel"]:
        return
    text = event.raw_text.strip()
    if not text:
        return
    cleaned = clean_text(text)
    if not cleaned:
        return
    await send_to_target_bot(cleaned)

print("🚀 البوت يعمل الآن...")
client.run_until_disconnected()
