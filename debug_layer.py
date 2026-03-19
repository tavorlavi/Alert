import json
from datetime import datetime
import asyncio
import os

async def debug_load_messages():
    try:
        if os.path.exists('real_messages.json'):
            print("🐞 DEBUG: Loading messages from real_messages.json")
            with open('real_messages.json', 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            # Sort chronologically so they replay correctly
            data.sort(key=lambda x: x["date"])
            
            # Group by channel
            by_channel = {}
            for msg in data:
                ch = msg.get("channel", "shigurimsh")
                if ch not in by_channel:
                    by_channel[ch] = []
                # Make sure msg_dt exists
                msg["msg_dt"] = datetime.fromisoformat(msg["date"])
                by_channel[ch].append(msg)
                
            # Override 12-hour cutoff by using the max date of messages as 'now'
            # But the code inside process_forecast_messages uses datetime.now().
            # To fix this without modifying process_forecast_messages heavily, 
            # we can temporarily mock datetime.now() for this module!
            
            import server
            original_now = server.datetime.now
            if data:
                latest_msg_dt = data[-1]["msg_dt"]
                # Mock it to 5 minutes after the last message
                mock_now = latest_msg_dt + server.timedelta(minutes=5)
                server.datetime.now = lambda tz=None: mock_now
                
                print(f"🐞 DEBUG: Faked 'now' to {mock_now}")
            
                for ch, msgs in by_channel.items():
                    print(f"🐞 Processing {len(msgs)} debug messages for {ch}")
                    await server.process_forecast_messages(msgs, ch, is_init=True)
                    
                # Restore original
                server.datetime.now = original_now
                print("🐞 DEBUG: Finished loading JSON data!")
    except Exception as e:
        print(f"DEBUG LOAD FAILED: {e}")
