import os
import sqlite3
import base64
import logging
import asyncio
import re
from typing import Optional, List, Tuple, Set

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

# ============ إعدادات عامة ============
DB_FILE = "bot_data.db"
SESSIONS_DIR = "sessions"
os.makedirs(SESSIONS_DIR, exist_ok=True)

# ضع هنا الـ ADMIN_ID الذي زودتني به
ADMIN_ID = 1037850299

# ============ قاعدة البيانات ============
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS apis (
            user_id INTEGER PRIMARY KEY,
            api_id TEXT,
            api_hash TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            channel_username TEXT,
            target_bot_username TEXT
        );
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            filename TEXT,
            data_b64 TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def save_api(user_id: int, api_id: str, api_hash: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO apis(user_id, api_id, api_hash) VALUES(?,?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET api_id=excluded.api_id, api_hash=excluded.api_hash;",
        (user_id, api_id, api_hash),
    )
    conn.commit()
    conn.close()


def get_api(user_id: int) -> Optional[Tuple[str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT api_id, api_hash FROM apis WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return (row[0], row[1]) if row else None


def add_channel_db(user_id: int, channel: str, target_bot: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO channels(user_id, channel_username, target_bot_username) VALUES(?,?,?)",
        (user_id, channel, target_bot),
    )
    conn.commit()
    conn.close()


def list_channels_db(user_id: int) -> List[Tuple[int, str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, channel_username, target_bot_username FROM channels WHERE user_id = ?",
        (user_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def list_all_channels_db() -> List[Tuple[int, int, str, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, user_id, channel_username, target_bot_username FROM channels"
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def delete_channel_db(channel_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()


def save_session_db(user_id: int, filename: str, data_b64: str):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO sessions(user_id, filename, data_b64) VALUES(?,?,?)",
        (user_id, filename, data_b64),
    )
    conn.commit()
    conn.close()


def list_sessions_db(user_id: int) -> List[Tuple[int, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, filename FROM sessions WHERE user_id = ?", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows


def list_all_sessions_db() -> List[Tuple[int, int, str]]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, filename FROM sessions")
    rows = cur.fetchall()
    conn.close()
    return rows


def get_all_user_ids() -> Set[int]:
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    ids = set()
    # from apis
    cur.execute("SELECT user_id FROM apis")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    # from channels
    cur.execute("SELECT DISTINCT user_id FROM channels")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    # from sessions
    cur.execute("SELECT DISTINCT user_id FROM sessions")
    rows = cur.fetchall()
    for r in rows:
        if r[0]:
            ids.add(r[0])
    conn.close()
    return ids


def list_users_db() -> List[int]:
    return sorted(list(get_all_user_ids()))


# ============ واجهة المستخدم ============
def main_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📤 رفع جلسة", callback_data="upload_session")],
            [InlineKeyboardButton("➕ إضافة قناة", callback_data="add_channel")],
            [InlineKeyboardButton("🗑️ حذف قناة", callback_data="delete_channel")],
            [InlineKeyboardButton("📜 عرض القنوات", callback_data="list_channels")],
            [InlineKeyboardButton("🔐 إضافة API (api_id / api_hash)", callback_data="add_api")],
            [InlineKeyboardButton("👀 عرض API الخاص بي", callback_data="view_api")],
            [InlineKeyboardButton("📁 عرض الجلسات", callback_data="list_sessions")],
        ]
    )


def admin_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👥 عرض المستخدمين", callback_data="admin_list_users")],
            [InlineKeyboardButton("🔐 عرض كل APIs", callback_data="admin_list_apis")],
            [InlineKeyboardButton("📜 عرض كل القنوات", callback_data="admin_list_channels")],
            [InlineKeyboardButton("📁 عرض كل الجلسات", callback_data="admin_list_sessions")],
            [InlineKeyboardButton("📢 رسالة جماعية (broadcast)", callback_data="admin_broadcast")],
            [InlineKeyboardButton("📊 إحصائيات", callback_data="admin_stats")],
        ]
    )


# ============ Handlers ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("مرحباً! اختر من القائمة:", reply_markup=main_menu())


# central pressed_button: handles both user and admin callback actions
async def pressed_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id

    # ---------------- admin-only callbacks ----------------
    if q.data.startswith("admin_"):
        if user_id != ADMIN_ID:
            await q.edit_message_text("❌ هذا القسم مخصّص للمشرف فقط.")
            return

        # admin: list users
        if q.data == "admin_list_users":
            users = list_users_db()
            if not users:
                await q.edit_message_text("لا يوجد مستخدمون مسجلون.", reply_markup=admin_menu())
                return
            text = "👥 المستخدمون (user_id):\n" + "\n".join(str(u) for u in users)
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

        # admin: list apis
        if q.data == "admin_list_apis":
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT user_id, api_id, api_hash FROM apis")
            rows = cur.fetchall()
            conn.close()
            if not rows:
                await q.edit_message_text("لا توجد APIs مسجلة.", reply_markup=admin_menu())
                return
            text = "🔐 APIs:\n" + "\n".join([f"- {r[0]}: {r[1]} | {r[2]}" for r in rows])
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

        # admin: list all channels
        if q.data == "admin_list_channels":
            rows = list_all_channels_db()
            if not rows:
                await q.edit_message_text("لا توجد قنوات مسجلة.", reply_markup=admin_menu())
                return
            text = "📜 جميع القنوات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} {r[2]} -> {r[3]}" for r in rows])
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

        # admin: list all sessions
        if q.data == "admin_list_sessions":
            rows = list_all_sessions_db()
            if not rows:
                await q.edit_message_text("لا توجد جلسات محفوظة.", reply_markup=admin_menu())
                return
            text = "📁 جميع الجلسات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} file:{r[2]}" for r in rows])
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

        # admin: begin broadcast flow
        if q.data == "admin_broadcast":
            context.user_data["mode"] = "admin_broadcast_wait"
            await q.edit_message_text("📢 أرسل الآن نص الرسالة التي تريد إرسالها إلى كل المستخدمين.")
            return

        # admin stats
        if q.data == "admin_stats":
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(DISTINCT user_id) FROM (SELECT user_id FROM apis UNION SELECT user_id FROM channels UNION SELECT user_id FROM sessions)")
            users_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM channels")
            channels_count = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM sessions")
            sessions_count = cur.fetchone()[0]
            conn.close()
            text = f"📊 إحصائيات:\n- مستخدمون مميزون: {users_count}\n- قنوات: {channels_count}\n- جلسات محفوظة: {sessions_count}"
            await q.edit_message_text(text, reply_markup=admin_menu())
            return

    # ---------------- non-admin admin-panel shortcut (/admin button) ----------------
    if q.data == "open_admin_panel":
        if user_id != ADMIN_ID:
            await q.edit_message_text("❌ هذا القسم مخصّص للمشرف فقط.")
            return
        await q.edit_message_text("لوحة المشرف:", reply_markup=admin_menu())
        return

    # ---------------- confirmation deletion flow ----------------
    if q.data.startswith("confirm_del:"):
        try:
            chid = int(q.data.split(":", 1)[1])
        except Exception:
            await q.edit_message_text("خطأ: معرّف القناة غير صالح.", reply_markup=main_menu())
            return
        delete_channel_db(chid)
        await q.edit_message_text("✔️ تم حذف القناة.", reply_markup=main_menu())
        return

    if q.data == "cancel_del":
        await q.edit_message_text("❌ تم إلغاء الحذف.", reply_markup=main_menu())
        return

    if q.data.startswith("del:"):
        try:
            chid = int(q.data.split(":", 1)[1])
        except Exception:
            await q.edit_message_text("خطأ: معرّف القناة غير صحيح.", reply_markup=main_menu())
            return
        confirm_keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✔️ نعم، احذف", callback_data=f"confirm_del:{chid}")],
                [InlineKeyboardButton("❌ لا، إلغاء", callback_data="cancel_del")],
            ]
        )
        await q.edit_message_text("هل أنت متأكد من حذف هذه القناة؟", reply_markup=confirm_keyboard)
        return

    # ---------------- user actions ----------------
    if q.data == "upload_session":
        context.user_data["mode"] = "upload_session"
        await q.edit_message_text("🟦 أرسل الآن ملف الجلسة (ملف .session أو ما لديك).")
        return

    if q.data == "add_channel":
        context.user_data["mode"] = "add_channel_wait_channel"
        await q.edit_message_text("أرسل معرف القناة مثل @example (أو اسم القناة بدون @).")
        return

    if q.data == "delete_channel":
        channels = list_channels_db(user_id)
        if not channels:
            await q.edit_message_text("❌ لا توجد قنوات لديك للحذف.", reply_markup=main_menu())
            return
        buttons = [
            [InlineKeyboardButton(f"{ch[1]} → {ch[2]}", callback_data=f"del:{ch[0]}")]
            for ch in channels
        ]
        await q.edit_message_text("اختر القناة لحذفها:", reply_markup=InlineKeyboardMarkup(buttons))
        return

    if q.data == "list_channels":
        channels = list_channels_db(user_id)
        if not channels:
            await q.edit_message_text("لا توجد قنوات.", reply_markup=main_menu())
            return
        text = "📜 قنواتك:\n" + "\n".join([f"- {c[1]}  (to: {c[2]}) [id:{c[0]}]" for c in channels])
        await q.edit_message_text(text, reply_markup=main_menu())
        return

    if q.data == "add_api":
        context.user_data["mode"] = "add_api_wait_id"
        await q.edit_message_text("أرسل الآن `api_id` كرسالة (أرسل الرقم فقط).")
        return

    if q.data == "view_api":
        row = get_api(user_id)
        if not row:
            await q.edit_message_text("❌ لم تسجّل API_ID / API_HASH بعد.", reply_markup=main_menu())
            return
        api_id, api_hash = row
        await q.edit_message_text(f"🔐 API الخاص بك:\napi_id: `{api_id}`\napi_hash: `{api_hash}`", reply_markup=main_menu())
        return

    # Unknown callback
    await q.edit_message_text("تمّت العملية.", reply_markup=main_menu())


async def text_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user
    user_id = user.id
    text = update.message.text.strip()

    # ---------------- فلترة كلمة "code" — تحذف أي ظهور (case-insensitive) ----------------
    if re.search(r"(?i)code", text):
        cleaned = re.sub(r"(?i)code", "", text).strip()
        cleaned = cleaned if cleaned else "❌ تم حذف كلمة code من رسالتك."
        await update.message.reply_text(cleaned)
        return

    mode = context.user_data.get("mode")

    # ---------- admin broadcast flow ----------
    if mode == "admin_broadcast_wait":
        # فقط المشرف يمكنه إرسال broadcast
        if user_id != ADMIN_ID:
            context.user_data["mode"] = None
            await update.message.reply_text("❌ غير مصرح لك.")
            return
        broadcast_text = text
        context.user_data["mode"] = None
        user_ids = list(get_all_user_ids())
        if not user_ids:
            await update.message.reply_text("لا يوجد مستخدمون للإرسال إليهم.", reply_markup=admin_menu())
            return
        sent = 0
        failed = 0
        await update.message.reply_text(f"♻️ جارٍ إرسال الرسالة إلى {len(user_ids)} مستخدماً ...")
        for uid in user_ids:
            try:
                await context.bot.send_message(uid, broadcast_text)
                sent += 1
                # صغير تأخير لتفادي قيود rate limits
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        await update.message.reply_text(f"✅ انتهى البث. تم الإرسال: {sent}. فشل: {failed}", reply_markup=admin_menu())
        return

    # ---------- add API flow ----------
    if mode == "add_api_wait_id":
        context.user_data["tmp_api_id"] = text
        context.user_data["mode"] = "add_api_wait_hash"
        await update.message.reply_text("حسناً. الآن أرسل `api_hash` (السلسلة).")
        return

    if mode == "add_api_wait_hash":
        api_id = context.user_data.get("tmp_api_id")
        api_hash = text
        if not api_id:
            await update.message.reply_text("خطأ داخلي: لم يتم العثور على api_id. أعد العملية بالضغط على زر إضافة API.")
            context.user_data["mode"] = None
            return
        save_api(user_id, api_id, api_hash)
        context.user_data.pop("tmp_api_id", None)
        context.user_data["mode"] = None
        await update.message.reply_text("✔️ تم حفظ API_ID و API_HASH بنجاح.", reply_markup=main_menu())
        return

    # ---------- add channel: step 1 (channel) ----------
    if mode == "add_channel_wait_channel":
        channel = text
        if not channel.startswith("@"):
            channel = "@" + channel
        context.user_data["tmp_channel"] = channel
        context.user_data["mode"] = "add_channel_wait_target"
        await update.message.reply_text("حسناً. الآن أرسل اسم بوت الهدف (مثال: @target_bot).")
        return

    # ---------- add channel: step 2 (target bot) ----------
    if mode == "add_channel_wait_target":
        target = text
        if not target.startswith("@"):
            target = "@" + target
        channel = context.user_data.get("tmp_channel")
        if not channel:
            await update.message.reply_text("خطأ: لم يتم العثور على اسم القناة. أعد العملية.")
            context.user_data["mode"] = None
            return
        add_channel_db(user_id, channel, target)
        context.user_data.pop("tmp_channel", None)
        context.user_data["mode"] = None
        await update.message.reply_text(f"✔️ تم إضافة القناة {channel} مع بوت الهدف {target}.", reply_markup=main_menu())
        return

    # ---------- default ----------
    await update.message.reply_text("استخدم الأزرار للتنقل أو /start لعرض القائمة.", reply_markup=main_menu())


async def file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    if context.user_data.get("mode") != "upload_session":
        return
    doc = update.message.document
    if not doc:
        await update.message.reply_text("❌ لم يتم العثور على ملف.")
        return
    file_obj = await doc.get_file()
    raw = await file_obj.download_as_bytearray()
    b64 = base64.b64encode(raw).decode()
    filename = doc.file_name
    save_session_db(user_id, filename, b64)
    # حفظ نسخة محلية اختيارية
    try:
        with open(os.path.join(SESSIONS_DIR, filename), "wb") as f:
            f.write(base64.b64decode(b64))
    except Exception:
        pass
    context.user_data["mode"] = None
    await update.message.reply_text("✔️ تم حفظ الجلسة في قاعدة البيانات.", reply_markup=main_menu())


# ============ أوامر Admin (text commands) ============
async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    await update.message.reply_text("لوحة المشرف:", reply_markup=admin_menu())


async def list_users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    users = list_users_db()
    if not users:
        await update.message.reply_text("لا يوجد مستخدمون مسجلون.")
        return
    text = "👥 المستخدمون (user_id):\n" + "\n".join(str(u) for u in users)
    await update.message.reply_text(text)


async def list_apis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT user_id, api_id, api_hash FROM apis")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await update.message.reply_text("لا توجد APIs مسجلة.")
        return
    text = "🔐 APIs:\n" + "\n".join([f"- {r[0]}: {r[1]} | {r[2]}" for r in rows])
    await update.message.reply_text(text)


async def list_channels_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    rows = list_all_channels_db()
    if not rows:
        await update.message.reply_text("لا توجد قنوات مسجلة.")
        return
    text = "📜 جميع القنوات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} {r[2]} -> {r[3]}" for r in rows])
    await update.message.reply_text(text)


async def list_sessions_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    rows = list_all_sessions_db()
    if not rows:
        await update.message.reply_text("لا توجد جلسات محفوظة.")
        return
    text = "📁 جميع الجلسات:\n" + "\n".join([f"- id:{r[0]} user:{r[1]} file:{r[2]}" for r in rows])
    await update.message.reply_text(text)


async def admin_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM (SELECT user_id FROM apis UNION SELECT user_id FROM channels UNION SELECT user_id FROM sessions)")
    users_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM channels")
    channels_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM sessions")
    sessions_count = cur.fetchone()[0]
    conn.close()
    text = f"📊 إحصائيات:\n- مستخدمون مميزون: {users_count}\n- قنوات: {channels_count}\n- جلسات محفوظة: {sessions_count}"
    await update.message.reply_text(text)


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text("❌ هذا الأمر مخصّص للمشرف فقط.")
        return
    context.user_data["mode"] = "admin_broadcast_wait"
    await update.message.reply_text("📢 أرسل الآن نص الرسالة التي تريد إرسالها إلى كل المستخدمين.")


# ============ تشغيل Webhook ============
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # مثال: https://myapp.onrender.com

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("users", list_users_command))
    app.add_handler(CommandHandler("list_apis", list_apis_command))
    app.add_handler(CommandHandler("list_channels_all", list_channels_all_command))
    app.add_handler(CommandHandler("list_sessions_all", list_sessions_all_command))
    app.add_handler(CommandHandler("admin_stats", admin_stats_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))

    app.add_handler(CallbackQueryHandler(pressed_button))
    app.add_handler(MessageHandler(filters.Document.ALL, file_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))

    # Webhook (مهيأ للعمل على Render)
    app.run_webhook(
        listen="0.0.0.0",
        port=10000,
        url_path=BOT_TOKEN,
        webhook_url=f"{WEBHOOK_URL}/{BOT_TOKEN}",
        allowed_updates=None,  # كل الأنواع
    )


if __name__ == "__main__":
    main()
