import pytest
import json
import asyncio
from datetime import datetime, timedelta
import dateutil.tz
import server

# Need to tell pytest-asyncio what scope to use for event_loop
@pytest.fixture(scope="session")
def event_loop():
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    yield loop
    loop.close()

# The proper modern pytest-asyncio way is to use async def with @pytest_asyncio.fixture or pytest.mark.asyncio around tests
@pytest.fixture(scope="session", autouse=True)
def setup_server_data(event_loop):
    event_loop.run_until_complete(server.fetch_israel_cities())

def test_extract_areas_from_text():
    text = "שיגורים לצפון, באזור נהריה" # "LaZafon", "be'ezor Nahariya"
    areas = server.extract_areas_from_text(text)
    # The server logic strips prefixes. Assuming "צפון" and "נהריה" are in KNOWN_AREAS.
    assert 'צפון' in areas
    assert 'נהריה' in areas

def test_extract_forecast_data_exact():
    text = "שיגור ממרכז הרצועה, צפי הגעה 2 דקות.\nאזעקות בשפלה, מרכז."
    result = server.extract_forecast_data(text)
    assert len(result["alerts"]) > 0
    valid_alerts = [a for a in result["alerts"] if a.get("expected_seconds")]
    assert len(valid_alerts) > 0
    assert valid_alerts[0]["expected_seconds"] == 120
    # Also verify it caught areas
    all_areas = []
    for a in result["alerts"]:
        all_areas.extend(a["areas"])
    assert 'שפלה' in all_areas
    assert 'מרכז' in all_areas

@pytest.mark.asyncio
async def test_process_real_data():
    with open('pytest_data.json', 'r', encoding='utf-8') as f:
        raw_data = json.load(f)
        
    by_channel = {}
    for msg in raw_data:
        ch = msg.get("channel", "shigurimsh")
        if ch not in by_channel:
            by_channel[ch] = []
        msg["msg_dt"] = datetime.fromisoformat(msg["date"])
        by_channel[ch].append(msg)
        
    # Reset tracking arrays to test cleanly
    server.alert_history.clear()
    server.latest_event = {"has_data": False}
    server.active_alerts_by_area.clear()
    server.channel_last_areas.clear()
    
    # We must patch datetime.now so the -12h cutoff doesn't block the json msgs
    latest_msg = sorted(raw_data, key=lambda x: x["msg_dt"])[-1]["msg_dt"]
    original_now = server.datetime.now
    
    mock_now = latest_msg + timedelta(minutes=5)
    
    class MockDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return mock_now

    server.datetime = MockDatetime
    
    try:
        # process in chronological order per-channel
        for ch, msgs in by_channel.items():
            msgs.sort(key=lambda x: x["msg_dt"])
            await server.process_forecast_messages(msgs, ch, is_init=True)
            
        assert len(server.alert_history) > 0, "No alerts were saved to history!"
        
        # Verify latest_event is populated
        if server.latest_event["has_data"]:
            assert "alerts" in server.latest_event
            assert type(server.latest_event["alerts"]) is list 
            
    finally:
        server.datetime = original_now
