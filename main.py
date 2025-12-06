import os
import base64
import logging
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)

logging.basicConfig(level=logging.INFO)

# ========= ملفات =========
SESSIONS_DIR = "sessions"
CHANNELS_FILE = "channels.txt"

os.makedirs(SESSIONS_DIR, exist_ok=True)
if not os.path.exists(CHANNELS_FILE):
    open(CHANNELS_FILE, "w").close()


# ========= لوحة التحكم =========
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📤 رفع جلسة", callback_data="upload_session")],
        [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
        [InlineKeyboardButton("🗑️ حذف قناة", callback_data="delete_channel")],
        [InlineKeyboardButton("📜 عرض القنوات", callback_data="list_channels")],
        [InlineKeyboardButton("📁 عرض الجلسات", callback_data="list_sessions")],
    ])


# ========= أوامر =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً! اختر من القائمة:", reply_markup=main_menu())


async def pressed_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    # ---------------------------------------
    # تأكيد الحذف YES
    # ---------------------------------------
    if q.data.startswith("confirm_del:"):
        ch = q.data.replace("confirm_del:", "").strip()

        channels = open(CHANNELS_FILE).read().split("\n")
        channels = [c for c in channels if c.strip() and c != ch]

        with open(CHANNELS_FILE, "w") as f:
            f.write("\n".join(channels))

        await q.edit_message_text(f"✔️ تم حذف القناة:\n{ch}", reply_markup=main_menu())
        return

    # ---------------------------------------
    # إلغاء الحذف NO
    # ---------------------------------------
    if q.data == "cancel_del":
        await q.edit_message_text("❌ تم إلغاء الحذف.", reply_markup=main_menu())
        return

    # ---------------------------------------
    # اختيار قناة للحذف — نعرض زرين تأكيد
    # ---------------------------------------
    if q.data.startswith("del:"):
        ch = q.data.replace("del:", "").strip()

        confirm_keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✔️ نعم، احذف", callback_data=f"confirm_del:{ch}")],
            [InlineKeyboardButton("❌ لا، إلغاء", callback_data="cancel_del")]
        ])

        await q.edit_message_text(
            f"هل أنت متأكد من حذف القناة:\n{ch} ؟",
            reply_markup=confirm_keyboard
        )
        return

    # ---------------------------------------
    # رفع جلسة
    # ---------------------------------------
    if q.data == "upload_session":
        context.user_data["mode"] = "upload_session"
        await q.edit_message_text("🟦 أرسل الآن ملف الجلسة.")
    
    # ---------------------------------------
    # إضافة قناة
    # ---------------------------------------
    elif q.data == "add_channel":
        context.user_data["mode"] = "add_channel"
        await q.edit_message_text("أرسل معرف القناة مثل @example")

    # ---------------------------------------
    # حذف قناة
    # ---------------------------------------
    elif q.data == "delete_channel":
        context.user_data["mode"] = "delete_channel"

        lines = open(CHANNELS_FILE).read().strip().split("\n")
        channels = [l for l in lines if l.strip()]

        if not channels:
            await q.edit_message_text("❌ لا توجد قنوات لحذفها", reply_markup=main_menu())
            return

        buttons = [
            [InlineKeyboardButton(ch, callback_data=f"del:{ch}")]
            for ch in channels
        ]

        await q.edit_message_text(
            "اختر قناة لحذفها:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    # ---------------------------------------
    # عرض القنوات
    # ---------------------------------------
    elif q.data == "list_channels":
        text = open(CHANNELS_FILE).read().strip()
        msg = "لا توجد قنوات." if not text else "📜 القنوات:\n" + text.replace("\n", "\n- ")
        await q.edit_message_text(msg, reply_markup=main_menu())

    # ---------------------------------------
    # عرض الجلسات
    # ---------------------------------------
    elif q.data == "list_sessions":
        files = os.listdir(SESSIONS_DIR)
        msg = "لا توجد جلسات." if not files else "📁 الجلسات:\n- " + "\n- ".join(files)
        await q.edit_message_text(msg, reply_markup=main_menu())


# ========= رسائل نصية =========
async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    # ---------------------------------------
    # فلترة كلمة code من أي رسالة
    # ---------------------------------------
    if "code" in text.lower():
        cleaned = text.lower().replace("code", "").strip()
        cleaned = cleaned if cleaned else "❌ تم حذف كلمة code."
        await update.message.reply_text(cleaned)
        return

    # ---------------------------------------
    # إضافة قناة
    # ---------------------------------------
    if context.user_data.get("mode") == "add_channel":

        if not text.startswith("@"):
            await update.message.reply_text("❌ يجب أن يبدأ بـ @")
            return

        with open(CHANNELS_FILE, "a") as f:
            f.write(text + "\n")

        await update.message.reply_text("✔️ تمت إضافة القناة!", reply_markup=main_menu())
        context.user_data["mode"] = None


# ========= رفع الملفات =========
async def file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("mode") != "upload_session":
        return

    file = await update.message.document.get_file()
    raw = await file.download_as_bytearray()
    encoded = base64.b64encode(raw).decode()

    filename = update.message.document.file_name + ".b64"
    with open(os.path.join(SESSIONS_DIR, filename), "w") as f:
        f.write(encoded)

    await update.message.reply_text("✔️ تم حفظ الجلسة!", reply_markup=main_menu())
    context.user_data["mode"] = None


# ========= تشغيل Webhook على Render =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # مثال: https://mybot.onrender.com

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(pressed_button))
    app.add_handler(MessageHandler(filters.Document.ALL, file_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    # Webhook
    app.run_webhook(
        listen="0.0.0.0",
        port=10000,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
