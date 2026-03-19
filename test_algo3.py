import asyncio, json, dateutil.tz
from datetime import datetime, timedelta
import server

async def test():
    await server.fetch_israel_cities()
    
    with open('real_messages.json', encoding='utf-8') as f:
        data = json.load(f)
    print(f"Loaded {len(data)} messages")
    
    for msg in data:
        msg["msg_dt"] = datetime.fromisoformat(msg["date"])
    
    # fake the now time in server to be slightly after the last msg in data
    # Last msg is 2026-03-18 21:00 roughly
    server.datetime = type('MockedDatetime', (datetime,), {
        'now': classmethod(lambda cls, tz=None: datetime(2026, 3, 19, 0, 0, tzinfo=tz))
    })
    
    server.telegram_last_seen_ids = { "talarmai": set(), "shigurimsh": set(), "alert_Real_Time": set(), "beforeredalert": set() }
    
    await server.process_forecast_messages(data, 'shigurimsh', is_init=True)
    
    print("History length:", len(server.alert_history))
    if server.alert_history:
        print("Last history:", tuple(server.alert_history)[:3])

asyncio.run(test())
