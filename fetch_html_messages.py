import httpx
import json
import asyncio
import os
import sys

# Add current dir to import server
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import server

async def fetch_and_save():
    channels = server.TELEGRAM_CHANNELS
    all_messages = []
    
    for channel_name, channel_config in channels.items():
        print(f"Fetching messages for {channel_name}...")
        try:
            messages = await server.scrape_telegram_channel(channel_name, channel_config, max_pages=1)
            for m in messages:
                if "msg_dt" in m:
                    m["msg_dt"] = m["msg_dt"].isoformat()
                all_messages.append(m)
        except Exception as e:
            print(f"Error fetching {channel_name}: {e}")

    with open("real_messages.json", "w", encoding="utf-8") as f:
        json.dump(all_messages, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(all_messages)} messages to real_messages.json.")

if __name__ == "__main__":
    asyncio.run(fetch_and_save())