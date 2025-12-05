import os
import json
import asyncio
import threading
from flask import Flask

from telegram import (
    Bot, Update, ReplyKeyboardMarkup,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)
from pyrogram import Client
import psycopg2
import logging
import re

logging.basicConfig(level=logging.INFO)

# ---------------------------------------------------
# Flask Server (fix for Render free web service)
# ---------------------------------------------------
app_flask = Flask(__name__)

@app_flask.route("/")
def home():
    return "Bot is running!"

def run_flask():
    app_flask.run(host="0.0.0.0", port=10000)

# ---------------------------------------------------
# تحميل config.json
# ---------------------------------------------------
if not os.path.exists("config.json"):
    raise Exception("❌ ملف config.json غير موجود!")

with open("config.json", "r") as f:
    cfg = json.load(f)

ADMIN_ID = int(cfg.get("admin_telegram_id", 0))
BOT_TOKEN = os.environ.get(cfg["bot_token_env_name"])
DB_URL = os.environ.get(cfg["db_url_env_name"])

# ---------------------------------------------------
# إظهار DB_URL (اختياري للتأكد)
# ---------------------------------------------------
print("\n==============================")
print("DB_URL READ:", DB_URL)
print("==============================\n")

if not DB_URL:
    raise Exception("❌ DB_URL غير موجود في Environment!")

# ---------------------------------------------------
# تصحيح البروتوكول إذا لزم الأمر
# ---------------------------------------------------
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

# ---------------------------------------------------
# اتصال قاعدة البيانات
# ---------------------------------------------------
conn = psycopg2.connect(DB_URL)
cur = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
)
""")
conn.commit()


def save_setting(key, value):
    cur.execute("""
        INSERT INTO settings (key, value)
        VALUES (%s, %s)
        ON CONFLICT (key)
        DO UPDATE SET value = EXCLUDED.value;
    """, (key, value))
    conn.commit()


def load_setting(key):
    cur.execute("SELECT value FROM settings WHERE key = %s", (key,))
    r = cur.fetchone()
    return r[0] if r else None


# -------------------------------------
# فلترة النصوص قبل إرسالها
# -------------------------------------
def filter_text(msg):
    if not msg:
        return ""
    msg = re.sub(r"http\S+|www\.\S+", "", msg)
    msg = re.sub(r"#\S+", "", msg)
    msg = msg.replace("code", "").replace("Code", "")
    msg = re.sub(r"[^\w\s]", "", msg)
    msg = re.sub(r"[٠-٩]", "", msg)

    if re.search(r"[A-Za-z]", msg):
        msg = re.sub(r"[\u0600-\u06FF]+", "", msg)

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


# ---------------------------------------------------------------
# Pyrogram Client Loader
# ---------------------------------------------------------------
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

# ---------------------------------------------------------------
# أوامر البوت
# ---------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ غير مصرح لك.")

    keyboard = [
        ["📡 تشغيل المستمع", "⛔ إيقاف المستمع"],
        ["⚙ حفظ API_ID / API_HASH"],
        ["📁 رفع جلسة", "📃 عرض API"]
    ]
    await update.message.reply_text(
        "مرحبًا بك في لوحة الإدارة 👑",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


api_state = {}

async def setapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ غير مصرح لك.")
    api_state[update.effective_user.id] = "api_id"
    await update.message.reply_text("أرسل لي API_ID الآن:")


async def handle_text(update: Update, context):
    uid = update.effective_user.id
    msg = update.message.text

    # حفظ API_ID
    if uid in api_state and api_state[uid] == "api_id":
        save_setting("api_id", msg)
        api_state[uid] = "api_hash"
        return await update.message.reply_text("تم حفظ API_ID ✔\nأرسل API_HASH الآن:")

    # حفظ API_HASH
    if uid in api_state and api_state[uid] == "api_hash":
        save_setting("api_hash", msg)
        api_state.pop(uid)
        return await update.message.reply_text("تم حفظ API_HASH ✔")

    # عرض API
    if msg == "📃 عرض API":
        return await update.message.reply_text(
            f"API_ID: {load_setting('api_id')}\nAPI_HASH: {load_setting('api_hash')}"
        )

    if msg == "📡 تشغيل المستمع":
        return await start_listener(update, context)

    if msg == "⛔ إيقاف المستمع":
        return await stop_listener(update, context)

    if msg == "📁 رفع جلسة":
        return await update.message.reply_text("أرسل ملف session الآن.")


async def handle_file(update: Update, context):
    file = await update.message.document.get_file()
    data = await file.download_as_bytearray()
    save_setting("session_string", data.decode())
    await update.message.reply_text("✔ تم حفظ جلسة Pyrogram بنجاح")


async def start_listener(update, context):
    global pyro_client, listener_running
    if listener_running:
        return await update.message.reply_text("المستمع يعمل بالفعل ✔")

    pyro_client = get_pyro()
    if not pyro_client:
        return await update.message.reply_text("❌ يجب رفع الجلسة + API_ID + API_HASH أولاً")

    await pyro_client.start()
    listener_running = True
    await update.message.reply_text("✔ تم تشغيل مستمع Telegram")


async def stop_listener(update, context):
    global pyro_client, listener_running
    if not listener_running:
        return await update.message.reply_text("المستمع متوقف بالفعل")

    await pyro_client.stop()
    listener_running = False
    await update.message.reply_text("⛔ تم إيقاف المستمع")


# ---------------------------------------------------------------
# تشغيل TELEGRAM BOT
# ---------------------------------------------------------------
application = ApplicationBuilder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("setapi", setapi))
application.add_handler(MessageHandler(filters.TEXT, handle_text))
application.add_handler(MessageHandler(filters.Document.ALL, handle_file))

def run_bot():
    print("🚀 البوت يعمل الآن...")
    application.run_polling()

# ---------------------------------------------------------------
# Multi-thread (Flask + Bot)
# ---------------------------------------------------------------
if __name__ == "__main__":
    # تشغيل Flask في Thread
    t1 = threading.Thread(target=run_flask)
    t1.start()

    # تشغيل البوت في Thread
    t2 = threading.Thread(target=run_bot)
    t2.start()
