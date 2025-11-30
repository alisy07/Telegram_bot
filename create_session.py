#!/usr/bin/env python3
from pyrogram import Client

API_ID = int(input("Enter API_ID: ").strip())
API_HASH = input("Enter API_HASH: ").strip()
SESSION_NAME = input("Session name (listener): ").strip() or "listener"

with Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH) as app:
    me = app.get_me()
    print("Logged in as:", me.first_name, '@' + (me.username or ''))
    print(f"Session saved as: {SESSION_NAME}.session or folder '{SESSION_NAME}'") 
