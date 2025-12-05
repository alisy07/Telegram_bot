import os
import json
import asyncio
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

logging.basicConfig(level=logging.INFO)

# -------------------------------
# تحميل config.json
# -------------------------------
if not os.path.exists("config.json"):
    raise Exception("❌ ملف config.json غير موجود!")

with open("config.json", "r") as f:
    cfg = json.load(f)

ADMIN_ID = int(cfg.get("admin_telegram_id", 0))
BOT_TOKEN = os.environ.get(cfg["bot_token_env_name"])
DB_URL = os.environ.get(cfg["db_url_env_name"])

DB_URL = os.getenv("DB_URL")

print("\n==============================")
print("DB_URL READ FROM ENV:", DB_URL)
print("==============================\n")

if not DB_URL:
    raise Exception("❌ DB_URL is EMPTY or NOT FOUND in Render environment.")

# تصحيح البروتوكول
if DB_URL.startswith("postgres://"):
    DB_URL = DB_URL.replace("postgres://", "postgresql://", 1)

conn = psycopg2.connect(DB_URL)

# -------------------------------
# اتصال قاعدة البيانات
# -------------------------------
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
import re

def filter_text(msg):
    if not msg:
        return ""

    # حذف الروابط
    msg = re.sub(r"http\S+|www\.\S+", "", msg)

    # حذف الهاشتاغ
    msg = re.sub(r"#\S+", "", msg)

    # حذف كلمة code
    msg = msg.replace("code", "").replace("Code", "")

    # حذف الرموز
    msg = re.sub(r"[^\w\s]", "", msg)

    # حذف الأرقام العربية فقط
    msg = re.sub(r"[٠-٩]", "", msg)

    # حذف العربية بالكامل إذا جاءت مع كلمات أجنبية
    if re.search(r"[A-Za-z]", msg):
        msg = re.sub(r"[\u0600-\u06FF]+", "", msg)

    # حذف الأرقام إلا إذا كانت مرتبطة بحرف إنجليزي
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


# -------------------------------
# Pyrogram Client Loader
# -------------------------------
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

# -------------------------------
# أوامر البوت
# -------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ غير مصرح لك.")
        return

    keyboard = [
        ["📡 تشغيل المستمع", "⛔ إيقاف المستمع"],
        ["⚙ حفظ API_ID / API_HASH"],
        ["📁 رفع جلسة", "📃 عرض API"]
    ]
    await update.message.reply_text(
        "مرحبًا بك في لوحة الإدارة 👑",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )


# -------------------------------
# حفظ API ID & Hash
# -------------------------------
api_state = {}  # {user_id: step}

async def setapi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("❌ غير مصرح لك.")

    api_state[update.effective_user.id] = "api_id"
    await update.message.reply_text("أرسل لي API_ID الآن:")


async def handle_text(update: Update, context):
    uid = update.effective_user.id
    msg = update.message.text

    # حفظ API ID
    if uid in api_state and api_state[uid] == "api_id":
        save_setting("api_id", msg)
        api_state[uid] = "api_hash"
        return await update.message.reply_text("تم حفظ API_ID ✔\nأرسل API_HASH الآن:")

    # حفظ API HASH
    if uid in api_state and api_state[uid] == "api_hash":
        save_setting("api_hash", msg)
        api_state.pop(uid)
        return await update.message.reply_text("تم حفظ API_HASH ✔")

    # الأزرار
    if msg == "📃 عرض API":
        api_id = load_setting("api_id")
        api_hash = load_setting("api_hash")
        return await update.message.reply_text(f"API_ID: {api_id}\nAPI_HASH: {api_hash}")

    if msg == "📡 تشغيل المستمع":
        return await start_listener(update, context)

    if msg == "⛔ إيقاف المستمع":
        return await stop_listener(update, context)

    if msg == "📁 رفع جلسة":
        return await update.message.reply_text("أرسل ملف session الآن.")



# -------------------------------
# رفع الجلسة
# -------------------------------
async def handle_file(update: Update, context):
    file = await update.message.document.get_file()
    data = await file.download_as_bytearray()

    save_setting("session_string", data.decode())

    await update.message.reply_text("✔ تم حفظ جلسة Pyrogram بنجاح")


# -------------------------------
# تشغيل المستمع
# -------------------------------
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


# -------------------------------
# إيقاف المستمع
# -------------------------------
async def stop_listener(update, context):
    global pyro_client, listener_running

    if not listener_running:
        return await update.message.reply_text("المستمع متوقف بالفعل")

    await pyro_client.stop()
    listener_running = False

    await update.message.reply_text("⛔ تم إيقاف المستمع")


# -------------------------------
# تشغيل البوت
# -------------------------------
app = ApplicationBuilder().token(BOT_TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("setapi", setapi))
app.add_handler(CommandHandler("getapi", lambda u, c: u.message.reply_text(
    f"API_ID: {load_setting('api_id')}\nAPI_HASH: {load_setting('api_hash')}"
)))

app.add_handler(MessageHandler(filters.TEXT, handle_text))
app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

print("🚀 البوت يعمل الآن...")
app.run_polling()
