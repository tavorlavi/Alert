import asyncio, json
from server import scrape_telegram_channel, TELEGRAM_CHANNELS
import dateutil.tz

async def fetch_it():
    all_msgs = []
    for ch_name, ch_config in TELEGRAM_CHANNELS.items():
        try:
            msgs = await scrape_telegram_channel(ch_name, ch_config, max_pages=2)
            # convert datetimes to iso strings immediately
            for m in msgs:
                m['date'] = m['msg_dt'].isoformat()
                m['channel'] = ch_name
                del m['msg_dt']
            all_msgs.extend(msgs)
        except Exception as e:
            print(f"Error {ch_name}: {e}")
            
    with open('pytest_data.json', 'w', encoding='utf-8') as f:
        json.dump(all_msgs, f, ensure_ascii=False, indent=2)
    print(f"Saved {len(all_msgs)} raw real messages to pytest_data.json!")

asyncio.run(fetch_it())
