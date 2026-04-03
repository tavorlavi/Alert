import re
import json
import asyncio
import httpx
from datetime import datetime, timedelta
from dateutil import tz
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
from html.parser import HTMLParser
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

# Load .env file for local development (ignored in production)
env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

# ==========================
# Configuration
# ==========================
PORT = int(os.environ.get("PORT", "8000"))

# Telegram channels to scrape via public t.me/s/ pages (no auth needed)
TELEGRAM_CHANNELS = {
    "shigurimsh": {
        "url": "https://t.me/s/shigurimsh",
        "type": "forecast",  # This channel provides timing forecasts
        "label": "שיגורים מהשנייה"
    },
    "alert_Real_Time": {
        "url": "https://t.me/s/alert_Real_Time",
        "type": "forecast",
        "label": "Alert Real Time"
    },
    "beforeredalert": {
        "url": "https://t.me/s/beforeredalert",
        "type": "forecast",
        "label": "Before Red Alert"
    },
    "Yemennews7071": {
        "url": "https://t.me/s/Yemennews7071",
        "type": "forecast",
        "label": "Yemen and Iran news"
    },
    "aharonyediotnews": {
        "url": "https://t.me/s/aharonyediotnews",
        "type": "forecast",
        "label": "אהרון ידיעות"
    },
    "fekalshigurim": {
        "url": "https://t.me/s/fekalshigurim",
        "type": "forecast",
        "label": "פקל שיגורים"
    }
}
TELEGRAM_POLL_INTERVAL = 5  # seconds between scrapes
# ==========================

local_tz = tz.gettz("Asia/Jerusalem")
app = FastAPI()

# Load city coordinates and region mapping for polygon computation
_geo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regional_coords_final.json")
with open(_geo_file) as _f:
    _geo_data = json.load(_f)
    CITY_COORDS_LOOKUP: dict = _geo_data.get("CITY_COORDS", {})
    _CITY_TO_REGION_RAW: dict = _geo_data.get("CITY_TO_REGION", {})

_REGION_ID_TO_HEBREW = {
    "NORTH": "צפון", "CENTER": "מרכז", "SOUTH": "דרום",
    "JERUSALEM": "ירושלים", "WEST_BANK": "יהודה ושומרון",
}
_CITY_REGION_LOOKUP: dict[str, str] = {}
for _rid, _rcities in _CITY_TO_REGION_RAW.items():
    _heb = _REGION_ID_TO_HEBREW.get(_rid)
    if _heb:
        for _c in _rcities:
            _CITY_REGION_LOOKUP[_c] = _heb

# Allow connections from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Store the latest event so new users see it immediately when they open the site
latest_event = {
    "text": "ממתין לעדכונים...",
    "target_time": None,
    "has_data": False
}

channel_last_areas = {}      # Track last areas mentioned by channel to group updates
active_alerts_by_area = {}   # Track current active alerts per individual area
active_oref_alerts: list = []    # [{id, cities, msg_dt}] from PikudHaOref_all siren messages
active_mivzak: dict = {}         # {area: [cities]} from מבזק proximity clustering
active_mivzak_polygons: dict = {}  # {area: [[lat,lon], ...]} tight polygons from מבזק cities
_mivzak_last_update: datetime | None = None
MIVZAK_TIMEOUT_SECONDS = 600    # 10 minutes
_oref_seen_ids: set = set()      # Dedup for independent oref scraper
_mock_state: dict = {"key": None, "target_time": None}

# Store alert history (last 50 alerts)
alert_history = []
MAX_HISTORY = 50

# ==========================
# Today's data for statistics
# ==========================
today_forecasts = []      # All "צפי" messages from today: [{text, target_time, received_at, raw_text}]
today_messages = []       # ALL messages from the channel today (for display)
MAX_TODAY_MESSAGES = 500
MAX_TODAY_FORECASTS = 300
MAX_SEEN_IDS_PER_CHANNEL = 2000
PENDING_COMBINE_WINDOW_MINUTES = 5  # window to merge separate time/area messages

# Pending partial forecasts per channel when time and areas arrive in separate messages
pending_forecast_parts = {
    ch: {"time": None, "areas": None}
    for ch in TELEGRAM_CHANNELS
}

def extract_time_from_text(text):
    match = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)', text)
    if match:
        return match.group(1)
    return None

def clean_forecast_text(text):
    """Remove URLs and extra whitespace from forecast display text."""
    # Remove Telegram URLs (with or without https:// prefix)
    text = re.sub(r'(?:https?://)?t\.me/\S*', '', text)
    # Remove extra newlines and whitespace  
    text = re.sub(r'\n{2,}', '\n', text).strip()
    return text

_MINUTE_UNITS = ("דקות", "דקה", "דק")
_SECOND_UNITS = ("שניות", "שניה")
_ALL_UNITS = "|".join(_MINUTE_UNITS + _SECOND_UNITS)

# Compiled patterns for duration extraction, ordered most-specific first
_DURATION_PATTERNS = [
    # "6 וחצי דקות" / "2 וחצי שניות"
    re.compile(r'(\d+(?:\.\d+)?)\s*וחצי\s*(' + _ALL_UNITS + ')'),
    # "דקה וחצי" / "שניה וחצי"
    re.compile(r'(דקה|שניה)\s*וחצי'),
    # "חצי דקה" / "חצי שניה"
    re.compile(r'חצי\s*(דקה|שניה)'),
    # "3/4 דקות" or "3-4 דקות" range notation
    re.compile(r'(\d+)\s*[/\-]\s*(\d+)\s*(' + _ALL_UNITS + ')'),
    # Standard: "5 דקות", "5.5 דק", "35 שניות"
    re.compile(r'(\d+(?:\.\d+)?)\s*(' + _ALL_UNITS + ')'),
    # Bare unit: "דקה", "דקות", "שניות"
    re.compile(r'(' + _ALL_UNITS + ')'),
]

# Combined pattern for stripping durations from text (used in area extraction)
DURATION_STRIP_RE = re.compile(
    r'(?:\d+(?:\.\d+)?\s*וחצי\s*(?:' + _ALL_UNITS + '))'
    r'|(?:(?:דקה|שניה)\s*וחצי)'
    r'|(?:חצי\s*(?:דקה|שניה))'
    r'|(?:\d+\s*[/\-]\s*\d+\s*(?:' + _ALL_UNITS + '))'
    r'|(?:(?:\d+(?:\.\d+)?)\s*)?(?:' + _ALL_UNITS + ')'
)

def extract_expected_time_text(text):
    """Extract expected duration expressions like '5 דקות', '35 שניות', '4.5 דק', 'דקה וחצי'."""
    for pat in _DURATION_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None

def _to_expected_seconds(expected_time_text):
    if not expected_time_text:
        return None

    # "X וחצי UNIT"
    m = _DURATION_PATTERNS[0].search(expected_time_text)
    if m:
        value = float(m.group(1)) + 0.5
        unit = m.group(2)
        if unit in _MINUTE_UNITS:
            return int(value * 60)
        return int(value)

    # "דקה וחצי" / "שניה וחצי"
    m = _DURATION_PATTERNS[1].search(expected_time_text)
    if m:
        unit = m.group(1)
        if unit == "דקה":
            return 90
        return 1  # שניה וחצי ≈ 1s (unlikely but safe)

    # "חצי דקה" / "חצי שניה"
    m = _DURATION_PATTERNS[2].search(expected_time_text)
    if m:
        unit = m.group(1)
        if unit == "דקה":
            return 30
        return 0

    # "X/Y UNIT" or "X-Y UNIT" - take the higher number (safety margin)
    m = _DURATION_PATTERNS[3].search(expected_time_text)
    if m:
        value = float(max(int(m.group(1)), int(m.group(2))))
        unit = m.group(3)
        if unit in _MINUTE_UNITS:
            return int(value * 60)
        return int(value)

    # Standard "X UNIT"
    m = _DURATION_PATTERNS[4].search(expected_time_text)
    if m:
        value = float(m.group(1))
        unit = m.group(2)
        if unit in _MINUTE_UNITS:
            return int(value * 60)
        return int(value)

    # Bare unit "דקה" / "שניה"
    m = _DURATION_PATTERNS[5].search(expected_time_text)
    if m:
        unit = m.group(1)
        if unit in _MINUTE_UNITS:
            return 60
        return 1

    return None

KNOWN_AREAS = [
    "מרכז", "צפון", "דרום", "ירושלים", "אילת", "עוטף עזה", "שרון", "שפלה", "גוש דן",
    "יהודה", "הגליל", "גליל", "הגולן", "גולן", "קריות", "עמק יזרעאל",
    "ים המלח", "הערבה", "מפרץ", "בקעה", "המדבר", "גליל עליון",
    "גליל תחתון", "גליל מערבי", "עוטף", "מירון", "כיש", "שומרון",
    "לכיש", "נגב", "מערב הנגב", "מרכז הנגב",
    "העמקים", "עמקים", "כינרת", "מישור החוף",
]

TACTICAL_REGION_MAPPING = {
    "באר שבע": "דרום", "דימונה": "דרום", "אשדוד": "דרום", "אשקלון": "דרום",
    "נתיבות": "דרום", "שדרות": "דרום", "אופקים": "דרום", "ערד": "דרום",
    "תל אביב": "מרכז", "ראשון לציון": "מרכז", "חולון": "מרכז", "רמת גן": "מרכז",
    "פתח תקווה": "מרכז", "הרצליה": "מרכז", "נתניה": "מרכז", "כפר סבא": "מרכז",
    "מודיעין": "מרכז", "יפו": "מרכז", "זכרון": "מרכז",
    "בית שמש": "ירושלים",
    "חיפה": "צפון", "עכו": "צפון", "נהריה": "צפון", "טבריה": "צפון", "צפת": "צפון",
    "כרמיאל": "צפון", "ראש פינה": "צפון", "קרית שמונה": "צפון", "חצור": "צפון",
    "בית שאן": "צפון", "יוקנעם": "צפון", "מעלות": "צפון",
}

AREA_NORMALIZATION = {
    "הגליל": "צפון", "גליל": "צפון", "הגולן": "צפון", "גולן": "צפון",
    "קריות": "צפון", "עמק יזרעאל": "צפון",
    "מפרץ": "צפון", "גליל עליון": "צפון", "גליל תחתון": "צפון",
    "גליל מערבי": "צפון", "מירון": "צפון", "כיש": "צפון", "בקעה": "צפון",
    "העמקים": "צפון", "עמקים": "צפון", "כינרת": "צפון",
    "שרון": "מרכז", "שפלה": "מרכז", "גוש דן": "מרכז", "יהודה": "מרכז", "שומרון": "מרכז",
    "מישור החוף": "מרכז",
    "עוטף עזה": "דרום", "עוטף": "דרום", "ים המלח": "דרום", "הערבה": "דרום", "המדבר": "דרום",
    "לכיש": "דרום", "נגב": "דרום", "מערב הנגב": "דרום", "מרכז הנגב": "דרום",
}

EXCLUDE_WORDS = {
    "שיגור", "שיגורים", "כעת", "אזעקות", "אזעקה", "יירוטים", "חזלש",
    "מלבנון", "מאיראן", "מעזה", "מתימן", "מעיראק", "מגיע", "זוהו", "זוהה",
    "שני", "שנייה", "נוסף", "נוספים", "נוספות", "חדש",
    "להתמגן", "התמגן", "תתמגנו", "השמרו",
    "מצרר", "מצררים",
    "נפילה", "נפילות", "נפתח", "נפתחים",
    "טיל", "טילי", "טילים",
    "לערך", "בערך", "ערך",
    "הגעה", "יציאות", "יציאה", "יופעלו", "שתפו",
    "כמובן", "שוב", "מספר", "ניהם", "זיהוי", "מאוחר",
    "תותח", "כולל", "מתרחב", "מיירט",
}

HEBREW_ABBREVIATIONS = {
    'ב"ש': "באר שבע", 'ב״ש': "באר שבע",
    'פ"ת': "פתח תקווה", 'פ״ת': "פתח תקווה",
    'ת"א': "תל אביב", 'ת״א': "תל אביב",
    'ר"ג': "רמת גן", 'ר״ג': "רמת גן",
    'ק"ש': "קרית שמונה", 'ק״ש': "קרית שמונה",
    'ר"ל': "ראשון לציון", 'ר״ל': "ראשון לציון",
}

# Inverted: region → list of specific cities (for oref mock expansion)
_REGION_TO_CITIES: dict[str, list[str]] = {}
for _city, _region in TACTICAL_REGION_MAPPING.items():
    _REGION_TO_CITIES.setdefault(_region, []).append(_city)

OREF_URL = "https://t.me/s/PikudHaOref_all"
OREF_POLL_INTERVAL = 3  # seconds

MOCK_OREF_MESSAGES = [
    {
        "text": (
            "🚨 מבזק\n"
            "בדקות הקרובות צפויות להתקבל התרעות באזורך\n"
            "אזור תל אביב\n"
            "תל אביב, רמת גן, חולון, ראשון לציון, פתח תקווה, הרצליה\n"
            "אזור מרכז הנגב\n"
            "באר שבע, דימונה, אשדוד, אשקלון, נתיבות, שדרות, אופקים\n"
            "אזור שרון\n"
            "נתניה, כפר סבא\n"
            "אזור חיפה\n"
            "חיפה, עכו, נהריה, טבריה, צפת, כרמיאל, קרית שמונה"
        ),
        "id": "mock_mivzak_1",
    },
    {
        "text": (
            "🚨 ירי רקטות וטילים\n"
            "אזור תל אביב\n"
            "תל אביב, רמת גן, חולון (דקה וחצי)\n"
            "ראשון לציון, פתח תקווה, הרצליה (דקה)\n"
            "אזור מרכז הנגב\n"
            "באר שבע, דימונה, אשדוד (דקה וחצי)\n"
            "אשקלון, נתיבות, שדרות, אופקים (דקה)\n"
            "אזור שרון\n"
            "נתניה, כפר סבא (דקה וחצי)\n"
            "אזור חיפה\n"
            "חיפה, עכו, נהריה (דקה וחצי)\n"
            "טבריה, צפת, כרמיאל, קרית שמונה (דקה)\n"
            "היכנסו למרחב המוגן."
        ),
        "id": "mock_siren_1",
    },
]


def extract_specific_places_from_text(text):
    """Extract city names from message text that are present in CITY_COORDS_LOOKUP."""
    places = []
    seen = set()
    for line in text.split('\n'):
        for part in re.split(r'[,،/|]', line):
            cleaned = re.sub(r'^[בלמהו]+', '', part.strip()).strip()
            if not cleaned or len(cleaned) < 2:
                continue
            for candidate in [cleaned, 'ה' + cleaned]:
                if candidate in CITY_COORDS_LOOKUP and candidate not in seen:
                    seen.add(candidate)
                    places.append(candidate)
                    break
    return places


def _convex_hull(points):
    """Andrew's monotone chain convex hull algorithm."""
    pts = sorted(set(map(tuple, points)))
    if len(pts) <= 2:
        return [[p[0], p[1]] for p in pts]

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return [[p[0], p[1]] for p in lower[:-1] + upper[:-1]]


def _buffer_polygon(hull, buf=0.08):
    """Expand hull vertices outward from centroid by buf degrees (~8 km)."""
    if len(hull) < 3:
        lats = [p[0] for p in hull]
        lons = [p[1] for p in hull]
        clat = sum(lats) / len(lats)
        clon = sum(lons) / len(lons)
        return [
            [clat + buf, clon - buf], [clat + buf, clon + buf],
            [clat - buf, clon + buf], [clat - buf, clon - buf],
        ]
    cx = sum(p[0] for p in hull) / len(hull)
    cy = sum(p[1] for p in hull) / len(hull)
    result = []
    for lat, lon in hull:
        dlat, dlon = lat - cx, lon - cy
        dist = (dlat ** 2 + dlon ** 2) ** 0.5
        if dist > 0:
            result.append([round(lat + dlat / dist * buf, 6), round(lon + dlon / dist * buf, 6)])
        else:
            result.append([round(lat + buf, 6), round(lon, 6)])
    return result


def compute_tight_polygon(place_names, buf=0.03):
    """Return a buffered convex hull polygon for the given city names, or None."""
    coords = [tuple(CITY_COORDS_LOOKUP[n]) for n in place_names if n in CITY_COORDS_LOOKUP]
    if not coords:
        return None
    return _buffer_polygon(_convex_hull(coords), buf=buf)


def _smooth_polygon(points, segments_per_edge=8):
    """Round polygon corners into smooth curves.

    For each triplet of consecutive vertices (A, B, C), replace the corner at B
    with an arc. The arc starts ~30% of the way from B toward A, curves through
    a control point near B, and ends ~30% from B toward C.
    """
    n = len(points)
    if n < 3:
        return points
    result = []
    for i in range(n):
        a = points[(i - 1) % n]
        b = points[i]
        c = points[(i + 1) % n]
        # Pull control point inward so arcs don't overshoot
        r = 0.3
        start = [b[0] + r * (a[0] - b[0]), b[1] + r * (a[1] - b[1])]
        end = [b[0] + r * (c[0] - b[0]), b[1] + r * (c[1] - b[1])]
        for j in range(segments_per_edge):
            t = j / segments_per_edge
            # Quadratic bezier: start -> b (control) -> end
            s = 1 - t
            lat = s * s * start[0] + 2 * s * t * b[0] + t * t * end[0]
            lon = s * s * start[1] + 2 * s * t * b[1] + t * t * end[1]
            result.append([round(lat, 6), round(lon, 6)])
    return result


def compute_smooth_polygon(place_names, buf=0.04):
    """Like compute_tight_polygon but with rounded corners and tighter buffer."""
    coords = [tuple(c) for n in place_names if (c := _resolve_city_coords(n))]
    if not coords:
        return None
    hull = _convex_hull(coords)
    buffered = _buffer_polygon(hull, buf=buf)
    return _smooth_polygon(buffered)


def _cluster_by_proximity(city_coords: list[tuple[str, float, float]],
                          max_dist: float = 0.25) -> list[list[str]]:
    """Single-linkage clustering of cities by geographic distance.

    max_dist: ~0.25 degrees ≈ 28km at Israel's latitude.
    Returns list of city-name groups.
    """
    n = len(city_coords)
    if n == 0:
        return []
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        pa, pb = find(a), find(b)
        if pa != pb:
            parent[pa] = pb

    for i in range(n):
        for j in range(i + 1, n):
            _, lat1, lon1 = city_coords[i]
            _, lat2, lon2 = city_coords[j]
            if ((lat1 - lat2) ** 2 + (lon1 - lon2) ** 2) ** 0.5 <= max_dist:
                union(i, j)

    groups: dict[int, list[str]] = {}
    for i, (name, _, _) in enumerate(city_coords):
        groups.setdefault(find(i), []).append(name)
    return list(groups.values())


def clean_hebrew_city(city_name):
    """Clean government city DB noise (like parentheses or double spaces)"""
    name = re.sub(r'\(.*?\)', '', city_name)
    # Replace hyphens with spaces for easier matching
    name = name.replace('-', ' ')
    name = re.sub(r'[^\w\sא-ת]', '', name)
    return ' '.join(name.split())

def extract_areas_from_text(text):
    """Extract area-like phrases from text (e.g., מרכז, אילת, מאשדוד עד נתניה)."""
    areas = []
    seen = set()
    
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if any(skip in line for skip in [
            "http://", "https://", "t.me/", "היכנסו", "פיקוד העורף", "ירי רקטות", "חדירת כלי", "חדירת מחבלים", "ללא התרעה", "לא יופעלו", "מערכות ההגנה", "ערוץ", "בלבד", "בדרכם", "יורטו", "חריג", "פרטים"
        ]):
            continue

        # Skip lines that are clearly purely metadata lines
        if line.startswith("צפי") and len(line) < 15:
            continue

        # Expand Hebrew abbreviations before punctuation strip removes quotes
        for abbr, full in HEBREW_ABBREVIATIONS.items():
            if abbr in line:
                line = line.replace(abbr, full)

        # Strip exact time formats and time units so they don't become areas
        line = re.sub(r'\d{1,2}:\d{2}(?::\d{2})?', '', line)
        line = DURATION_STRIP_RE.sub('', line)
        line = re.sub(r'צפי|משך|עוד|חצי|וחצי|לאזעקה|לאזעקות|כעת|כרגע|לכרגע|מיקוד|מרחב|גזרת|איזור|ממוקד(?:ת)?', '', line)
        line = re.sub(r'[^\w\s\u05d0-\u05ea,/|\-]', '', line)  # strip emojis

        for part in re.split(r'[,/|\-\n]', line):
            part = re.sub(r'\(.*?\)', '', part).strip()

            for city, region in TACTICAL_REGION_MAPPING.items():
                if city in part:
                    part = part.replace(city, region)
            
            # Extract recognized predefined areas
            found_known = False
            for ka in sorted(KNOWN_AREAS, key=len, reverse=True):
                # Allow standard Hebrew prefixes on regions
                pattern = r'(?<![א-ת])(?:ו?[בלמה]?)(?:' + ka.replace(' ', r'\s+') + r')(?![א-ת])'
                if re.search(pattern, part):
                    if ka not in seen:
                        seen.add(ka)
                        areas.append(ka)
                    found_known = True
                    # Remove matched portion to allow other separate areas to match
                    part = re.sub(pattern, ' ', part)

            if not found_known:
                # Remove excluded words
                words = [w for w in part.split() if w not in EXCLUDE_WORDS and w != "ו"]
                cleaned_area = " ".join(words).strip()
                cleaned_area = re.sub(r'^(לכיוון\s|אל\s|כיוון\s|אזור\s|באזור\s|איזור\s|מיקוד\s|מרחב\s|גזרת\s|גם\sל|גם\sב|גם\s|ל|ב)', '', cleaned_area).strip()
                
                # Short generic words aren't areas usually
                if not cleaned_area or len(cleaned_area) < 2 or len(cleaned_area.split()) > 3:
                    continue
                    
                if cleaned_area in seen:
                    continue
                seen.add(cleaned_area)
                areas.append(cleaned_area)
            
    return areas

def extract_forecast_data(text):
    """Unify and extract forecast data from a message, returning a list of alerts.
    Each alert is: {"areas": [...], "clock_time": str, "expected_time_text": str, "expected_seconds": int}
    """
    alerts = []

    lines = re.split(r'\n|\.\s+', text)
    
    global_clock_time = None
    global_expected_text = None
    
    for line in lines:
        line = line.strip()
        if not line: continue
            
        if any(skip in line.lower() for skip in [
            "http://", "https://", "t.me/", "היכנסו", "פיקוד העורף", "ירי רקטות", "חדירת כלי", "חדירת מחבלים", "ללא התרעה", "לא יופעלו", "מערכות ההגנה", "ערוץ", "בלבד", "בדרכם", "יורטו", "חריג", "פרטים",
            "מבצע", "טלויזיה", "מומלץ", "לחץ כאן", "tv", "מגשימים", "חבורה", "פיצוצים", "נפילה", "קולות", "הדף", "שנה של", "ערבות", "ארוך טווח"
        ]):
            continue

        clock_m = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)', line)
        line_clock_time = clock_m.group(1) if clock_m else None

        line_expected_text = extract_expected_time_text(line)

        if line_clock_time: global_clock_time = line_clock_time
        if line_expected_text: global_expected_text = line_expected_text

        line_clean = line
        # Expand Hebrew abbreviations before punctuation strip removes quotes
        for abbr, full in HEBREW_ABBREVIATIONS.items():
            if abbr in line_clean:
                line_clean = line_clean.replace(abbr, full)
        line_clean = re.sub(r'\d{1,2}:\d{2}(?::\d{2})?', '', line_clean)
        line_clean = DURATION_STRIP_RE.sub('', line_clean)
        line_clean = re.sub(r'צפי|משך|עוד|חצי|וחצי|לאזעקה|לאזעקות|מיקוד|מרחב|גזרת|איזור|ממוקד(?:ת)?', '', line_clean)
        line_clean = re.sub(r'[^\w\s\u05d0-\u05ea,/|\-]', '', line_clean)  # strip emojis
        
        line_areas = []
        for part in re.split(r'[,/|\-\n]', line_clean):
            part = re.sub(r'\(.*?\)', '', part).strip()
            
            for city, region in TACTICAL_REGION_MAPPING.items():
                if city in part:
                    part = part.replace(city, region)
            
            # Check against KNOWN_AREAS first
            found_known = False
            for ka in sorted(KNOWN_AREAS, key=len, reverse=True):
                # Match as a whole word (allowing Hebrew prefixes like 'ב','ל','מ','ה' and 'ו')
                pattern = r'(?<![א-ת])(?:ו?[בלמה]?)(?:' + ka.replace(' ', r'\s+') + r')(?![א-ת])'
                if re.search(pattern, part):
                    if ka not in line_areas:
                        line_areas.append(ka)
                    found_known = True
                    # Blank out matched string
                    part = re.sub(pattern, ' ', part)
                        
        if line_areas:
            alerts.append({
                "areas": line_areas,
                "clock_time": line_clock_time,
                "expected_time_text": line_expected_text,
                "expected_seconds": _to_expected_seconds(line_expected_text)
            })
            
    if not alerts and (global_clock_time or global_expected_text):
        alerts.append({
            "areas": [],
            "clock_time": global_clock_time,
            "expected_time_text": global_expected_text,
            "expected_seconds": _to_expected_seconds(global_expected_text)
        })
        
    # Backfill missing times using global discovered times
    for a in alerts:
        if not a["clock_time"] and global_clock_time:
            a["clock_time"] = global_clock_time
        if not a["expected_time_text"] and global_expected_text:
            a["expected_time_text"] = global_expected_text
            a["expected_seconds"] = _to_expected_seconds(global_expected_text)
        
    result = {
        "raw_text": text,
        "clean_text": clean_forecast_text(text),
        "alerts": alerts,
    }
    if "בדקות" in text:
        places = extract_specific_places_from_text(text)
        poly = compute_tight_polygon(places)
        if poly:
            result["tight_polygon"] = poly
    return result


def _store_pending_part(channel_name, part_type, payload):
    if channel_name not in pending_forecast_parts:
        pending_forecast_parts[channel_name] = {"time": None, "areas": None}
    pending_forecast_parts[channel_name][part_type] = payload


def _maybe_combine_pending(channel_name):
    """Try to combine separate time/area messages within a short window."""
    parts = pending_forecast_parts.get(channel_name, {})
    t_part = parts.get("time")
    a_part = parts.get("areas")
    if not t_part or not a_part:
        return None

    delta = abs((t_part["msg_dt"] - a_part["msg_dt"]).total_seconds())
    if delta > PENDING_COMBINE_WINDOW_MINUTES * 60:
        # Drop the older part to allow fresher pairing
        if t_part["msg_dt"] < a_part["msg_dt"]:
            parts["time"] = None
        else:
            parts["areas"] = None
        return None

    combined_text = f"{t_part['text']}\n{a_part['text']}".strip()
    combined = {
        "text": combined_text,
        "msg_dt": max(t_part["msg_dt"], a_part["msg_dt"]),
        "id": f"{t_part.get('id','')}_{a_part.get('id','')}" or None,
        "clock_time": t_part["clock_time"],
        "expected_time_text": t_part.get("expected_time_text"),
        "expected_seconds": t_part.get("expected_seconds"),
        "areas": a_part["areas"],
    }

    # Clear after successful combine
    parts["time"] = None
    parts["areas"] = None
    return combined

def get_target_datetime(target_time_str, reference_time=None):
    """Convert a time string like '16:12' to a datetime.
    If reference_time is provided, use its date; otherwise use today's date.
    Won't push to next day for historical messages."""
    ref = reference_time or datetime.now(local_tz)
    time_parts = list(map(int, target_time_str.split(":")))
    
    if len(time_parts) == 2:
        hour, minute = time_parts
        second = 0
    else:
        hour, minute, second = time_parts

    target_time = ref.replace(hour=hour, minute=minute, second=second, microsecond=0)

    # Only push to next day for live messages (no reference_time), not historical
    if reference_time is None and target_time < ref:
        target_time += timedelta(days=1)

    return target_time


# ==========================
# Pikud Haoref (PikudHaOref_all Telegram channel)
# ==========================

def parse_oref_siren_cities(text: str) -> list[str] | None:
    """Extract city names from a PikudHaOref_all active siren message.

    Siren messages contain 'ירי רקטות וטילים'. Cities are listed under
    region headers, with optional timing suffixes like (30 שניות).
    Returns flat list of city names, or None if not a siren message.
    """
    if "ירי רקטות וטילים" not in text:
        return None
    cities = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("\U0001f6a8") or line.startswith("אזור ") or "היכנסו למרחב" in line:
            continue
        line = re.sub(r'\s*\([^)]+\)\s*$', '', line).strip()
        if line:
            cities.extend(c.strip() for c in line.split(',') if c.strip())
    return cities if cities else None


def parse_oref_mivzak(text: str) -> list[str] | None:
    """Extract city names from a PikudHaOref_all מבזק early-warning message.

    מבזק messages contain both 'מבזק' and 'בדקות הקרובות'. Cities are listed
    under region headers (אזור X). We ignore the region grouping and return
    a flat list of all cities mentioned.
    Returns list of city names, or None if not a מבזק message.
    """
    if "מבזק" not in text or "בדקות הקרובות" not in text:
        return None
    cities = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if (line.startswith("\U0001f6a8")
                or (line.startswith("אזור ") and ',' not in line)
                or "בדקות הקרובות" in line or "על תושבי" in line
                or "במקרה של" in line or "היכנסו למרחב" in line):
            continue
        cities.extend(c.strip() for c in line.split(',') if c.strip())
    return cities if cities else None


def _resolve_city_coords(city: str):
    """Look up city coordinates, trying exact match then stripping suffix."""
    if city in CITY_COORDS_LOOKUP:
        return CITY_COORDS_LOOKUP[city]
    base = city.split(" - ")[0].strip()
    if base in CITY_COORDS_LOOKUP:
        return CITY_COORDS_LOOKUP[base]
    return None


def _region_from_lat(lat: float) -> str:
    if lat >= 32.5:
        return "צפון"
    if lat >= 31.3:
        return "מרכז"
    return "דרום"


def build_mivzak_replacements(cities: list[str]) -> tuple[dict[str, list[str]], dict[str, list]]:
    """Build {area: [cities]} and {area: polygon} from מבזק city list.

    Clusters cities by geographic proximity (~30km threshold) and computes
    a tight polygon per cluster. Each cluster is labeled by the majority
    region of its cities (for frontend handoff to forecast areas).
    """
    city_coords = []
    for city in cities:
        coords = _resolve_city_coords(city)
        if coords:
            lat = coords[0] if isinstance(coords, (list, tuple)) else coords
            lon = coords[1] if isinstance(coords, (list, tuple)) else 0
            city_coords.append((city, lat, lon))

    if not city_coords:
        return {}, {}

    clusters = _cluster_by_proximity(city_coords)

    replacements: dict[str, list[str]] = {}
    polygons: dict[str, list] = {}

    for cluster_cities in clusters:
        region_votes: dict[str, int] = {}
        for city in cluster_cities:
            region = (_CITY_REGION_LOOKUP.get(city)
                      or TACTICAL_REGION_MAPPING.get(city))
            if region:
                region_votes[region] = region_votes.get(region, 0) + 1

        if region_votes:
            area_name = max(region_votes, key=region_votes.get)
        else:
            coords = _resolve_city_coords(cluster_cities[0])
            area_name = _region_from_lat(coords[0]) if coords else "אזור לא ידוע"

        replacements[area_name] = cluster_cities
        poly = compute_smooth_polygon(cluster_cities)
        if poly:
            polygons[area_name] = poly

    return replacements, polygons


def merge_mivzak(replacements: dict[str, list[str]]):
    """Merge mivzak data into active state, accumulating cities across messages."""
    global _mivzak_last_update
    for region, cities in replacements.items():
        existing = active_mivzak.get(region, [])
        active_mivzak[region] = list(dict.fromkeys(existing + cities))
    # Recompute polygons with merged city lists
    for region, cities in active_mivzak.items():
        poly = compute_smooth_polygon(cities)
        if poly:
            active_mivzak_polygons[region] = poly
    _mivzak_last_update = datetime.now(local_tz)


# ==========================
# Telegram Scraping (no auth needed - uses public t.me/s/ pages)
# ==========================
class TelegramPageParser(HTMLParser):
    """Parse messages from t.me/s/ public channel preview pages."""
    def __init__(self):
        super().__init__()
        self.messages = []
        self._in_message = False
        self._in_text = False
        self._in_time = False
        self._current_msg = {}
        self._current_text_parts = []
        self._msg_id = None
        self._div_depth = 0       # Track nested div depth inside a message
        self._text_div_depth = 0   # Track div depth when text started
    
    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = attrs_dict.get("class", "")
        
        if tag == "div" and "tgme_widget_message_wrap" in classes:
            self._in_message = True
            self._current_msg = {}
            self._current_text_parts = []
            self._msg_id = None
            self._div_depth = 0
        
        if self._in_message:
            if tag == "div":
                self._div_depth += 1
            
            if tag == "div" and "tgme_widget_message " in (classes + " "):
                data_post = attrs_dict.get("data-post", "")
                if "/" in data_post:
                    self._msg_id = data_post.split("/")[-1]
            
            if tag == "div" and "tgme_widget_message_text" in classes:
                self._in_text = True
                self._current_text_parts = []
                self._text_div_depth = self._div_depth
            
            if tag == "time" and "datetime" in attrs_dict:
                self._current_msg["datetime"] = attrs_dict["datetime"]
            
            if tag == "br" and self._in_text:
                self._current_text_parts.append("\n")
    
    def handle_data(self, data):
        if self._in_text:
            self._current_text_parts.append(data)
    
    def handle_endtag(self, tag):
        if not self._in_message:
            return
            
        if tag == "div":
            # Close the text region when we return to the div depth where text started
            if self._in_text and self._div_depth == self._text_div_depth:
                self._in_text = False
                self._current_msg["text"] = "".join(self._current_text_parts).strip()
            
            self._div_depth -= 1
            
            # When div_depth goes to 0, the outer message_wrap div is closed
            if self._div_depth <= 0 and self._current_msg.get("text"):
                if self._msg_id:
                    self._current_msg["id"] = self._msg_id
                self.messages.append(self._current_msg)
                self._in_message = False
                self._current_msg = {}

# Track last seen message IDs per channel to detect new messages
telegram_last_seen_ids = {ch: set() for ch in TELEGRAM_CHANNELS}
telegram_initialized = {ch: False for ch in TELEGRAM_CHANNELS}

async def scrape_telegram_channel(channel_name, channel_config, max_pages=1, cutoff_dt=None):
    """Scrape latest messages from a public Telegram channel via t.me/s/.
    
    Args:
        max_pages: Number of pages to load. Each page has ~20 messages.
                   Use max_pages>1 during init to load more history.
        cutoff_dt: Stop paginating when messages are older than this datetime.
    """
    url = channel_config["url"]
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }
    
    async with httpx.AsyncClient(verify=False, timeout=15, follow_redirects=True) as http_client:
        try:
            all_results = []
            oldest_msg_id = None
            reached_cutoff = False
            
            for page in range(max_pages):
                if page == 0:
                    # First page: normal GET
                    resp = await http_client.get(url, headers=headers)
                else:
                    # Subsequent pages: POST with before=oldest_msg_id for older messages
                    if not oldest_msg_id:
                        break
                    resp = await http_client.post(
                        url,
                        headers={**headers, "X-Requested-With": "XMLHttpRequest"},
                        data={"before": oldest_msg_id}
                    )
                
                if resp.status_code != 200:
                    break
                
                # POST responses return JSON-encoded HTML string, GET returns raw HTML
                html_content = resp.text
                if page > 0:
                    try:
                        html_content = json.loads(html_content)
                    except (json.JSONDecodeError, TypeError):
                        pass  # Use raw text if not JSON
                
                parser = TelegramPageParser()
                parser.feed(html_content)
                
                if not parser.messages:
                    break
                
                page_results = []
                for msg in parser.messages:
                    text = msg.get("text", "").strip()
                    dt_str = msg.get("datetime", "")
                    msg_id = msg.get("id", "")
                    
                    if not text:
                        continue
                    
                    # Parse datetime
                    try:
                        msg_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                        msg_dt = msg_dt.astimezone(local_tz)
                    except Exception:
                        msg_dt = datetime.now(local_tz)
                    
                    # Stop if message is older than cutoff
                    if cutoff_dt and msg_dt < cutoff_dt:
                        reached_cutoff = True
                        continue
                    
                    page_results.append({
                        "text": text,
                        "date": msg_dt.isoformat(),
                        "id": msg_id,
                        "channel": channel_name,
                        "msg_dt": msg_dt,
                    })
                
                all_results.extend(page_results)
                
                if reached_cutoff:
                    break
                
                # Find the oldest message ID for pagination
                # Message IDs from t.me/s/ are like "123" (numeric)
                page_ids = [r["id"] for r in page_results if r["id"]]
                if page_ids:
                    # Get the numerically smallest ID (oldest message)
                    numeric_ids = []
                    for mid in page_ids:
                        try:
                            numeric_ids.append(int(mid))
                        except (ValueError, TypeError):
                            pass
                    if numeric_ids:
                        oldest_msg_id = str(min(numeric_ids))
                    else:
                        break
                else:
                    break
                
                # Small delay between pagination requests
                if page < max_pages - 1:
                    await asyncio.sleep(0.3)
            
            if max_pages > 1:
                print(f"   Ã°Å¸â€œâ€ž {channel_name}: scraped {page + 1} pages, {len(all_results)} messages" + (" (reached 2h cutoff)" if reached_cutoff else ""))
            
            return all_results
        except Exception as e:
            print(f"Ã¢Å¡Â Ã¯Â¸Â Error scraping {channel_name}: {e}")
            return []


async def process_forecast_messages(messages, channel_name, is_init=False):
    """Process forecast messages from forecast-only channels."""
    global latest_event, today_forecasts, today_messages
    global channel_last_areas, active_alerts_by_area

    now = datetime.now(local_tz)
    cutoff = now - timedelta(hours=24)

    new_msgs = []
    for msg in messages:
        msg_dt = msg["msg_dt"]
        if msg_dt < cutoff:
            continue

        msg_id = msg["id"]
        is_new = msg_id and msg_id not in telegram_last_seen_ids[channel_name]

        if is_init or is_new:
            new_msgs.append(msg)

    if not new_msgs:
        return

    for msg in new_msgs:
        msg_id = msg["id"]
        if msg_id:
            telegram_last_seen_ids[channel_name].add(msg_id)

        text = msg["text"]
        msg_dt = msg["msg_dt"]

        # Add to today's messages
        exists = any(m.get("id") == msg_id for m in today_messages)
        if not exists:
            today_messages.insert(0, {
                "text": text,
                "date": msg_dt.isoformat(),
                "id": msg_id,
            })
            if len(today_messages) > MAX_TODAY_MESSAGES:
                today_messages[MAX_TODAY_MESSAGES:] = []

        extracted = extract_forecast_data(text)
        display_text = extracted.get("clean_text") or clean_forecast_text(text)
        alerts = extracted.get("alerts", [])

        # Gather areas from this message
        msg_areas = []
        for a in alerts:
            msg_areas.extend(a["areas"])
        # deduplicate maintaining order
        msg_areas = list(dict.fromkeys(msg_areas))

        msg_time_info = None
        for a in alerts:
            if a.get("clock_time") or a.get("expected_seconds") is not None:
                msg_time_info = a
                break

        last_info = channel_last_areas.get(channel_name)
        inherited_areas = False
        
        if msg_areas:
            # Inherit TIMING if we have areas but NO timing, and previous message had timing
            if not msg_time_info and last_info:
                time_diff = abs((msg_dt - last_info["msg_dt"]).total_seconds())
                if time_diff <= 15 * 60: # up to 15 min timing inheritance
                    for a in alerts:
                        if not a.get("clock_time") and a.get("expected_seconds") is None:
                            a["clock_time"] = last_info.get("clock_time")
                            a["expected_time_text"] = last_info.get("expected_time_text")
                            a["expected_seconds"] = last_info.get("expected_seconds")

            # Store the updated state
            updated_time_info = msg_time_info if msg_time_info else alerts[0] if alerts else None
            channel_last_areas[channel_name] = {
                "areas": msg_areas,
                "msg_dt": msg_dt,
                "clock_time": updated_time_info.get("clock_time") if updated_time_info else None,
                "expected_time_text": updated_time_info.get("expected_time_text") if updated_time_info else None,
                "expected_seconds": updated_time_info.get("expected_seconds") if updated_time_info else None
            }
        else:
            # Try to inherit areas from the same channel if we have time info but no areas
            if last_info:
                time_diff = abs((msg_dt - last_info["msg_dt"]).total_seconds())
                if time_diff <= 20 * 60: # up to 20 min channel inheritance
                    msg_areas = last_info["areas"]
                    inherited_areas = True
                    # Apply inherited areas to alerts that lack them
                    for a in alerts:
                        if not a.get("areas"):
                            a["areas"] = list(msg_areas)

        forecast_areas_for_history = []

        for a in alerts:
            # If after inheritance we still have no areas and no time, skip
            if not a["areas"] and not a["clock_time"] and a.get("expected_seconds") is None:
                continue
            
            # calculate target time
            a_target_time = None
            if a.get("clock_time"):
                target_dt = get_target_datetime(a["clock_time"], reference_time=msg_dt)
                a_target_time = target_dt.isoformat()
            elif a.get("expected_seconds") is not None:
                target_dt = msg_dt + timedelta(seconds=a["expected_seconds"])
                a_target_time = target_dt.isoformat()
                
            a["target_time"] = a_target_time
            
            # Normalize areas to broad regions (צפון/מרכז/דרום)
            a["areas"] = list(dict.fromkeys(
                AREA_NORMALIZATION.get(area, area) for area in a["areas"]
            ))

            # Update active alerts by area
            for area in a["areas"]:
                forecast_areas_for_history.append(area)
                
                existing = active_alerts_by_area.get(area)
                preserve_time = not a_target_time and existing and existing.get("target_time")
                
                final_target_time = existing.get("target_time") if preserve_time else a_target_time
                final_clock_time = existing.get("clock_time") if preserve_time else a.get("clock_time")
                final_expected_time = existing.get("expected_time_text") if preserve_time else a.get("expected_time_text")

                # We always take the LATEST message's timing for an area, unless it has no timing
                # and we already have an active timing, in which case we preserve it.
                active_alerts_by_area[area] = {
                    "text": display_text,
                    "target_time": final_target_time,
                    "received_at": msg_dt.isoformat(),
                    "clock_time": final_clock_time,
                    "expected_time_text": final_expected_time,
                    "source_channel": channel_name,
                    "areas": [area],
                    "tight_polygon": extracted.get("tight_polygon"),
                }
                
                # Update today_forecasts (just basic stats tracking)
                fc_exists = any(
                    f.get("text") == text and f.get("areas") == [area]
                    for f in today_forecasts
                )
                if not fc_exists:
                    today_forecasts.append({
                        "text": text,
                        "target_time": a_target_time,
                        "received_at": msg_dt.isoformat(),
                        "expected_time_text": a.get("expected_time_text"),
                        "expected_seconds": a.get("expected_seconds"),
                        "areas": [area],
                        "source_channel": channel_name,
                    })
                    if len(today_forecasts) > MAX_TODAY_FORECASTS:
                        today_forecasts[:] = today_forecasts[-MAX_TODAY_FORECASTS:]

        # Deduplicate history areas
        forecast_areas_for_history = list(dict.fromkeys(forecast_areas_for_history))
        
        if forecast_areas_for_history or text:
            merged_in_history = False
            for h in alert_history:
                h_dt = datetime.fromisoformat(h["received_at"])
                time_diff_sec = abs((msg_dt - h_dt).total_seconds())

                is_close = time_diff_sec <= 6 * 60  # Allow some padding if checking received_at
                
                h_tt_str = h.get("target_time")
                f_tt_str = alerts[0].get("target_time") if alerts else None
                if h_tt_str and f_tt_str:
                    try:
                        h_tt = datetime.fromisoformat(h_tt_str)
                        f_tt = datetime.fromisoformat(f_tt_str)
                        
                        h_sec = h_tt.hour * 3600 + h_tt.minute * 60 + h_tt.second
                        f_sec = f_tt.hour * 3600 + f_tt.minute * 60 + f_tt.second
                        diff = abs(f_sec - h_sec)
                        if diff > 12 * 3600:  # handle midnight wrap-around
                            diff = 24 * 3600 - diff
                            
                        if diff <= 5 * 60:
                            is_close = True
                        else:
                            is_close = False
                    except Exception:
                        pass
                elif h_tt_str or f_tt_str:
                    # If one has target time and the other doesn't, but they are within 15 min received_at, merge them
                    if time_diff_sec <= 15 * 60:
                        is_close = True
                        
                h_areas_set = set(h.get("areas", []))
                f_areas_set = set(forecast_areas_for_history)
                
                if is_close:
                    if not f_areas_set and h_areas_set:
                        h["received_at"] = max(h_dt, msg_dt).isoformat()
                        merged_in_history = True
                    elif f_areas_set and not h_areas_set:
                        h["areas"] = list(f_areas_set)
                        h["received_at"] = max(h_dt, msg_dt).isoformat()
                        merged_in_history = True
                    elif f_areas_set == h_areas_set and f_areas_set:
                        h["received_at"] = max(h_dt, msg_dt).isoformat()
                        merged_in_history = True
                        
                    if merged_in_history:
                        if alerts and (alerts[0].get("expected_time_text") or alerts[0].get("target_time")):
                            h["expected_time_text"] = alerts[0].get("expected_time_text")
                            h["target_time"] = alerts[0].get("target_time")
                            h["text"] = display_text
                        break

            if not merged_in_history and forecast_areas_for_history:
                alert_history.insert(0, {
                    "text": display_text,
                    "received_at": msg_dt.isoformat(),
                    "areas": forecast_areas_for_history,
                    "source_channel": channel_name,
                    "expected_time_text": alerts[0].get("expected_time_text") if alerts else None,
                    "target_time": alerts[0].get("target_time") if alerts else None
                })
                if len(alert_history) > MAX_HISTORY:
                    alert_history.pop()

    _rebuild_latest_event()


def _rebuild_latest_event():
    """Clean up expired alerts and rebuild latest_event from active state."""
    global latest_event

    cleanup_dt = datetime.now(local_tz)
    to_delete = []
    current_alerts_array = []

    for area, info in active_alerts_by_area.items():
        is_relevant = False
        target_time_str = info.get("target_time")
        if target_time_str:
            t_dt = datetime.fromisoformat(target_time_str)
            if (cleanup_dt - t_dt).total_seconds() <= 15 * 60:
                is_relevant = True
        else:
            r_dt = datetime.fromisoformat(info["received_at"])
            if (cleanup_dt - r_dt).total_seconds() <= 30 * 60:  # area-only alerts: 30 min window
                is_relevant = True

        if is_relevant:
            current_alerts_array.append(info)
        else:
            to_delete.append(area)

    for area in to_delete:
        del active_alerts_by_area[area]

    if current_alerts_array:
        # Group by identical timings and texts to avoid duplicate cards
        grouped_alerts = []
        for alert in current_alerts_array:
            found_group = False
            for group in grouped_alerts:
                if group.get("target_time") == alert.get("target_time") and group.get("expected_time_text") == alert.get("expected_time_text"):
                    group["areas"].extend(alert["areas"])
                    group["areas"] = list(dict.fromkeys(group["areas"]))
                    found_group = True
                    break
            if not found_group:
                new_group = alert.copy()
                new_group["areas"] = list(alert["areas"])
                grouped_alerts.append(new_group)

        latest_event = {
            "text": "מערכת התרעות פעילה",
            "target_time": grouped_alerts[0].get("target_time") if grouped_alerts else None,
            "received_at": max([a["received_at"] for a in current_alerts_array]),
            "areas": [],
            "alerts": grouped_alerts,
            "has_data": True
        }
    else:
        latest_event = {
            "text": "ממתין לעדכונים...",
            "target_time": None,
            "has_data": False
        }

async def telegram_polling_loop():
    """Background task: poll Telegram channels via web scraping."""
    print("Ã°Å¸â€œÂ± Starting Telegram channel scraping (no auth needed)...")
    
    # Initial fetch for all channels Ã¢â‚¬â€ load up to 250 pages (~5000 messages) or 2h of history
    INIT_PAGES = 250
    cutoff_2h = datetime.now(local_tz) - timedelta(hours=2)
    for ch_name, ch_config in TELEGRAM_CHANNELS.items():
        try:
            messages = await scrape_telegram_channel(ch_name, ch_config, max_pages=INIT_PAGES, cutoff_dt=cutoff_2h)
            if messages:
                await process_forecast_messages(messages, ch_name, is_init=True)
                telegram_initialized[ch_name] = True
                print(f"Ã¢Å“â€¦ {ch_config['label']}: loaded {len(messages)} messages")
        except Exception as e:
            print(f"Ã¢Å¡Â Ã¯Â¸Â Error initializing {ch_name}: {e}")
    
    # Continuous polling Ã¢â‚¬â€ only latest page
    while True:
        await asyncio.sleep(TELEGRAM_POLL_INTERVAL)
        any_new = False
        for ch_name, ch_config in TELEGRAM_CHANNELS.items():
            try:
                messages = await scrape_telegram_channel(ch_name, ch_config, max_pages=1)
                if messages:
                    await process_forecast_messages(messages, ch_name, is_init=False)
                    any_new = True
            except Exception as e:
                print(f"Ã¢Å¡Â Ã¯Â¸Â Error polling {ch_name}: {e}")

        # Rebuild latest_event once after all channels, not per-channel
        if any_new:
            _rebuild_latest_event()

        # Trim dedup sets to prevent unbounded memory growth
        for ch_name in telegram_last_seen_ids:
            s = telegram_last_seen_ids[ch_name]
            if len(s) > MAX_SEEN_IDS_PER_CHANNEL:
                telegram_last_seen_ids[ch_name] = set(sorted(s, key=int, reverse=True)[:MAX_SEEN_IDS_PER_CHANNEL])

@app.get("/api/latest")
async def get_latest_event(mock: bool = False, tactical: str = None, minutes: float = 5):
    if mock and tactical:
        global _mock_state
        areas = [a.strip() for a in tactical.split(",") if a.strip()]
        now = datetime.now(local_tz)
        mock_key = f"{tactical}:{minutes}"

        if _mock_state["key"] != mock_key:
            _mock_state["key"] = mock_key
            _mock_state["target_time"] = (now + timedelta(minutes=minutes)).isoformat()
            _mock_state["start_time"] = now.isoformat()

        target_dt = datetime.fromisoformat(_mock_state["target_time"])
        clock_time = target_dt.strftime("%H:%M")
        total_secs = minutes * 60
        if total_secs < 60:
            expected_time_text = f"{int(total_secs)} שניות"
        elif minutes == 1:
            expected_time_text = "דקה"
        elif minutes == int(minutes):
            expected_time_text = f"{int(minutes)} דקות"
        else:
            expected_time_text = f"{minutes} דקות"
        areas_label = ", ".join(areas)
        text = f"שיגורים ל{areas_label}\nצפי {clock_time} מגיע"
        alert = {
            "areas": areas,
            "target_time": target_dt.isoformat(),
            "clock_time": clock_time,
            "expected_time_text": expected_time_text,
            "expected_seconds": int(total_secs),
            "source_channel": "mock",
            "text": text,
        }
        # Delay mivzak data by 20s so the UI shows big polygon first, then shrinks
        mock_start = datetime.fromisoformat(_mock_state["start_time"])
        mivzak_ready = (now - mock_start).total_seconds() >= 10
        return {
            "has_data": True,
            "text": text,
            "received_at": now.isoformat(),
            "target_time": target_dt.isoformat(),
            "alerts": [alert],
            "mivzak_replacements": active_mivzak if mivzak_ready else {},
            "mivzak_polygons": active_mivzak_polygons if mivzak_ready else {},
        }
    return {**latest_event, "mivzak_replacements": active_mivzak, "mivzak_polygons": active_mivzak_polygons}

@app.get("/api/history")
async def get_alert_history():
    return alert_history

@app.get("/api/oref-alerts")
async def get_oref_alerts(mock: bool = False, oref: str = None, tactical: str = None):
    if mock:
        if oref:
            broad_areas = [a.strip() for a in oref.split(",") if a.strip()]
        elif tactical and _mock_state.get("target_time"):
            target_dt = datetime.fromisoformat(_mock_state["target_time"])
            if datetime.now(local_tz) < target_dt:
                return {"data": [], "title": ""}
            broad_areas = [a.strip() for a in tactical.split(",") if a.strip()]
        else:
            return {"data": [], "title": ""}
        cities: list[str] = []
        for area in broad_areas:
            cities.extend(_REGION_TO_CITIES.get(area, [area]))
        return {
            "data": list(dict.fromkeys(cities)),
            "title": "ירי רקטות וטילים",
        }

    global active_oref_alerts
    now = datetime.now(local_tz)
    active_oref_alerts = [a for a in active_oref_alerts if (now - a["msg_dt"]).total_seconds() <= 300]
    all_cities: list[str] = []
    for a in active_oref_alerts:
        all_cities.extend(a["cities"])
    all_cities = list(dict.fromkeys(all_cities))
    return {
        "data": all_cities,
        "title": "ירי רקטות וטילים" if all_cities else "",
    }

@app.get("/")
async def serve_index():
    return FileResponse("index.html")

@app.get("/sw.js")
async def serve_sw():
    return FileResponse("sw.js", media_type="application/javascript", headers={"Service-Worker-Allowed": "/"})

@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse("manifest.json", media_type="application/manifest+json")

@app.get("/icon.svg")
async def serve_icon():
    return FileResponse("icon.svg", media_type="image/svg+xml")

@app.get("/regional_coords_final.json")
async def serve_regional_coords():
    return FileResponse("regional_coords_final.json", media_type="application/json")

async def fetch_israel_cities():
    """Fetch all Israeli settlements from data.gov.il dynamically on startup."""
    print("Ã°Å¸Å’Â Fetching official Israeli cities from data.gov.il...")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            url = "https://data.gov.il/api/3/action/datastore_search?resource_id=5c78e9fa-c2e2-4771-93ff-7f400a12f7ba&limit=2000"
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                records = data.get("result", {}).get("records", [])
                
                new_cities = 0
                for rec in records:
                    city_name = rec.get("שם_ישוב", "").strip()
                    clean_name = clean_hebrew_city(city_name)
                    if clean_name and len(clean_name) >= 2 and clean_name not in KNOWN_AREAS and clean_name != "אזור":
                        KNOWN_AREAS.append(clean_name)
                        new_cities += 1
                        
                # Sort descending by length so "תל אביב" matches before "תל"
                KNOWN_AREAS.sort(key=len, reverse=True)
                print(f"Ã¢Å“â€¦ Successfully loaded {new_cities} new cities/settlements from online DB.")
    except Exception as e:
        print(f"Ã¢Å¡Â Ã¯Â¸Â Error fetching cities: {e}")

import json
from datetime import datetime
import asyncio
import os

async def debug_load_messages():
    """Load test data from pytest_data.json (preferred) or real_messages.json for local debug."""
    # Choose data file: prefer pytest_data.json, then real_messages.json
    debug_file = None
    for candidate in ['pytest_data.json', 'real_messages.json']:
        if os.path.exists(candidate):
            debug_file = candidate
            break
    
    if not debug_file:
        return

    try:
        print(f"🐞 DEBUG: Loading messages from {debug_file}")
        with open(debug_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Add msg_dt to each message
        for msg in data:
            msg["msg_dt"] = datetime.fromisoformat(msg["date"])
        
        # Group by channel, sorted chronologically per channel
        by_channel = {}
        for msg in data:
            ch = msg.get("channel", "shigurimsh")
            if ch not in by_channel:
                by_channel[ch] = []
            by_channel[ch].append(msg)
        for msgs in by_channel.values():
            msgs.sort(key=lambda x: x["msg_dt"])
        
        if not data:
            return

        original_now = datetime.now

        print(f"🐞 DEBUG: Processing {len(data)} messages from {len(by_channel)} channels")
        
        try:
            from unittest.mock import patch
            for ch, msgs in by_channel.items():
                ch_last_dt = msgs[-1]["msg_dt"]
                mock_now = ch_last_dt + timedelta(minutes=5)
                print(f"🐞 Processing {len(msgs)} debug messages for channel: {ch} (mock_now={mock_now.strftime('%H:%M')})")
                
                class MockDatetime(datetime):
                    @classmethod
                    def now(cls, tz=None):
                        return mock_now
                        
                with patch('server.datetime', MockDatetime):
                    await process_forecast_messages(msgs, ch, is_init=True)
        finally:
            pass

        print(f"🐞 DEBUG: Done! alert_history has {len(alert_history)} entries.")

        # Load mock oref mivzak data only (not siren alerts, to avoid fake green dots)
        global active_mivzak
        for mock_msg in MOCK_OREF_MESSAGES:
            text = mock_msg["text"]
            mivzak_cities = parse_oref_mivzak(text)
            if mivzak_cities:
                replacements, _ = build_mivzak_replacements(mivzak_cities)
                merge_mivzak(replacements)
        print(f"🐞 DEBUG: Loaded mivzak data from {len(MOCK_OREF_MESSAGES)} mock oref messages.")

    except Exception as e:
        import traceback
        print(f"DEBUG LOAD FAILED: {e}")
        traceback.print_exc()

@app.on_event("startup")
async def startup_event():
    # Load cities dynamically
    await fetch_israel_cities()
    
    # DEBUG: Load test messages
    await debug_load_messages()
    
    # Start Telegram channel scraping (no auth needed)
    asyncio.create_task(telegram_polling_loop())
    
    # Start Oref polling loop (independent from forecast channels)
    asyncio.create_task(oref_polling_loop())


async def oref_polling_loop():
    """Independent polling loop for PikudHaOref_all Telegram channel.

    Scrapes the public page, parses messages with TelegramPageParser,
    and processes siren alerts, מבזק early warnings, and all-clear events.
    """
    global active_oref_alerts, active_mivzak, _mivzak_last_update
    url = OREF_URL
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/131.0.0.0 Safari/537.36",
    }
    print(f"Starting Pikud Haoref polling: {url}")
    async with httpx.AsyncClient(verify=False, timeout=15, follow_redirects=True) as client:
        while True:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    parser = TelegramPageParser()
                    parser.feed(resp.text)
                    now = datetime.now(local_tz)
                    for msg in parser.messages:
                        msg_id = msg.get("id", "")
                        text = msg.get("text", "").strip()
                        if not msg_id or not text:
                            continue
                        if msg_id in _oref_seen_ids:
                            continue
                        _oref_seen_ids.add(msg_id)
                        try:
                            msg_dt = datetime.fromisoformat(
                                msg.get("datetime", "").replace("Z", "+00:00")
                            ).astimezone(local_tz)
                        except Exception:
                            msg_dt = now
                        if (now - msg_dt).total_seconds() > 300:
                            continue

                        cities = parse_oref_siren_cities(text)
                        if cities:
                            active_oref_alerts.append({
                                "id": msg_id,
                                "cities": cities,
                                "msg_dt": msg_dt,
                            })
                            continue

                        mivzak_cities = parse_oref_mivzak(text)
                        if mivzak_cities:
                            replacements, _ = build_mivzak_replacements(mivzak_cities)
                            merge_mivzak(replacements)
                            continue

                        if "האירוע הסתיים" in text:
                            active_mivzak.clear()
                            active_mivzak_polygons.clear()
                            _mivzak_last_update = None

                    # Expire old alerts
                    active_oref_alerts = [
                        a for a in active_oref_alerts
                        if (now - a["msg_dt"]).total_seconds() <= 300
                    ]

                    # Expire stale mivzak data
                    if _mivzak_last_update and (now - _mivzak_last_update).total_seconds() > MIVZAK_TIMEOUT_SECONDS:
                        active_mivzak.clear()
                        active_mivzak_polygons.clear()
                        _mivzak_last_update = None
                    # Trim dedup set
                    if len(_oref_seen_ids) > MAX_SEEN_IDS_PER_CHANNEL:
                        _oref_seen_ids.clear()
            except Exception as e:
                print(f"Oref polling error: {e}")
            await asyncio.sleep(OREF_POLL_INTERVAL)


def kill_port(port):
    """Kill any process listening on the given port."""
    import subprocess
    import signal
    import time
    try:
        output = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}"], stderr=subprocess.DEVNULL
        ).decode().strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return
    for pid_str in output.split():
        pid = int(pid_str)
        if pid == os.getpid():
            continue
        print(f"Killing process {pid} on port {port}...")
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    time.sleep(1)


if __name__ == "__main__":
    kill_port(PORT)
    uvicorn.run(app, host="0.0.0.0", port=PORT)
