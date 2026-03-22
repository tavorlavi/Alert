# Test Consolidation & Cleanup Design

Date: 2026-03-23

## Goal

Consolidate the fragmented test suite, add complete Yemennews7071 test coverage, and delete all dead utility/scratch files.

## Current Problems

1. `pytest_data.json` and `real_messages.json` are missing the 4th channel (`Yemennews7071`).
2. Seven test-like files exist but five of them (`test_algo.py`, `test_algo2.py`, `test_algo3.py`, `test_api.py`, `test_stats.py`) are scratch scripts with `print()` — not real tests.
3. Real tests are split across `test_server.py` (pytest/async), `test_detection.py` (unittest), and `test_edge_cases.py` (pytest, 1 test).
4. Ten one-off utility scripts remain from old Telethon-era development and are no longer used by the server.

## Design

### 1. Add Yemennews7071 messages to `pytest_data.json`

Append 9 messages in chronological order. All fields required: `text`, `date` (ISO 8601 with +02:00 offset), `id` (unique string), `channel` (`"Yemennews7071"`). Dates start ~30 minutes after the latest existing message in the file, spaced 1-3 minutes apart.

| id | Text | Purpose |
|----|------|---------|
| Y1 | `"זוהו שיגורים מתימן לדרום, צפי הגעה 8 דקות"` | Launch detected with forecast |
| Y2 | `"גם לאזור אילת"` | Area expansion (inherits timing from Y1) |
| Y3 | `"עוד 5 דקות"` | Timing update |
| Y4 | `"עוד 3 דקות"` | Timing update |
| Y5 | `"כעת אזעקות בדרום"` | Alerts firing |
| Y6 | `"יורטו בהצלחה ✅"` | Interception (no forecast relevance) |
| Y7 | `"שיגור נוסף מתימן למרכז, 7 דקות לאזעקה"` | Second launch to center |
| Y8 | `"כעת אזעקות"` | Alerts firing |
| Y9 | `"יירוטים מוצלחים ✅"` | Interception |

### 2. Rewrite `test_server.py`

Single pytest file. No unittest.TestCase.

#### Global state reset

Every test that calls `process_forecast_messages` must reset ALL of these server globals before running:

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

Implement this as an `autouse` fixture scoped to `function`.

#### Datetime patching

Use `monkeypatch.setattr(server, "datetime", MockDatetime)` — not the hand-rolled `server.datetime = original_now` restore pattern from the old code. Define `MockDatetime` as a subclass of `datetime` that overrides `classmethod now`. Restore is automatic via monkeypatch.

#### Test structure

```
Fixtures:
  reset_state (autouse, function scope) — resets all server globals above
  mock_now(monkeypatch) — patches server.datetime; takes a datetime arg

Unit tests (no async, no state mutation, no fixtures needed):
  test_to_expected_seconds         — _to_expected_seconds: "5 דקות"->300, "75 שניות"->75,
                                     "4.5 דקות"->270, "דקה"->60, "שניה"->1
  test_extract_expected_time_text  — extract_expected_time_text: same inputs, checks string form
  test_extract_areas_basic         — extract_areas_from_text: "למרכז", "לצפון ודרום"
  test_extract_areas_prefixes      — ל/ב/מ/ה prefix stripping: "בירושלים"->"ירושלים"
  test_extract_forecast_data       — full parse of multi-line text with time + areas + seconds
  test_convex_hull_polygon         — compute_tight_polygon with known city names returns polygon

Integration tests (async via asyncio.run, use reset_state fixture):
  test_shigurimsh_launch_sequence  — "שיגורים לצפון" then "צפי 5 דקות מגיע" -> alert_history populated
  test_alert_real_time_with_timing — "שיגור מאיראן לצפון, עוד 6 דקות אזעקה" -> 360s forecast
  test_beforeredalert_split_msgs   — area msg then time msg in separate calls -> areas + time merged
  test_yemennews_forecast_sequence — Y1-Y9 sequence -> at least 2 history entries (דרום + מרכז)
  test_timing_inheritance          — area-only msg within 15 min of timed msg inherits timing
  test_no_duplicate_history        — same msg_id processed twice (is_init=False) stays once
  test_full_dataset_all_channels   — loads pytest_data.json, processes all 4 channels,
                                     asserts alert_history > 0 and all 4 channels present in data
```

#### Dropped test

The `E2EForecastTests::test_fetch_and_process_real_telegram_messages` (from `test_detection.py`) that loaded `real_messages.json` and skipped-if-absent is dropped. Its coverage is superseded by `test_full_dataset_all_channels` which uses `pytest_data.json` unconditionally.

### 3. Files to delete

**Scratch scripts (not tests):**
- `test_algo.py`
- `test_algo2.py`
- `test_algo3.py`
- `test_api.py`
- `test_stats.py`

**Merged into `test_server.py` (delete after merge):**
- `test_detection.py`
- `test_edge_cases.py`

**Obsolete utility scripts (Telethon-era, unused by server):**
- `auth_telegram.py`
- `export_session.py`
- `extract_data.py`
- `fetch_cities.py`
- `fetch_github_cities.py`
- `fetch_html_messages.py`
- `fetch_real_messages.py`
- `fetch_wikidata.py`
- `geocode_missing.py`
- `process_geojson.py`

**Kept:**
- `generate_test_data.py` — regenerates `pytest_data.json` from live scraping, still useful
- `real_messages.json` — kept as reference data; no longer loaded by any test

## Success Criteria

- `pytest` passes with 0 failures, 0 errors
- All 4 channels represented in `pytest_data.json`
- Exactly one test file: `test_server.py`
- No scratch/utility scripts in repo root
- No `test_algo*.py`, `test_api.py`, `test_stats.py`, `test_detection.py`, `test_edge_cases.py`
