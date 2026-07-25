"""Print chat IDs the bot can see.

Usage: send any message to your bot in Telegram first, then run:
    TELEGRAM_BOT_TOKEN=xxx python scripts/get_chat_id.py
"""

import os
import sys

import requests

token = os.environ.get("TELEGRAM_BOT_TOKEN")
if not token:
    sys.exit("Set TELEGRAM_BOT_TOKEN first")

updates = requests.get(
    f"https://api.telegram.org/bot{token}/getUpdates", timeout=30
).json()

chats = {}
for update in updates.get("result", []):
    message = update.get("message") or update.get("channel_post") or {}
    chat = message.get("chat")
    if chat:
        chats[chat["id"]] = chat

if not chats:
    print("No chats found — send your bot a message first, then re-run.")
for chat_id, chat in chats.items():
    name = chat.get("title") or chat.get("username") or chat.get("first_name")
    print(f"TELEGRAM_CHAT_ID={chat_id}  ({chat.get('type')}: {name})")
