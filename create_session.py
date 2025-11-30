# create_session.py
#!/usr/bin/env python3
from pyrogram import Client

API_ID = int(input("Enter API_ID: ").strip())
API_HASH = input("Enter API_HASH: ").strip()
SESSION_NAME = input("Session name (listener): ").strip() or "listener"

print("This will open a Pyrogram client and ask for your phone and code. Create the session locally and then upload the generated session file (listener.session) to the server's sessions/ folder.")
with Client(SESSION_NAME, api_id=API_ID, api_hash=API_HASH) as app:
    me = app.get_me()
    print("Logged in as:", me.first_name, "@"+(me.username or ""))
    print(f"Session saved as: {SESSION_NAME}.session (or a folder). Upload it to the server sessions/ directory.")
