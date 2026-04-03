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
    replacements, polygons = server.build_mivzak_replacements(cities)
    # Tel Aviv cluster and Beer Sheva should be in separate groups
    assert "מרכז" in replacements
    assert "תל אביב" in replacements["מרכז"]
    assert "רמת גן" in replacements["מרכז"]
    assert "חולון" in replacements["מרכז"]
    assert "דרום" in replacements
    assert "באר שבע" in replacements["דרום"]
    assert "מרכז" in polygons
    assert len(polygons["מרכז"]) >= 3  # valid polygon


def test_build_mivzak_replacements_unknown_cities():
    cities = ["עיר שלא קיימת", "תל אביב"]
    replacements, polygons = server.build_mivzak_replacements(cities)
    assert "מרכז" in replacements
    assert "תל אביב" in replacements["מרכז"]
    assert len(replacements) == 1


def test_cluster_by_proximity_multiple_groups():
    """Cities in north and south should form separate clusters."""
    city_coords = [
        ("חיפה", 32.794, 34.989),
        ("עכו", 32.927, 35.084),
        ("נהריה", 33.004, 35.094),
        ("באר שבע", 31.252, 34.791),
        ("אשדוד", 31.804, 34.649),
        ("אשקלון", 31.668, 34.571),
    ]
    clusters = server._cluster_by_proximity(city_coords)
    # North cities cluster together, south cities split by distance
    assert len(clusters) >= 2
    cluster_names = [set(c) for c in clusters]
    north = {"חיפה", "עכו", "נהריה"}
    assert north in cluster_names
    # אשדוד and אשקלון are close, should be together
    ashkelon_cluster = [c for c in cluster_names if "אשקלון" in c][0]
    assert "אשדוד" in ashkelon_cluster
    # באר שבע is far from אשדוד, should NOT be in same cluster
    assert "באר שבע" not in ashkelon_cluster


def test_cluster_by_proximity_single_cluster():
    """Nearby cities should form one cluster."""
    city_coords = [
        ("תל אביב", 32.066, 34.788),
        ("בת ים", 32.017, 34.751),
        ("חולון", 32.011, 34.780),
    ]
    clusters = server._cluster_by_proximity(city_coords)
    assert len(clusters) == 1
    assert set(clusters[0]) == {"תל אביב", "בת ים", "חולון"}


def test_build_mivzak_splits_distant_cities():
    """Cities far apart should get separate tight polygons."""
    cities = ["תל אביב", "חולון", "בת ים", "חיפה", "עכו", "נהריה"]
    replacements, polygons = server.build_mivzak_replacements(cities)
    assert len(replacements) >= 2
    assert len(polygons) >= 2


def test_build_mivzak_ashdod_not_in_center():
    """אשדוד should NOT be grouped with center cities (the old bug)."""
    cities = ["תל אביב", "רמת גן", "אשדוד", "אשקלון"]
    replacements, polygons = server.build_mivzak_replacements(cities)
    # אשדוד and אשקלון should not be in the same group as תל אביב
    for area, area_cities in replacements.items():
        if "תל אביב" in area_cities:
            assert "אשדוד" not in area_cities, "אשדוד should not be in same cluster as תל אביב"
            assert "אשקלון" not in area_cities, "אשקלון should not be in same cluster as תל אביב"


def test_mivzak_timeout_state():
    """merge_mivzak should set _mivzak_last_update."""
    server.active_mivzak.clear()
    server.active_mivzak_polygons.clear()
    server._mivzak_last_update = None
    replacements = {"מרכז": ["תל אביב"]}
    server.merge_mivzak(replacements)
    assert server._mivzak_last_update is not None
    server.active_mivzak.clear()
    server.active_mivzak_polygons.clear()
    server._mivzak_last_update = None


# ==========================
# Algorithm improvement regression tests
# ==========================

class TestUrlCleaning:
    def test_clean_forecast_text_strips_bare_tme_url(self):
        text = "שיגור למרכז\nt.me/beforeredalert"
        cleaned = server.clean_forecast_text(text)
        assert "t.me" not in cleaned

    def test_clean_forecast_text_strips_https_tme_url(self):
        text = "שיגור למרכז\nhttps://t.me/shigurimsh"
        cleaned = server.clean_forecast_text(text)
        assert "t.me" not in cleaned

    def test_extract_areas_bare_tme_not_area(self):
        text = "שיגורים למרכז\nt.me/beforeredalert"
        areas = server.extract_areas_from_text(text)
        assert "מרכז" in areas
        assert "tme" not in areas
        assert "beforeredalert" not in areas

    def test_extract_forecast_data_bare_tme_not_area(self):
        text = "שיגור למרכז עוד 5 דקות\nt.me/beforeredalert"
        result = server.extract_forecast_data(text)
        all_areas = [a for alert in result["alerts"] for a in alert["areas"]]
        assert "מרכז" in all_areas
        assert "tme" not in all_areas
        assert "beforeredalert" not in all_areas


class TestDurationParsing:
    def test_vahetzi_daka(self):
        """דקה וחצי = 1.5 minutes = 90 seconds"""
        assert server.extract_expected_time_text("דקה וחצי") == "דקה וחצי"
        assert server._to_expected_seconds("דקה וחצי") == 90

    def test_vahetzi_with_number(self):
        """6 וחצי דקות = 6.5 minutes = 390 seconds"""
        assert server._to_expected_seconds(server.extract_expected_time_text("6 וחצי דקות")) == 390

    def test_vahetzi_in_sentence(self):
        """Extract וחצי from a full message"""
        text = "עוד דקה וחצי אזעקה במרכז"
        result = server.extract_forecast_data(text)
        valid = [a for a in result["alerts"] if a.get("expected_seconds")]
        assert len(valid) > 0
        assert valid[0]["expected_seconds"] == 90

    def test_dak_abbreviation(self):
        """7 דק = 7 minutes = 420 seconds"""
        assert server.extract_expected_time_text("7 דק") == "7 דק"
        assert server._to_expected_seconds("7 דק") == 420

    def test_dak_decimal(self):
        """5.5 דק = 5.5 minutes = 330 seconds"""
        assert server._to_expected_seconds("5.5 דק") == 330

    def test_range_notation(self):
        """3/4 דקות = take max(3,4) = 4 minutes = 240 seconds"""
        text = "3/4 דקות"
        assert server._to_expected_seconds(server.extract_expected_time_text(text)) == 240

    def test_standard_dakot_unchanged(self):
        """Existing patterns still work"""
        assert server._to_expected_seconds("5 דקות") == 300
        assert server._to_expected_seconds("35 שניות") == 35
        assert server._to_expected_seconds("4.5 דקות") == 270
        assert server._to_expected_seconds("דקה") == 60


class TestAreaExtraction:
    def test_mikud_extracts_area(self):
        """מיקוד should not block area extraction"""
        result = server.extract_forecast_data("מיקוד דימונה")
        all_areas = [a for alert in result["alerts"] for a in alert["areas"]]
        assert "דרום" in all_areas

    def test_mikud_prefix_stripped(self):
        areas = server.extract_areas_from_text("מיקוד אזור נהריה")
        assert "צפון" in areas
        assert "מיקוד" not in areas

    def test_merhav_prefix_stripped(self):
        areas = server.extract_areas_from_text("מרחב שרון")
        assert "שרון" in areas

    def test_lehitmagen_not_area(self):
        areas = server.extract_areas_from_text("מרכז להתמגן")
        assert "מרכז" in areas
        assert "להתמגן" not in areas
        assert "התמגן" not in areas

    def test_metzarer_not_area(self):
        areas = server.extract_areas_from_text("מצרר להתמגן")
        assert "מצרר" not in areas

    def test_la_yufalu_skipped(self):
        """Lines with לא יופעלו should be skipped entirely"""
        areas = server.extract_areas_from_text("לא יופעלו אזעקות בדרום")
        assert "דרום" not in areas

    def test_beit_shemesh_maps_to_yerushalayim(self):
        result = server.extract_forecast_data("שיגור לבית שמש")
        all_areas = [a for alert in result["alerts"] for a in alert["areas"]]
        assert "ירושלים" in all_areas

    def test_modiin_maps_to_merkaz(self):
        result = server.extract_forecast_data("שיגור למודיעין")
        all_areas = [a for alert in result["alerts"] for a in alert["areas"]]
        assert "מרכז" in all_areas

    def test_kineret_recognized(self):
        areas = server.extract_areas_from_text("שיגור לכינרת")
        assert "כינרת" in areas

    def test_haamakim_recognized(self):
        areas = server.extract_areas_from_text("שיגור לעמקים")
        assert "עמקים" in areas

    def test_hebrew_abbreviation_beer_sheva(self):
        areas = server.extract_areas_from_text('שיגור לב"ש')
        assert "דרום" in areas

    def test_hebrew_abbreviation_petach_tikva(self):
        areas = server.extract_areas_from_text('שיגור לפ"ת')
        assert "מרכז" in areas

    def test_lerech_not_area(self):
        """לערך (approximately) should not be extracted as an area"""
        areas = server.extract_areas_from_text("7 דקות לערך")
        assert "ערך" not in areas
        assert "לערך" not in areas

    def test_beit_shean_maps_to_tzafon(self):
        result = server.extract_forecast_data("שיגור לבית שאן")
        all_areas = [a for alert in result["alerts"] for a in alert["areas"]]
        assert "צפון" in all_areas

    def test_gezrat_prefix_stripped(self):
        """גזרת (sector) should be stripped like אזור"""
        areas = server.extract_areas_from_text("שיגורים לגזרת בית שאן")
        assert "צפון" in areas

    def test_izor_misspelling_stripped(self):
        """איזור (misspelling of אזור) should be handled"""
        areas = server.extract_areas_from_text("איזור שפלה")
        assert "שפלה" in areas

    def test_nosafim_not_blocking_line(self):
        """נוספים should not block the entire line"""
        result = server.extract_forecast_data("שיגורים נוספים לדרום")
        all_areas = [a for alert in result["alerts"] for a in alert["areas"]]
        assert "דרום" in all_areas

    def test_haga_a_not_area(self):
        """הגעה should not be extracted as an area"""
        areas = server.extract_areas_from_text("הגעה למרכז בקרוב")
        assert "הגעה" not in areas
        assert "מרכז" in areas

    def test_yetziot_not_area(self):
        areas = server.extract_areas_from_text("יציאות מאיראן לדרום")
        assert "יציאות" not in areas
        assert "דרום" in areas

    def test_yokneam_maps_to_tzafon(self):
        result = server.extract_forecast_data("שיגור ליוקנעם")
        all_areas = [a for alert in result["alerts"] for a in alert["areas"]]
        assert "צפון" in all_areas


class TestDurationEdgeCases:
    def test_hatzi_daka_30_seconds(self):
        """חצי דקה = 0.5 minutes = 30 seconds"""
        assert server.extract_expected_time_text("חצי דקה") == "חצי דקה"
        assert server._to_expected_seconds("חצי דקה") == 30

    def test_hatzi_daka_in_sentence(self):
        assert server._to_expected_seconds(server.extract_expected_time_text("חצי דקה לאזעקה")) == 30

    def test_dash_range(self):
        """3-4 דקות = take max = 4 minutes = 240 seconds"""
        assert server._to_expected_seconds(server.extract_expected_time_text("3-4 דקות")) == 240

    def test_dash_range_in_sentence(self):
        text = "3-4 דקות לאזעקה"
        assert server._to_expected_seconds(server.extract_expected_time_text(text)) == 240


class TestTimingPreservation:
    """Verify timing is not lost when area-only messages arrive from faster channels."""

    def _reset_state(self):
        server.today_forecasts.clear()
        server.today_messages.clear()
        server.alert_history.clear()
        server.active_alerts_by_area.clear()
        server.channel_last_areas.clear()
        server.pending_forecast_parts.clear()
        for key in server.telegram_last_seen_ids:
            server.telegram_last_seen_ids[key].clear()
        server.latest_event = {"text": "ממתין לעדכונים...", "target_time": None, "has_data": False}

    def _make_msg(self, text, channel, dt, msg_id):
        return {"text": text, "msg_dt": dt, "id": str(msg_id), "channel": channel}

    def test_area_only_preserves_existing_timing(self):
        """When an area-only msg arrives after a timed msg for same area, timing is preserved."""
        self._reset_state()
        local_tz = server.local_tz
        now = datetime(2026, 4, 2, 19, 18, 0, tzinfo=local_tz)
        original_dt = server.datetime

        class MockDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        server.datetime = MockDT

        import asyncio
        loop = asyncio.new_event_loop()
        try:
            # Channel 1 (alert_Real_Time): full alert with timing
            msgs_rt = [self._make_msg("שיגור מאיראן למרכז\nעוד 8 דקות אזעקה", "alert_Real_Time", now, "rt1")]
            loop.run_until_complete(server.process_forecast_messages(msgs_rt, "alert_Real_Time", is_init=True))

            # Verify timing is set
            merkaz = server.active_alerts_by_area.get("מרכז")
            assert merkaz is not None
            assert merkaz["target_time"] is not None
            saved_target = merkaz["target_time"]

            # Channel 2 (shigurimsh): area-only, no timing
            msgs_sh = [self._make_msg("שיגורים למרכז", "shigurimsh", now + timedelta(seconds=120), "sh1")]
            loop.run_until_complete(server.process_forecast_messages(msgs_sh, "shigurimsh", is_init=True))

            # Timing must still be present
            merkaz = server.active_alerts_by_area.get("מרכז")
            assert merkaz is not None, "מרכז entry was deleted"
            assert merkaz["target_time"] == saved_target, \
                f"Timing lost: expected {saved_target}, got {merkaz['target_time']}"
        finally:
            loop.close()
            server.datetime = original_dt

    def test_rebuild_latest_event_groups_timing(self):
        """_rebuild_latest_event produces correct grouped output with timing."""
        self._reset_state()
        local_tz = server.local_tz
        now = datetime(2026, 4, 2, 19, 18, 0, tzinfo=local_tz)
        target = (now + timedelta(seconds=480)).isoformat()
        original_dt = server.datetime

        class MockDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        server.datetime = MockDT

        try:
            server.active_alerts_by_area["מרכז"] = {
                "text": "שיגור למרכז",
                "target_time": target,
                "received_at": now.isoformat(),
                "clock_time": None,
                "expected_time_text": "8 דקות",
                "source_channel": "alert_Real_Time",
                "areas": ["מרכז"],
                "tight_polygon": None,
            }

            server._rebuild_latest_event()

            assert server.latest_event["has_data"] is True
            alerts = server.latest_event["alerts"]
            merkaz_alerts = [a for a in alerts if "מרכז" in a.get("areas", [])]
            assert len(merkaz_alerts) == 1
            assert merkaz_alerts[0]["target_time"] == target
            assert merkaz_alerts[0]["expected_time_text"] == "8 דקות"
        finally:
            server.datetime = original_dt
