# Alert — Project Guide for Agents

## What This Is

Real-time pre-siren rocket warning app for Israel. The Israeli Home Front Command (Pikud Haoref) issues official sirens, but unofficial Telegram intelligence channels post warnings **1-8 minutes before** sirens fire. This app scrapes those channels, extracts forecast timing and target areas from Hebrew text, and serves the data to a browser map.

Two independent data sources run in parallel:

| Source | What it provides | Latency |
|--------|-----------------|---------|
| 4 Telegram channels (web scrape) | Early warnings with area + arrival time forecast | 1-8 min before siren |
| Official Pikud Haoref API (`oref.org.il`) | Confirmed rocket alerts by city | Real-time, at siren |

## Repository Layout

```
server.py                  — Entire backend (FastAPI + scraping + parsing)
index.html                 — Entire frontend (single-file, vanilla JS + Leaflet map)
regional_coords_final.json — City/region lat-lon lookup (loaded at startup, never edited)
pytest_data.json           — Test fixture: real messages from all 4 channels
real_messages.json         — Reference sample messages (not loaded by tests)
generate_test_data.py      — Regenerates pytest_data.json from live channel scraping
requirements.txt           — fastapi, uvicorn, httpx, python-dateutil
render.yaml                — Render.com deployment config
```

No database. All state is in-memory, reset on restart.

## The 4 Telegram Channels

All scraped via public `t.me/s/` pages — no API key or auth required.

| Key | URL slug | Hebrew name | Typical message style |
|-----|----------|-------------|----------------------|
| `shigurimsh` | `t.me/s/shigurimsh` | שיגורים מהשנייה | Short bursts: area msg, then separate timing msg |
| `alert_Real_Time` | `t.me/s/alert_Real_Time` | Alert Real Time | Combined: "שיגור מאיראן לצפון, עוד 6 דקות אזעקה" |
| `beforeredalert` | `t.me/s/beforeredalert` | Before Red Alert | Split: area first, then bare timing ("6 דקות") |
| `Yemennews7071` | `t.me/s/Yemennews7071` | Yemen and Iran news | Forecasts from Yemen/Iran: "זוהו שיגורים מתימן לדרום, צפי הגעה 8 דקות" |

Each channel sends messages in bursts during an event. A typical event sequence:
1. Launch detected + direction ("שיגורים לצפון")
2. Time forecast ("צפי 17:52 מגיע" or "עוד 5 דקות")
3. Alerts firing confirmation ("כעת אזעקות")
4. Interception result ("יירוטים מוצלחים ✅")

## server.py Architecture

### Startup sequence (`startup_event`)
1. `fetch_israel_cities()` — fetches ~2000 Israeli settlements from data.gov.il, appends to `KNOWN_AREAS`
2. `debug_load_messages()` — in DEBUG mode, loads `pytest_data.json` to pre-populate state
3. `telegram_polling_loop()` — background task, scrapes all 4 channels
4. `oref_polling_loop()` — background task, polls Pikud Haoref history API every 3s

### Telegram scraping pipeline

```
scrape_telegram_channel()        — HTTP GET/POST t.me/s/ pages, returns list of {text, date, id, channel, msg_dt}
    |
    v
TelegramPageParser (HTMLParser)  — Parses tgme_widget_message_wrap divs, extracts text + datetime
    |
    v
process_forecast_messages()      — Core processing: dedup, parse, track state, build latest_event
    |
    v
extract_forecast_data(text)      — Returns {alerts: [{areas, clock_time, expected_time_text, expected_seconds}]}
```

### Key parsing functions

| Function | Input | Output |
|----------|-------|--------|
| `extract_forecast_data(text)` | Raw message text | `{alerts: [...], raw_text, clean_text, tight_polygon?}` |
| `extract_areas_from_text(text)` | Raw message text | List of area strings |
| `extract_expected_time_text(text)` | Raw message text | String like `"5 דקות"` or `"75 שניות"` |
| `_to_expected_seconds(text)` | `"5 דקות"` / `"75 שניות"` | Integer seconds (300 / 75) |
| `get_target_datetime(time_str, reference_time)` | `"17:52"`, optional ref datetime | Absolute datetime |
| `compute_tight_polygon(place_names)` | List of city names | Buffered convex hull polygon or None |
| `clean_forecast_text(text)` | Raw text | Text with URLs and extra whitespace removed |

### Area detection logic

Hebrew area names come in two forms:

1. **Broad regions** (`KNOWN_AREAS` list): מרכז, צפון, דרום, שרון, שפלה, גוש דן, עוטף עזה, etc. — matched with regex allowing Hebrew prefixes (ב/ל/מ/ה/ו).

2. **Specific cities** (loaded from data.gov.il + hardcoded in `CITY_COORDS_LOOKUP`): matched for tight polygon computation when message contains "בדקות".

`TACTICAL_REGION_MAPPING` maps specific cities to broad regions for deduplication (e.g. "חיפה" → "צפון").

### Cross-message state tracking

Channels often split time and area across consecutive messages. Two mechanisms handle this:

- **`channel_last_areas`**: Per-channel, stores the last known {areas, timing, msg_dt}. A timing-only message within 15 min inherits areas from previous; an area-only message within 20 min inherits timing.
- **`pending_forecast_parts`**: Per-channel {time, areas} pair. If both arrive within `PENDING_COMBINE_WINDOW_MINUTES` (5 min), they are merged into one combined forecast.

### Global state (all in-memory, reset on restart)

| Variable | Type | Purpose |
|----------|------|---------|
| `latest_event` | dict | Current live alert served to `/api/latest` |
| `active_alerts_by_area` | dict[area → info] | One entry per active area; cleaned up 15 min after target time |
| `alert_history` | list (max 50) | Deduped history of alert events |
| `today_forecasts` | list | All forecast entries for stats |
| `today_messages` | list | All raw messages received today |
| `channel_last_areas` | dict[channel → info] | Last known areas+timing per channel |
| `pending_forecast_parts` | dict[channel → {time, areas}] | Partial message accumulator |
| `telegram_last_seen_ids` | dict[channel → set] | Dedup tracker, prevents reprocessing |
| `active_oref_alerts` | list | Official alerts within last 5 min |

### API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/latest?mock=true&tactical=<area>` | Current forecast event. Mock mode returns test alert. |
| `GET /api/history` | Last 50 deduped alert history entries |
| `GET /api/oref-alerts?mock=true&oref=<cities>` | Official Pikud Haoref alerts. Mock accepts comma-separated cities. |
| `GET /` | Serves `index.html` |
| `GET /regional_coords_final.json` | City coordinate data for frontend map |

## Testing

```bash
pytest test_server.py          # run all tests
pytest test_server.py -v       # verbose
pytest test_server.py -k unit  # only unit tests (fast, no async)
```

Test fixture: `pytest_data.json` — 120+ real messages from all 4 channels with ISO 8601 dates.

Each message in the fixture has: `text`, `date` (ISO 8601 +02:00), `id` (string), `channel`.

`msg_dt` is derived at load time via `datetime.fromisoformat(msg["date"])` — it is NOT stored in the fixture.

### Patching datetime in tests

`server.py` imports `datetime` at module level (`from datetime import datetime`), so `server.datetime` is the class itself. Patch it with:

```python
class MockDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return mock_now

monkeypatch.setattr(server, "datetime", MockDatetime)
```

This is required in any test that calls `process_forecast_messages()` because the function uses `datetime.now(local_tz)` to compute the 24h cutoff window.

### State reset between tests

Any test that calls `process_forecast_messages()` must reset all these globals first:

```python
server.today_forecasts.clear()
server.today_messages.clear()
server.alert_history.clear()
server.active_alerts_by_area.clear()
server.channel_last_areas.clear()
server.pending_forecast_parts.clear()
for key in server.telegram_last_seen_ids:
    server.telegram_last_seen_ids[key].clear()
server.latest_event = {"text": "ממתין לעדכונים...", "target_time": None, "has_data": False}
```

## Running Locally

```bash
pip install -r requirements.txt
python server.py               # starts on port 8000 (kills existing process first)
# or
uvicorn server:app --reload --port 8000
```

With `pytest_data.json` present, `debug_load_messages()` pre-populates state on startup so the UI shows something immediately.

## Deployment

Render.com via `render.yaml`. `PORT` is set by environment variable (defaults to 8000).

## What NOT to Edit

- `regional_coords_final.json` — large coordinate dataset, generated by offline scripts
- `pytest_data.json` — test fixture, only update if adding new channel coverage or refreshing from live data via `generate_test_data.py`
