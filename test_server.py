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


# ==========================
# Pikud Haoref (oref) tests
# ==========================

def test_parse_oref_siren_cities():
    text = (
        "🚨 ירי רקטות וטילים 23.03.2026 14:30\n"
        "אזור תל אביב\n"
        "תל אביב, רמת גן (דקה וחצי)\n"
        "חולון, ראשון לציון (דקה)\n"
        "היכנסו למרחב המוגן."
    )
    cities = server.parse_oref_siren_cities(text)
    assert cities is not None
    assert "תל אביב" in cities
    assert "רמת גן" in cities
    assert "חולון" in cities
    assert "ראשון לציון" in cities
    # Timing suffixes should be stripped
    for city in cities:
        assert "(" not in city
        assert ")" not in city


def test_parse_oref_siren_cities_not_siren():
    assert server.parse_oref_siren_cities("הודעה רגילה") is None
    assert server.parse_oref_siren_cities("🚨 מבזק\nבדקות הקרובות") is None


def test_parse_oref_mivzak():
    text = (
        "🚨 מבזק 23.03.2026 14:25\n"
        "בדקות הקרובות צפויות להתקבל התרעות באזורך\n"
        "על תושבי האזורים הבאים להיכנס למרחב המוגן\n"
        "אזור תל אביב\n"
        "תל אביב, רמת גן, חולון\n"
        "אזור דן\n"
        "בני ברק, גבעתיים"
    )
    cities = server.parse_oref_mivzak(text)
    assert cities is not None
    assert "תל אביב" in cities
    assert "רמת גן" in cities
    assert "חולון" in cities
    assert "בני ברק" in cities
    assert "גבעתיים" in cities


def test_parse_oref_mivzak_not_mivzak():
    assert server.parse_oref_mivzak("הודעה רגילה") is None
    assert server.parse_oref_mivzak("🚨 ירי רקטות וטילים") is None
    # Must have BOTH markers
    assert server.parse_oref_mivzak("מבזק בלי הסיפא") is None


def test_build_mivzak_replacements():
    cities = ["תל אביב", "רמת גן", "חולון", "באר שבע"]
    result = server.build_mivzak_replacements(cities)
    assert "מרכז" in result
    assert "תל אביב" in result["מרכז"]
    assert "רמת גן" in result["מרכז"]
    assert "חולון" in result["מרכז"]
    assert "דרום" in result
    assert "באר שבע" in result["דרום"]


def test_build_mivzak_replacements_unknown_cities():
    cities = ["עיר שלא קיימת", "תל אביב"]
    result = server.build_mivzak_replacements(cities)
    assert "מרכז" in result
    assert "תל אביב" in result["מרכז"]
    assert len(result) == 1  # unknown city not mapped
