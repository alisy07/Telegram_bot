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

        cursor.execute("INSERT INTO channels(channel_name, bot_target) VALUES(?,?)",
                       (channel_name, bot_target))
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

    bot_target = row[0]
    active_channels[channel_name] = bot_target

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
