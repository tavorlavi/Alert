"""
Run this locally to export your Telegram session as a base64 string.
Copy the output and paste it as the TELEGRAM_SESSION env var in Render.
"""
import base64
import os

session_file = "session.session"
if not os.path.exists(session_file):
    print(f"❌ File '{session_file}' not found. Run auth_telegram.py first.")
else:
    with open(session_file, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    print("=" * 60)
    print("Copy the string below and set it as TELEGRAM_SESSION in Render:")
    print("=" * 60)
    print(b64)
    print("=" * 60)
    print(f"Length: {len(b64)} chars")
