#!/usr/bin/env python3
# create_session.py - run locally to create session file using API from DB or manual entry
import sqlite3, os
from pyrogram import Client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "bot.db")
SESSIONS_DIR = os.path.join(BASE_DIR, "sessions")
os.makedirs(SESSIONS_DIR, exist_ok=True)

def read_api_from_db():
    try:
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("SELECT api_id, api_hash FROM sessions ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        conn.close()
        if row:
            return int(row[0]), str(row[1])
    except Exception as e:
        print("DB read error:", e)
    return None, None

def main():
    api_id, api_hash = read_api_from_db()
    if not api_id or not api_hash:
        api_id = int(input("Enter API_ID: ").strip())
        api_hash = input("Enter API_HASH: ").strip()
    session_name = input("Session name (listener): ").strip() or "listener"
    print("Starting Pyrogram to create session locally...")
    with Client(session_name, api_id=api_id, api_hash=api_hash) as app:
        me = app.get_me()
        print("Logged in as:", me.first_name, "@"+(me.username or ""))
        print("Session saved as:", session_name + ".session (or folder). Move it to server's sessions/ directory.")

if __name__ == "__main__":
    main()
