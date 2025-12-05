#!/usr/bin/env python3
import os
import json
import logging
import re
import psycopg2
import asyncio
from psycopg2.extras import DictCursor
from telegram import Bot, Update, ReplyKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from pyrogram import Client
from typing import Optional

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------------------
# Load config.json
# ---------------------------
if not os.path.exists("config.json"):
    raise Exception("❌ ملف config.json غير موجود!")

with open("config.json", "r", encoding="utf-8") as f:
    cfg = json.load(f)

# config.json expected keys:
# - admin_telegram_id
# - bot_token_env_name
# - db_url_env_name
# optional:
# - target_bot_token_env_name
# - target_chat_id_env_name

ADMIN_ID = int(cfg.get("admin_telegram_id", 0))
BOT_TOKEN_ENV = cfg.get("bot_token_env_name", "BOT_TOKEN")
DB_URL_ENV = cfg.get("db_url_env_name", "DB_URL")

# Flexible target bot/chat environment names:
TARGET_BOT_TOKEN_ENV = cfg.get("target_bot_token_env_name", "TARGET_BOT_TOKEN")
TARGET_CHAT_ID_ENV = cfg.get("target_chat_id_env_name", "TARGET_CHAT_ID")

# Read environment variables
BOT_TOKEN = os.getenv(BOT_TOKEN_ENV)
DB_URL = os.getenv(DB_URL_ENV)
TARGET_BOT_TOKEN = os.getenv(TARGET_BOT_TOKEN_ENV)  # optional: token of the other bot used to forward messages
TARGET_CHAT_ID = os.getenv(TARGET_CHAT_ID_ENV)      # optional: chat_id (int or string) to send messages to

print("=== ENV VARS ===")
print(f"{BOT_TOKEN_ENV} -> {BOT_TOKEN is not None}")
print(f"{DB_URL_ENV}  -> {DB_URL is not None}")
print(f"{TARGET_BOT_TOKEN_ENV} -> {TARGET_BOT_TOKEN is not None}")
print(f"{TARGET_CHAT_ID_ENV} -> {TARGET_CHAT_ID is not None}")
print("=================")

if not BOT_TOKEN:
    raise Exception(f"❌ {BOT_TOKEN_ENV} غير موجود داخل Environment Variables!")

if not DB_URL:
    raise Exception(f"❌ {DB_URL_ENV} غير موجود داخل Environment Variables!")

# fix protocol if needed
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------
# Database connection
# ---------------------------
try:
    conn = psycopg2.connect(DB_URL, cursor_factory=DictCursor)
    cur = conn.cursor()
    logger.info("✅ اتصال قاعدة البيانات ناجح")
except Exception as e:
    logger.exception("❌ خطأ في الاتصال بقاعدة البيانات")
    raise e

# create settings table if not exists
cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")
conn.commit()

def save_setting(key: str, value: str):
    cur.execute("""
        INSERT INTO settings (key, value)
        VALUES (%s, %s)
        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value;
    """, (key, value))
    conn.commit()

def load_setting(key: str) -> Optional[str]:
    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
    r = cur.fetchone()
    return r[0] if r else None

# ---------------------------
# Text filtering function
# ---------------------------
def filter_text(msg: str) -> str:
    if not msg:
        return ""
    # remove urls
    msg = re.sub(r"http\S+|www\.\S+", "", msg)
    # remove hashtags
    msg = re.sub(r"#\S+", "", msg)
    # remove word "code"
    msg = msg.replace("code", "").replace("Code", "")
    # remove punctuation
    msg = re.sub(r"[^\w\s]", "", msg)
    # remove arabic-Indic digits
    msg = re.sub(r"[٠-٩]", "", msg)
    # if contains latin letters, remove Arabic block
    if re.search(r"[A-Za-z]", msg):
        msg = re.sub(r"[\u0600-\u06FF]+", "", msg)
    # remove numbers unless adjacent to english letters
    result = ""
    for i in range(len(msg)):
        ch = msg[i]
        if ch.isdigit():
            prev_is_eng = i > 0 and msg[i-1].isalpha()
            next_is_eng = i+1 < len(msg) and msg[i+1].isalpha()
            if not (prev_is_eng or next_is_eng):
                continue
        result += ch
    return result.strip()

# ---------------------------
# Pyrogram (listener) loader
# ---------------------------
def get_pyro():
    session = load_setting("session_string")
    api_id = load_setting("api_id")
    api_hash = load_setting("api_hash")
    if not (session and api_id and api_hash):
        return None
    return Client(
        name="listener",
        api_id=int(api_id),
        api_hash=api_hash,
        in_memory=True,
        session_string=session
    )

pyro_client = None
listener_running = False

# ---------------------------
# Telegram bot (Application)
# ---------------------------
application = ApplicationBuilder().token(BOT_TOKEN).build()

# Prepare target bot object if token provided
target_bot = Bot(token=TARGET_BOT_TOKEN) if TARGET_BOT_TOKEN else None

# Reply keyboard used in your original UI
MAIN_KEYBOARD = [
    ["📡 تشغيل المستمع", "⛔ إيقاف المستمع"],
    ["⚙ حفظ API_ID / API_HASH"],
    ["📁 رفع جلسة", "📃 عرض API"]
]

# ---------------------------
# Handlers
# ---------------------------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ غير مصرح لك.")
        return
    await update.message.reply_text(
        "مرحبًا بك في لوحة الإدارة 👑",
        reply_markup=ReplyKeyboardMarkup(MAIN_KEYBOARD, resize_keyboard=True)
    )

# state for saving api id/hash
api_state = {}

async def setapi_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ غير مصرح لك.")
    api_state[update.effective_user.id] = "api_id"
    await update.message.reply_text("أرسل لي API_ID الآن:")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text or ""
    # admin input flow for API_ID / API_HASH
    if uid in api_state and api_state[uid] == "api_id":
        save_setting("api_id", text)
        api_state[uid] = "api_hash"
        await update.message.reply_text("تم حفظ API_ID ✔\nأرسل API_HASH الآن:")
        return
    if uid in api_state and api_state[uid] == "api_hash":
        save_setting("api_hash", text)
        api_state.pop(uid, None)
        await update.message.reply_text("تم حفظ API_HASH ✔")
        return

    # buttons commands
    if text == "📃 عرض API":
        api_id = load_setting("api_id")
        api_hash = load_setting("api_hash")
        return await update.message.reply_text(f"API_ID: {api_id}\nAPI_HASH: {api_hash}")

    if text == "📡 تشغيل المستمع":
        return await start_listener(update, context)

    if text == "⛔ إيقاف المستمع":
        return await stop_listener(update, context)

    if text == "📁 رفع جلسة":
        return await update.message.reply_text("أرسل ملف session الآن.")

    # --- Normal user message handling: filter and forward to target ---
    filtered = filter_text(text)
    if not filtered:
        return await update.message.reply_text("⚠️ بعد الفلترة الرسالة أصبحت فارغة؛ لم يُرسل شيء.")

    # Determine destination: prefer explicit TARGET_CHAT_ID, else echo back to admin if admin sent
    dest_chat = None
    if TARGET_CHAT_ID:
        # allow numeric or string chat id
        try:
            dest_chat = int(TARGET_CHAT_ID)
        except Exception:
            dest_chat = TARGET_CHAT_ID

    # If target_bot token is available, use it; otherwise use current bot to forward
    try:
        sent_msg_info = None
        if target_bot and dest_chat:
            # send with target bot
            sent = await target_bot.send_message(chat_id=dest_chat, text=filtered)
            sent_msg_info = f"✔ أرسلت الرسالة إلى البوت الهدف (chat_id={dest_chat})."
        elif dest_chat:
            # send using same bot token but to given chat id
            await context.bot.send_message(chat_id=dest_chat, text=filtered)
            sent_msg_info = f"✔ أرسلت الرسالة إلى chat_id={dest_chat} بواسطة نفس التوكن."
        else:
            # fallback: send to admin only
            await update.message.reply_text(f"⚠️ لا يوجد مقصد محدد. الرسالة بعد الفلترة:\n\n{filtered}")
            return
        await update.message.reply_text(sent_msg_info)
    except Exception as e:
        logger.exception("خطأ عند إرسال الرسالة للمستلم")
        await update.message.reply_text(f"❌ فشل الإرسال: {e}")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # save uploaded session file (document)
    if not update.message.document:
        return
    file = await update.message.document.get_file()
    data = await file.download_as_bytearray()
    try:
        save_setting("session_string", data.decode())
        await update.message.reply_text("✔ تم حفظ جلسة Pyrogram بنجاح")
    except Exception as e:
        await update.message.reply_text(f"❌ خطأ في حفظ الجلسة: {e}")

# ---------------------------
# Pyrogram listener controls
# ---------------------------
async def start_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pyro_client, listener_running
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ غير مصرح لك.")
    if listener_running:
        return await update.message.reply_text("المستمع يعمل بالفعل ✔")
    pyro_client = get_pyro()
    if not pyro_client:
        return await update.message.reply_text("❌ يجب رفع الجلسة + API_ID + API_HASH أولاً")
    await pyro_client.start()
    listener_running = True
    await update.message.reply_text("✔ تم تشغيل مستمع Pyrogram")

async def stop_listener(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global pyro_client, listener_running
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ غير مصرح لك.")
    if not listener_running:
        return await update.message.reply_text("المستمع متوقف بالفعل")
    await pyro_client.stop()
    listener_running = False
    await update.message.reply_text("⛔ تم إيقاف المستمع")

# ---------------------------
# Register handlers
# ---------------------------
application.add_handler(CommandHandler("start", start_handler))
application.add_handler(CommandHandler("setapi", setapi_handler))
application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))
application.add_handler(MessageHandler(filters.Document.ALL, handle_document))

# ---------------------------
# Main: run polling
# ---------------------------
def main():
    print("🚀 بدء تشغيل البوت (polling)...")
    # Drop pending updates so bot starts cleanly (optional)
    application.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
