
import os
import sqlite3
import logging
from flask import Flask, render_template, request, redirect
from telethon import TelegramClient, events

logging.basicConfig(level=logging.INFO)

DB = "config.db"

def init_db():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS config (
        id INTEGER PRIMARY KEY,
        bot_token TEXT,
        api_id TEXT,
        api_hash TEXT
    )""")
    conn.commit()
    conn.close()

def get_config():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT bot_token, api_id, api_hash FROM config WHERE id=1")
    row = c.fetchone()
    conn.close()
    return row if row else ("", "", "")

def save_config(bot_token, api_id, api_hash):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM config")
    c.execute("INSERT INTO config(id, bot_token, api_id, api_hash) VALUES (1,?,?,?)",
              (bot_token, api_id, api_hash))
    conn.commit()
    conn.close()

init_db()

app = Flask(__name__)
client = None

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        bot_token = request.form["bot_token"]
        api_id = request.form["api_id"]
        api_hash = request.form["api_hash"]
        save_config(bot_token, api_id, api_hash)
        return redirect("/")
    bot_token, api_id, api_hash = get_config()
    return render_template("index.html", bot_token=bot_token, api_id=api_id, api_hash=api_hash)

@app.route("/restart")
def restart():
    os.system("pkill -f main.py")
    return "Restarted"

def start_bot():
    global client
    bot_token, api_id, api_hash = get_config()
    if not bot_token or not api_id or not api_hash:
        logging.warning("Config missing.")
        return
    client = TelegramClient("session", int(api_id), api_hash)
    client.start(bot_token=bot_token)

    @client.on(events.NewMessage(pattern="/setapi"))
    async def setapi(event):
        await event.respond("Send API_ID API_HASH")

    client.run_until_disconnected()

if __name__ == "__main__":
    start_bot()
    app.run(host="0.0.0.0", port=8000)
