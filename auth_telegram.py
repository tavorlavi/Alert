"""
Run this script ONCE to authenticate your Telegram session.
It will create a 'session.session' file that server.py will use.
After running this, you can start server.py normally.
"""
import os
from telethon import TelegramClient

api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
api_hash = os.environ.get("TELEGRAM_API_HASH", "")

if not api_id or not api_hash:
    api_id = int(input("Enter your Telegram API ID: "))
    api_hash = input("Enter your Telegram API Hash: ")

client = TelegramClient("session", api_id, api_hash)

async def main():
    await client.start()
    me = await client.get_me()
    print(f"\n✅ Authenticated as: {me.first_name} (ID: {me.id})")
    print("Session file saved. You can now run server.py")
    await client.disconnect()

import asyncio
asyncio.run(main())
