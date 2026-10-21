"""Find your TELEGRAM_CHAT_ID.

1. Create a bot with @BotFather, copy the token.
2. Open your bot in Telegram and send it any message (e.g. "hi").
3. python tools/telegram_chat_id.py <BOT_TOKEN>
"""
import sys

import requests

r = requests.get(f"https://api.telegram.org/bot{sys.argv[1]}/getUpdates", timeout=30).json()
chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
         for u in r.get("result", []) if "message" in u}
if not chats:
    print("No messages found. Send your bot a message first, then run again.")
for cid, name in chats.items():
    print(f"TELEGRAM_CHAT_ID = {cid}   ({name})")
