cursor.execute("SELECT bot_target FROM channels WHERE channel_name=?",(channel_name,))
    row = cursor.fetchone()
    if not row:
        await event.answer("⚠ القناة غير موجودة")
        return
    bot_target = row[0]
    if channel_name in active_channels:
        await event.answer("🔹 المراقبة بالفعل مفعلة")
        return
    active_channels[channel_name] = bot_target
    await event.answer(f"🚀 بدأ مراقبة {channel_name}")

# ======= مراقبة كل الرسائل =======
@client.on(events.NewMessage())
async def watcher(event):
    for channel_name, bot_target in active_channels.items():
        try:
            if event.chat.username == channel_name:
                text = event.raw_text.strip()
                if not text:
                    return
                cleaned = clean_text(text)
                if not cleaned:
                    return
                await send_to_target(cleaned,bot_target)
        except:
            continue

# ======= تشغيل البوت =======
print("🚀 البوت يعمل الآن...")
client.run_until_disconnected()
