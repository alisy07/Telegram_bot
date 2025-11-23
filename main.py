# ======= إضافة قناة جديدة =======
@client.on(events.CallbackQuery(data=b"new"))
async def new_channel(event):
    await event.respond("💬 أدخل اسم القناة الجديدة:")
    async with client.conversation(event.sender_id) as conv:
        ch_msg = await conv.get_response()
        channel_name = ch_msg.text.strip()
        await event.respond("🤖 أدخل اسم البوت الهدف:")
        bot_msg = await conv.get_response()
        bot_target = bot_msg.text.strip()
        cursor.execute("INSERT INTO channels(channel_name,bot_target) VALUES(?,?)",(channel_name,bot_target))
        conn.commit()
        await event.respond(f"✅ تم حفظ القناة {channel_name} مع البوت {bot_target}")

# ======= تشغيل مراقبة القنوات =======
@client.on(events.CallbackQuery)
async def start_watching(event):
    channel_name = event.data.decode()
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

print("🚀 البوت يعمل الآن...")
client.run_until_disconnected()
