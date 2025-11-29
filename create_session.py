#!/usr/bin/env python3
import os, json, sqlite3

BASE_DIR = os.path.dirname(__file__)
CONFIG_FILE = os.path.join(BASE_DIR,"config.json")
DB_FILE = os.path.join(BASE_DIR,"bot.db")

conn=sqlite3.connect(DB_FILE)
cursor=conn.cursor()
cursor.execute("""
CREATE TABLE IF NOT EXISTS api_sessions(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    api_id TEXT,
    api_hash TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

def save_api(api_id,api_hash):
    cursor.execute("INSERT INTO api_sessions(api_id,api_hash) VALUES(?,?)",(api_id,api_hash))
    conn.commit()