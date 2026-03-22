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
    }
}
TELEGRAM_POLL_INTERVAL = 5  # seconds between scrapes
# ==========================

local_tz = tz.gettz("Asia/Jerusalem")
app = FastAPI()

# Load city coordinates for tight-polygon computation on בדקות messages
_geo_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "regional_coords_final.json")
with open(_geo_file) as _f:
    CITY_COORDS_LOOKUP: dict = json.load(_f).get("CITY_COORDS", {})

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
active_oref_alerts = []      # Track official Pikud Haoref alerts

# Store alert history (last 50 alerts)
alert_history = []
MAX_HISTORY = 50

# ==========================
# Today's data for statistics
# ==========================
today_forecasts = []      # All "צפי" messages from today: [{text, target_time, received_at, raw_text}]
today_messages = []       # ALL messages from the channel today (for display)
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
    # Remove Telegram URLs
    text = re.sub(r'https?://t\.me/\S*', '', text)
    # Remove extra newlines and whitespace  
    text = re.sub(r'\n{2,}', '\n', text).strip()
    return text

def extract_expected_time_text(text):
    """Extract expected duration expressions like '5 דקות' or '35 שניות' or '4.5 דקות'."""
    m = re.search(r'(?:(\d+(?:\.\d+)?)\s*)?(דקות|דקה|שניות|שניה)', text)
    if not m:
        return None
    num = m.group(1)
    unit = m.group(2)
    if num:
        return f"{num} {unit}"
    else:
        return unit

def _to_expected_seconds(expected_time_text):
    if not expected_time_text:
        return None
    m = re.search(r'(?:(\d+(?:\.\d+)?)\s*)?(דקות|דקה|שניות|שניה)', expected_time_text)
    if not m:
        return None
    
    num_str = m.group(1)
    unit = m.group(2)
    
    if num_str:
        value = float(num_str)
    else:
        value = 1.0 # default for "דקה" or "שניה"
        
    if unit in ("דקות", "דקה"):
        return int(value * 60)
    return int(value)

KNOWN_AREAS = [
    "מרכז", "צפון", "דרום", "עוטף עזה", "שרון", "שפלה", "גוש דן",
    "יהודה", "הגליל", "גליל", "הגולן", "גולן", "קריות", "עמק יזרעאל",
    "ים המלח", "הערבה", "מפרץ", "בקעה", "המדבר", "גליל עליון",
    "גליל תחתון", "גליל מערבי", "עוטף", "מירון", "כיש", "שומרון"
]

TACTICAL_REGION_MAPPING = {
    "באר שבע": "דרום", "דימונה": "דרום", "אשדוד": "דרום", "אשקלון": "דרום",
    "נתיבות": "דרום", "שדרות": "דרום", "אילת": "דרום", "אופקים": "דרום",
    "תל אביב": "מרכז", "ראשון לציון": "מרכז", "חולון": "מרכז", "רמת גן": "מרכז",
    "פתח תקווה": "מרכז", "הרצליה": "מרכז", "נתניה": "שרון", "כפר סבא": "שרון",
    "חיפה": "צפון", "עכו": "צפון", "נהריה": "צפון", "טבריה": "צפון", "צפת": "צפון",
    "כרמיאל": "צפון", "ראש פינה": "צפון", "קרית שמונה": "צפון"
}

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


def compute_tight_polygon(place_names):
    """Return a buffered convex hull polygon for the given city names, or None."""
    coords = [tuple(CITY_COORDS_LOOKUP[n]) for n in place_names if n in CITY_COORDS_LOOKUP]
    if not coords:
        return None
    return _buffer_polygon(_convex_hull(coords))


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
    
    # Exclude common non-area words
    exclude_words = {"שיגור", "שיגורים", "כעת", "אזעקות", "אזעקה", "יירוטים", "חזלש", "מלבנון", "מאיראן", "מעזה", "מתימן", "מעיראק", "מגיע"}
    
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if any(skip in line for skip in [
            "http://", "https://", "היכנסו", "פיקוד העורף", "ירי רקטות", "חדירת כלי", "חדירת מחבלים", "ללא התרעה", "מערכות ההגנה", "ערוץ", "בלבד", "בדרכם", "יורטו", "חריג", "פרטים", "נוספים"
        ]):
            continue
            
        # Skip lines that are clearly purely metadata lines
        if line.startswith("צפי") and len(line) < 15:
            continue
            
        # Strip exact time formats and time units so they don't become areas
        line = re.sub(r'\d{1,2}:\d{2}(?::\d{2})?', '', line)
        line = re.sub(r'(?:(\d+(?:\.\d+)?)\s*)?(דקות|דקה|שניות|שניה)', '', line)
        line = re.sub(r'צפי|משך|עוד|לאזעקה|כעת|כרגע|לכרגע', '', line)
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
                words = [w for w in part.split() if w not in exclude_words and w != "ו"]
                cleaned_area = " ".join(words).strip()
                cleaned_area = re.sub(r'^(לכיוון\s|אל\s|כיוון\s|אזור\s|באזור\s|גם\sל|גם\sב|גם\s|ל|ב)', '', cleaned_area).strip()
                
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
    
    # Exclude common non-area words
    exclude_words = {"שיגור", "שיגורים", "כעת", "אזעקות", "אזעקה", "יירוטים", "חזלש", "מלבנון", "מאיראן", "מעזה", "מתימן", "מעיראק", "מגיע", "זוהו"}
    
    lines = re.split(r'\n|\.\s+', text)
    
    global_clock_time = None
    global_expected_text = None
    
    for line in lines:
        line = line.strip()
        if not line: continue
            
        if any(skip in line.lower() for skip in [
            "http://", "https://", "היכנסו", "פיקוד העורף", "ירי רקטות", "חדירת כלי", "חדירת מחבלים", "ללא התרעה", "מערכות ההגנה", "ערוץ", "בלבד", "בדרכם", "יורטו", "חריג", "פרטים", "נוספים",
            "מבצע", "טלויזיה", "מומלץ", "לחץ כאן", "tv", "מגשימים", "חבורה", "פיצוצים", "נפילה", "קולות", "הדף", "שנה של", "ערבות", "מיקוד", "ארוך טווח"
        ]):
            continue
            
        clock_m = re.search(r'(\d{1,2}:\d{2}(?::\d{2})?)', line)
        line_clock_time = clock_m.group(1) if clock_m else None
        
        expected_m = re.search(r'(?:(\d+(?:\.\d+)?)\s*)?(דקות|דקה|שניות|שניה)', line)
        line_expected_text = expected_m.group(0) if expected_m else None
        
        if line_clock_time: global_clock_time = line_clock_time
        if line_expected_text: global_expected_text = line_expected_text
            
        line_clean = line
        line_clean = re.sub(r'\d{1,2}:\d{2}(?::\d{2})?', '', line_clean)
        line_clean = re.sub(r'(?:(\d+(?:\.\d+)?)\s*)?(דקות|דקה|שניות|שניה)', '', line_clean)
        line_clean = re.sub(r'צפי|משך|עוד|לאזעקה', '', line_clean)
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
                target_dt = get_target_datetime(a["clock_time"], reference_time=msg_dt if is_init else None)
                a_target_time = target_dt.isoformat()
            elif a.get("expected_seconds") is not None:
                target_dt = msg_dt + timedelta(seconds=a["expected_seconds"])
                a_target_time = target_dt.isoformat()
                
            a["target_time"] = a_target_time
            
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

    # Clean up active alerts and build latest_event
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
            "areas": [], # Handled by subAlertsContainer
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
        for ch_name, ch_config in TELEGRAM_CHANNELS.items():
            try:
                messages = await scrape_telegram_channel(ch_name, ch_config, max_pages=1)
                if messages:
                    await process_forecast_messages(messages, ch_name, is_init=False)
            except Exception as e:
                print(f"Ã¢Å¡Â Ã¯Â¸Â Error polling {ch_name}: {e}")

@app.get("/api/latest")
async def get_latest_event(mock: bool = False, tactical: str = None, minutes: float = 5):
    if mock and tactical:
        areas = [a.strip() for a in tactical.split(",") if a.strip()]
        now = datetime.now(local_tz)
        target_dt = now + timedelta(minutes=minutes)
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
            "source_channel": "shigurimsh",
            "text": text,
        }
        return {
            "has_data": True,
            "text": text,
            "received_at": now.isoformat(),
            "target_time": target_dt.isoformat(),
            "alerts": [alert],
        }
    return latest_event

@app.get("/api/history")
async def get_alert_history():
    return alert_history

@app.get("/api/oref-alerts")
async def get_oref_alerts(mock: bool = False, oref: str = None):
    if mock and oref:
        cities = [c.strip() for c in oref.split(",") if c.strip()]
        return {
            "data": cities,
            "title": "ירי רקטות וטילים",
        }
    global active_oref_alerts
    now = datetime.now(local_tz)
    # Filter out alerts older than 5 minutes (300s)
    active_oref_alerts = [a for a in active_oref_alerts if (now - a["msg_dt"]).total_seconds() <= 300]
    
    all_areas = []
    for a in active_oref_alerts:
        all_areas.extend(a["areas"])
    all_areas = list(dict.fromkeys(all_areas))
    
    return {
        "data": all_areas,
        "title": "ירי רקטות וטילים" if all_areas else ""
    }

@app.get("/")
async def serve_index():
    return FileResponse("index.html")

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
    
    # Start Oref polling loop
    asyncio.create_task(oref_polling_loop())

async def oref_polling_loop():
    global active_oref_alerts
    print("📡 Starting Pikud Haoref polling loop...")
    # Public history API (gets latest alerts)
    oref_url = "https://www.oref.org.il/WarningMessages/History/AlertsHistory.json"
    headers = {
        "Referer": "https://www.oref.org.il/",
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        while True:
            try:
                resp = await client.get(oref_url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    now = datetime.now(local_tz)
                    for alert in data:
                        # alertDate is like "2024-03-21 03:30:00"
                        try:
                            alert_dt = datetime.strptime(alert["alertDate"], "%Y-%m-%d %H:%M:%S")
                            alert_dt = alert_dt.replace(tzinfo=local_tz)
                            if (now - alert_dt).total_seconds() <= 300:
                                # Filter only for Rocket alerts (category 1)
                                if alert.get("category") != 1:
                                    continue
                                
                                areas = [a.strip() for a in alert["data"].split(',')]
                                rid = alert.get("rid") or f"{alert['alertDate']}-{alert['data']}"
                                if not any(a["id"] == rid for a in active_oref_alerts):
                                    active_oref_alerts.append({
                                        "id": rid,
                                        "areas": areas,
                                        "msg_dt": now
                                    })
                        except Exception:
                            continue
            except Exception as e:
                # Silently catch common connection errors to avoid spamming logs
                pass
            await asyncio.sleep(3)

if __name__ == "__main__":
    import os, subprocess, sys, time
    print(f"Checking for existing server on port {PORT}...")
    try:
        if os.name == 'nt':
            output = subprocess.check_output(f"netstat -ano | findstr :{PORT}", shell=True).decode()
            for line in output.strip().split('\n'):
                if 'LISTENING' in line:
                    pid = line.split()[-1]
                    if str(pid) != str(os.getpid()) and pid != '0':
                        print(f"Killing old server process (PID: {pid}) on port {PORT}...")
                        subprocess.call(["taskkill", "/F", "/PID", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        time.sleep(1)
        else:
            subprocess.call(["fuser", "-k", f"{PORT}/tcp"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

    uvicorn.run(app, host="0.0.0.0", port=PORT)
