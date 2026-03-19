import json
import os
import asyncio
from telethon import TelegramClient
from datetime import datetime

CHANNELS = ["shigurimsh", "alert_Real_Time", "beforeredalert"]

async def fetch_messages():
    # Attempt to load from env or rely on active session if created
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, val = line.split("=", 1)
                    os.environ.setdefault(key.strip(), val.strip())

    api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
    api_hash = os.environ.get("TELEGRAM_API_HASH", "")
    
    # We will use the existing session file "session"
    client = TelegramClient("session", api_id, api_hash)
    
    await client.connect()
    if not await client.is_user_authorized():
        print("Telegram session not authorized, please run auth_telegram.py")
        return
        
    all_messages = []
    
    for channel in CHANNELS:
        print(f"Fetching messages for {channel}...")
        try:
            entity = await client.get_entity(channel)
            messages = await client.get_messages(entity, limit=50) # fetch last 50
            
            for msg in messages:
                if not msg.text:
                    continue
                    
                msg_dt = msg.date
                all_messages.append({
                    "channel": channel,
                    "id": str(msg.id),
                    "text": msg.text,
                    "date": msg_dt.isoformat()
                })
        except Exception as e:
            print(f"Error fetching {channel}: {e}")
            
    with open("test_messages.json", "w", encoding="utf-8") as f:
        json.dump(all_messages, f, ensure_ascii=False, indent=2)
        
    print(f"Saved {len(all_messages)} messages to test_messages.json")
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(fetch_messages())