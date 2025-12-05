import os
import base64
import logging
from flask import Flask, request
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

logging.basicConfig(level=logging.INFO)

# =======================
#   قاعدة بيانات بسيطة
# =======================
SESSIONS_DIR = "sessions"
CHANNELS_FILE = "channels.txt"

if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

if not os.path.exists(CHANNELS_FILE):
    open(CHANNELS_FILE, "w").close()

# =======================
#       لوحة التحكم
# =======================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع جلسة", callback_data="upload_session")],
        [InlineKeyboardButton("➕ إضافة قناة/بوت هدف", callback_data="add_channel")],
        [InlineKeyboardButton("📜 عرض القنوات", callback_data="list_channels")],
        [InlineKeyboardButton("📁 عرض الجلسات", callback_data="list_sessions")],
    ])


# =======================
#      أوامر البوت
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً! اختر من القائمة:", reply_markup=main_menu())


async def handle_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "upload_session":
        context.user_data["mode"] = "upload_session"
        await query.edit_message_text("🟦 أرسل الآن ملف **session** بأي صيغة.")
    
    elif query.data == "add_channel":
        context.user_data["mode"] = "add_channel"
        await query.edit_message_text("أرسل اسم القناة أو معرف البوت الهدف (مثال: @mychannel).")

    elif query.data == "list_channels":
        with open(CHANNELS_FILE, "r") as f:
            data = f.read().strip()

        if not data:
            msg = "لا توجد قنوات مضافة."
        else:
            msg = "📜 القنوات:\n" + "\n".join([f"- {x}" for x in data.splitlines()])

        await query.edit_message_text(msg, reply_markup=main_menu())

    elif query.data == "list_sessions":
        files = os.listdir(SESSIONS_DIR)
        if not files:
            msg = "لا توجد جلسات."
        else:
            msg = "📁 الجلسات:\n" + "\n".join([f"- {x}" for x in files])
        await query.edit_message_text(msg, reply_markup=main_menu())


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")

    # إضافة قناة
    if mode == "add_channel":
        channel = update.message.text.strip()
        if not channel.startswith("@"):
            await update.message.reply_text("❌ يجب أن يبدأ بـ @")
            return
        
        with open(CHANNELS_FILE, "a") as f:
            f.write(channel + "\n")

        await update.message.reply_text("✔️ تم حفظ القناة!", reply_markup=main_menu())
        context.user_data["mode"] = None


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")
    
    if mode != "upload_session":
        return
    
    file = await update.message.document.get_file()
    raw = await file.download_as_bytearray()

    filename = update.message.document.file_name

    # نخزن الملف base64 بدون محاولة قراءة UTF-8
    encoded = base64.b64encode(raw).decode()

    save_path = os.path.join(SESSIONS_DIR, filename + ".b64")
    with open(save_path, "w") as f:
        f.write(encoded)

    await update.message.reply_text("✔️ تم حفظ الجلسة بنجاح!", reply_markup=main_menu())
    context.user_data["mode"] = None


# =======================
#        Flask Webhook
# =======================
app = Flask(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

application = Application.builder().token(BOT_TOKEN).build()

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(handle_buttons))
application.add_handler(MessageHandler(filters.Document.ALL, handle_file))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))


@app.route(f"/{BOT_TOKEN}", methods=["POST"])
def telegram_webhook():
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.create_task(application.process_update(update))
    return "OK", 200


if __name__ == "__main__":
    application.run_webhook(
        listen="0.0.0.0",
        port=10000,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}"
    )
