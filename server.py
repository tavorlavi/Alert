import re
import json
import asyncio
import httpx
from datetime import datetime, timedelta
from dateutil import tz
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
from html.parser import HTMLParser

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
    "PikudHaOref_all": {
        "url": "https://t.me/s/PikudHaOref_all",
        "type": "official_alert",  # Official Pikud HaOref alerts
        "label": "פיקוד העורף"
    }
}
TELEGRAM_POLL_INTERVAL = 5  # seconds between scrapes
# ==========================

local_tz = tz.gettz("Asia/Jerusalem")
app = FastAPI()

# Serve static files (CSS, JS, images)
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)

# Load city coordinates for the map
city_coords_path = os.path.join(static_dir, "cities.json")
city_coords = {}
if os.path.exists(city_coords_path):
    with open(city_coords_path, "r", encoding="utf-8") as f:
        city_coords = json.load(f)

# Allow connections from any origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# WebSocket connection manager for real-time updates
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

manager = ConnectionManager()

# Store the latest event so new users see it immediately when they open the site
latest_event = {
    "text": "ממתין לעדכונים...", 
    "target_time": None,
    "has_data": False
}

# Store alert history (last 50 alerts)
alert_history = []
MAX_HISTORY = 50

# ==========================
# Today's data for statistics
# ==========================
today_forecasts = []      # All "צפי" messages from today: [{text, target_time, received_at, raw_text}]
today_real_alerts = []    # All real alerts from today (from Oref history): [{alertDate, title, city, category}]
today_messages = []       # ALL messages from the channel today (for display)

# ==========================
# Pikud HaOref (Home Front Command) API
# ==========================
OREF_HISTORY_URL = "https://alerts-history.oref.org.il/Shared/Ajax/GetAlarmsHistory.aspx?lang=he&mode=2"
OREF_HISTORY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "he-IL,he;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://www.oref.org.il/",
}

OREF_POLL_INTERVAL = 2  # seconds between polls

# Alert category mapping
ALERT_CATEGORIES = {
    1: {"type": "missiles", "icon": "🚀", "label": "ירי רקטות וטילים", "color": "red"},
    2: {"type": "hostileAircraft", "icon": "✈️", "label": "חדירת כלי טיס עוין", "color": "orange"},
    3: {"type": "general", "icon": "⚠️", "label": "אירוע כללי", "color": "amber"},
    4: {"type": "radiological", "icon": "☢️", "label": "אירוע רדיולוגי", "color": "purple"},
    5: {"type": "tsunami", "icon": "🌊", "label": "צונאמי", "color": "blue"},
    6: {"type": "hostileAircraft", "icon": "✈️", "label": "חדירת כלי טיס עוין", "color": "orange"},
    7: {"type": "hazardous", "icon": "☣️", "label": "חומרים מסוכנים", "color": "green"},
    10: {"type": "newsFlash", "icon": "📢", "label": "מבזק", "color": "blue"},
    13: {"type": "eventEnded", "icon": "✅", "label": "האירוע הסתיים", "color": "gray"},
    14: {"type": "earlyWarning", "icon": "⚠️", "label": "בדקות הקרובות צפויות להתקבל התרעות באזורך", "color": "yellow"},
}

# Oref state
oref_active_alerts = []     # Currently active alerts from Alerts.json
oref_recent_history = []    # Recent history from AlertsHistory.json (last 50)
oref_last_alert_ids = set() # Track which alerts we already broadcasted

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

# (Telegram message fetching is now handled by telegram_polling_loop via web scraping)


def compute_stats():
    """Compare last 12 hours of forecasts vs real alerts to produce accuracy statistics."""
    now = datetime.now(local_tz)
    today_str = now.strftime("%Y-%m-%d")
    cutoff_12h = now - timedelta(hours=12)
    
    # Use accumulated today_real_alerts (last 12h, persists across Oref history rotations)
    # Filter to only last 12 hours
    real_alerts_recent = [
        a for a in today_real_alerts
        if a.get("alertDate", "") >= cutoff_12h.strftime("%Y-%m-%d %H:%M:%S")
    ]
    
    # Group real alerts by time (within 1 min window) to get unique "alert rounds"
    # Only include MISSILE alerts (category 1) for forecast matching,
    # since forecasts from shigurimsh are specifically about missile launches
    alert_rounds = []
    for item in real_alerts_recent:
        cat = item.get("category", 1)
        if cat != 1:
            continue  # Skip non-missile alerts (aircraft, earthquake, etc.)
        
        alert_date_str = item.get("alertDate", "")
        try:
            alert_dt = datetime.strptime(alert_date_str, "%Y-%m-%d %H:%M:%S")
            alert_dt = alert_dt.replace(tzinfo=local_tz)
        except Exception:
            continue
        
        # Check if this belongs to an existing round (within 2 minutes)
        found = False
        for rnd in alert_rounds:
            if abs((alert_dt - rnd["time"]).total_seconds()) < 120:
                rnd["cities"].append(item.get("city", ""))
                rnd["count"] += 1
                found = True
                break
        if not found:
            alert_rounds.append({
                "time": alert_dt,
                "time_str": alert_dt.strftime("%H:%M:%S"),
                "title": item.get("title", ""),
                "cities": [item.get("city", "")],
                "count": 1,
                "category": cat,
            })
    
    # Sort rounds by time
    alert_rounds.sort(key=lambda r: r["time"])
    
    # Find oldest alert time (to know data coverage boundary)
    oldest_alert_time = alert_rounds[0]["time"] if alert_rounds else None
    
    # Build comparison: match each forecast to the closest real alert
    # Deduplicate forecasts by target_time (same time = same prediction)
    comparisons = []
    used_rounds = set()
    seen_forecast_times = set()
    
    for fc in today_forecasts:
        try:
            fc_time = datetime.fromisoformat(fc["target_time"])
            if fc_time.tzinfo is None:
                fc_time = fc_time.replace(tzinfo=local_tz)
        except Exception:
            continue
        
        fc_time_key = fc_time.strftime("%H:%M")
        if fc_time_key in seen_forecast_times:
            continue  # Skip duplicate forecasts for the same time
        seen_forecast_times.add(fc_time_key)
        
        best_match = None
        best_diff = None
        best_idx = None
        
        for idx, rnd in enumerate(alert_rounds):
            if idx in used_rounds:
                continue
            diff = abs((rnd["time"] - fc_time).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_match = rnd
                best_idx = idx
        
        comparison = {
            "forecast_text": fc["text"],
            "forecast_time": fc_time_key,
            "received_at": fc.get("received_at", ""),
        }
        
        # Match only if within 3 minutes (180 seconds) — tighter window to avoid false matches
        if best_match and best_diff is not None and best_diff < 180:
            used_rounds.add(best_idx)
            diff_minutes = (best_match["time"] - fc_time).total_seconds() / 60
            comparison["real_time"] = best_match["time_str"]
            comparison["real_title"] = best_match["title"]
            comparison["real_cities_count"] = best_match["count"]
            comparison["diff_minutes"] = round(diff_minutes, 1)
            comparison["diff_label"] = _format_diff(diff_minutes)
            comparison["matched"] = True
        else:
            comparison["real_time"] = None
            comparison["matched"] = False
            comparison["diff_minutes"] = None
            # If forecast is older than our oldest Oref data, show "no data"
            if oldest_alert_time and fc_time < oldest_alert_time:
                comparison["diff_label"] = "אין מידע"
            else:
                comparison["diff_label"] = "ללא התרעה תואמת"
        
        comparisons.append(comparison)
    
    # Unmatched real alerts (missile alerts with no forecast)
    unmatched_alerts = []
    for idx, rnd in enumerate(alert_rounds):
        if idx not in used_rounds and rnd.get("category") == 1:
            unmatched_alerts.append({
                "real_time": rnd["time_str"],
                "title": rnd["title"],
                "cities_count": rnd["count"],
                "cities": rnd["cities"][:5],
            })
    
    # Summary stats
    total_forecasts = len(comparisons)  # Deduplicated forecast count
    total_alerts = len(alert_rounds)
    matched = len([c for c in comparisons if c["matched"]])
    avg_diff = None
    if matched > 0:
        diffs = [abs(c["diff_minutes"]) for c in comparisons if c["matched"] and c["diff_minutes"] is not None]
        avg_diff = round(sum(diffs) / len(diffs), 1) if diffs else None
    
    return {
        "date": today_str,
        "summary": {
            "total_forecasts": total_forecasts,
            "total_alert_rounds": total_alerts,
            "matched": matched,
            "unmatched_forecasts": total_forecasts - matched,
            "unmatched_alerts": len(unmatched_alerts),
            "avg_diff_minutes": avg_diff,
        },
        "comparisons": comparisons,
        "unmatched_alerts": unmatched_alerts,
        "today_messages_count": len(today_messages),
    }


def _format_diff(diff_minutes):
    """Format time difference as a human-readable Hebrew string."""
    abs_diff = abs(diff_minutes)
    if abs_diff < 1:
        return "⏱️ מדויק!"
    
    if abs_diff < 60:
        mins = int(abs_diff)
        label = f"{mins} דקות"
    else:
        hours = int(abs_diff // 60)
        mins = int(abs_diff % 60)
        label = f"{hours} שעות" + (f" ו-{mins} דקות" if mins > 0 else "")
    
    if diff_minutes > 0:
        return f"⏰ ההתרעה איחרה ב-{label}"
    else:
        return f"⚡ ההתרעה הקדימה ב-{label}"


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
                print(f"   📄 {channel_name}: scraped {page + 1} pages, {len(all_results)} messages" + (" (reached 12h cutoff)" if reached_cutoff else ""))
            
            return all_results
        except Exception as e:
            print(f"⚠️ Error scraping {channel_name}: {e}")
            return []


async def process_shigurimsh_messages(messages, is_init=False):
    """Process messages from shigurimsh channel (timing forecasts)."""
    global latest_event, today_forecasts, today_messages
    
    now = datetime.now(local_tz)
    cutoff = now - timedelta(hours=12)
    
    new_msgs = []
    for msg in messages:
        msg_dt = msg["msg_dt"]
        if msg_dt < cutoff:
            continue
        
        msg_id = msg["id"]
        is_new = msg_id and msg_id not in telegram_last_seen_ids["shigurimsh"]
        
        if is_init or is_new:
            new_msgs.append(msg)
    
    if not new_msgs:
        return
    
    for msg in new_msgs:
        msg_id = msg["id"]
        if msg_id:
            telegram_last_seen_ids["shigurimsh"].add(msg_id)
        
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
        
        # Try to extract timing forecast
        time_str = extract_time_from_text(text)
        if time_str:
            target_time = get_target_datetime(time_str, reference_time=msg_dt if is_init else None)
            display_text = clean_forecast_text(text)
            
            # Add to today's forecasts
            fc_exists = any(
                f.get("target_time") == target_time.isoformat() and f.get("text") == text
                for f in today_forecasts
            )
            if not fc_exists:
                today_forecasts.append({
                    "text": text,
                    "target_time": target_time.isoformat(),
                    "received_at": msg_dt.isoformat(),
                })
            
            # Update latest event
            latest_event = {
                "text": display_text,
                "target_time": target_time.isoformat(),
                "has_data": True
            }
            
            # Save to history
            h_exists = any(
                h.get("target_time") == target_time.isoformat() and h.get("text") == text
                for h in alert_history
            )
            if not h_exists:
                alert_history.insert(0, {
                    "text": display_text,
                    "target_time": target_time.isoformat(),
                    "received_at": msg_dt.isoformat()
                })
                if len(alert_history) > MAX_HISTORY:
                    alert_history.pop()
            
            # Broadcast to clients (only for NEW messages, not init)
            if not is_init:
                await manager.broadcast({
                    "msg_type": "telegram_timing",
                    **latest_event
                })


async def process_pikud_haoref_messages(messages, is_init=False):
    """Process messages from PikudHaOref_all channel (official alerts).
    
    Handles ALL message types:
    - 🚨ירי רקטות וטילים  → category 1 (missiles)
    - ✈חדירת כלי טיס עוין → category 2 (hostile aircraft)  
    - 🚨מבזק + בדקות הקרובות → category 14 (early warning)
    - 🚨עדכון + האירוע הסתיים → category 13 (event ended)
    
    Populates today_real_alerts for stats and triggers live alerts.
    """
    global telegram_pikud_active_alerts, today_messages, today_real_alerts
    
    now = datetime.now(local_tz)
    cutoff = now - timedelta(hours=12)
    
    for msg in messages:
        msg_id = msg["id"]
        msg_dt = msg["msg_dt"]
        text = msg["text"]
        
        if msg_dt < cutoff:
            continue
        
        is_new = msg_id and msg_id not in telegram_last_seen_ids["PikudHaOref_all"]
        if msg_id:
            telegram_last_seen_ids["PikudHaOref_all"].add(msg_id)
        
        if not is_new and not is_init:
            continue
        
        # --- Determine alert category from message text ---
        alert_cat = None
        title = ""
        
        if "ירי רקטות וטילים" in text:
            if "האירוע הסתיים" in text:
                alert_cat = 13
                title = "ירי רקטות וטילים -  האירוע הסתיים"
            else:
                alert_cat = 1
                title = "ירי רקטות וטילים"
        elif "חדירת כלי טיס עוין" in text:
            if "האירוע הסתיים" in text:
                alert_cat = 13
                title = "חדירת כלי טיס עוין - האירוע הסתיים"
            else:
                alert_cat = 2
                title = "חדירת כלי טיס עוין"
        elif "בדקות הקרובות צפויות להתקבל התרעות באזורך" in text:
            alert_cat = 14
            title = "בדקות הקרובות צפויות להתקבל התרעות באזורך"
        elif "חדירת מחבלים" in text:
            if "האירוע הסתיים" in text:
                alert_cat = 13
                title = "חדירת מחבלים - האירוע הסתיים"
            else:
                alert_cat = 3
                title = "חדירת מחבלים"
        else:
            # Unknown message type — skip
            continue
        
        # --- Extract the actual alert time from message text ---
        # Format in messages: (D/M/YYYY) H:MM  e.g. (5/3/2026) 2:28
        alert_time_match = re.search(r'\((\d{1,2})/(\d{1,2})/(\d{4})\)\s*(\d{1,2}):(\d{2})', text)
        if alert_time_match:
            day = int(alert_time_match.group(1))
            month = int(alert_time_match.group(2))
            year = int(alert_time_match.group(3))
            hour = int(alert_time_match.group(4))
            minute = int(alert_time_match.group(5))
            try:
                alert_dt = datetime(year, month, day, hour, minute, 0, tzinfo=local_tz)
            except Exception:
                alert_dt = msg_dt
        else:
            alert_dt = msg_dt
        
        # --- Extract cities from message ---
        lines = text.split("\n")
        cities = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Skip header/instruction lines
            if any(skip in line for skip in [
                "היכנסו", "ירי רקטות", "חדירת כלי", "חדירת מחבלים",
                "מבזק", "אזור ", "בדקות הקרובות", "לשפר את", "לשהות בו",
                "בהמשך לדיווח", "עדכון", "האירוע הסתיים", "על תושבי",
                "במקרה של", "יש להיכנס", "באזורים הבאים"
            ]):
                continue
            # This line likely contains city names
            for city in line.split(","):
                city = city.strip()
                if city and len(city) > 1:
                    city = re.sub(r'\(.*?\)', '', city).strip()
                    if city:
                        cities.append(city)
        
        # --- Add each city to today_real_alerts for stats ---
        alert_date_str = alert_dt.strftime("%Y-%m-%d %H:%M:%S")
        cat_info = ALERT_CATEGORIES.get(alert_cat, ALERT_CATEGORIES.get(1, {}))
        
        for city in cities:
            exists = any(
                a["alertDate"] == alert_date_str and a["city"] == city
                for a in today_real_alerts
            )
            if not exists:
                today_real_alerts.append({
                    "alertDate": alert_date_str,
                    "title": title,
                    "city": city,
                    "category": alert_cat,
                    "category_info": cat_info,
                })
        
        # --- Add to today's messages for display ---
        display_id = f"pikud_{msg_id}"
        exists = any(m.get("id") == display_id for m in today_messages)
        if not exists:
            today_messages.insert(0, {
                "text": f"[פיקוד העורף] {text}",
                "date": msg_dt.isoformat(),
                "id": display_id,
            })
        
        # --- Live alert broadcasting (only new real-time alerts, not init) ---
        if cities and is_new and not is_init and alert_cat in (1, 2, 14):
            alert_key = f"tg_{alert_cat}_{msg_id}"
            
            alert_obj = {
                "id": alert_key,
                "cities": cities[:50],
                "title": cat_info.get("label", title),
                "desc": "",
                "category": alert_cat,
                "category_info": cat_info,
                "timestamp": alert_dt.isoformat(),
                "source": "telegram_pikud"
            }
            
            add_persistent_alert(alert_obj)
            
            await manager.broadcast({
                "msg_type": "oref_alert",
                "alert": alert_obj,
                "is_new": True
            })
            
            # --- Check if this alert matches the current forecast (early/late detection) ---
            if alert_cat == 1 and latest_event.get("has_data") and latest_event.get("target_time"):
                try:
                    forecast_target = datetime.fromisoformat(latest_event["target_time"])
                    diff_seconds = (alert_dt - forecast_target).total_seconds()
                    diff_minutes = diff_seconds / 60
                    # Only match if within 10 minutes of forecast
                    if abs(diff_minutes) < 10:
                        await manager.broadcast({
                            "msg_type": "forecast_matched",
                            "diff_seconds": diff_seconds,
                            "diff_minutes": round(diff_minutes, 1),
                            "forecast_time": latest_event["target_time"],
                            "alert_time": alert_dt.isoformat(),
                            "early": diff_seconds < 0,
                            "late": diff_seconds > 0,
                        })
                except Exception:
                    pass


async def telegram_polling_loop():
    """Background task: poll Telegram channels via web scraping."""
    print("📱 Starting Telegram channel scraping (no auth needed)...")
    
    # Initial fetch for all channels — load up to 250 pages (~5000 messages) or 12h of history
    INIT_PAGES = 250
    cutoff_12h = datetime.now(local_tz) - timedelta(hours=12)
    for ch_name, ch_config in TELEGRAM_CHANNELS.items():
        try:
            messages = await scrape_telegram_channel(ch_name, ch_config, max_pages=INIT_PAGES, cutoff_dt=cutoff_12h)
            if messages:
                if ch_config["type"] == "forecast":
                    await process_shigurimsh_messages(messages, is_init=True)
                elif ch_config["type"] == "official_alert":
                    await process_pikud_haoref_messages(messages, is_init=True)
                telegram_initialized[ch_name] = True
                print(f"✅ {ch_config['label']}: loaded {len(messages)} messages")
        except Exception as e:
            print(f"⚠️ Error initializing {ch_name}: {e}")
    
    # Continuous polling — only latest page
    while True:
        await asyncio.sleep(TELEGRAM_POLL_INTERVAL)
        for ch_name, ch_config in TELEGRAM_CHANNELS.items():
            try:
                messages = await scrape_telegram_channel(ch_name, ch_config, max_pages=1)
                if messages:
                    if ch_config["type"] == "forecast":
                        await process_shigurimsh_messages(messages, is_init=False)
                    elif ch_config["type"] == "official_alert":
                        await process_pikud_haoref_messages(messages, is_init=False)
            except Exception as e:
                print(f"⚠️ Error polling {ch_name}: {e}")

# ==========================
# Persistent Alert System (30-minute active window)
# ==========================
ALERT_PERSIST_SECONDS = 30 * 60  # 30 minutes
persistent_alerts = {}  # alert_key -> {alert_obj, first_seen, last_seen}
telegram_pikud_active_alerts = []  # Track alerts from Pikud HaOref Telegram

def add_persistent_alert(alert_obj):
    """Add or refresh a persistent alert. Alerts stay active for 30 minutes."""
    key = alert_obj["id"]
    now = datetime.now(local_tz)
    
    if key in persistent_alerts:
        persistent_alerts[key]["last_seen"] = now
        persistent_alerts[key]["alert"] = alert_obj
    else:
        persistent_alerts[key] = {
            "alert": alert_obj,
            "first_seen": now,
            "last_seen": now,
        }

def get_all_active_alerts():
    """Get all alerts that are still within their 30-minute persistence window."""
    now = datetime.now(local_tz)
    active = []
    expired_keys = []
    
    for key, entry in persistent_alerts.items():
        age = (now - entry["first_seen"]).total_seconds()
        if age <= ALERT_PERSIST_SECONDS:
            active.append(entry["alert"])
        else:
            expired_keys.append(key)
    
    # Clean up expired
    for key in expired_keys:
        del persistent_alerts[key]
    
    return active

def has_active_missile_alert():
    """Check if there's any active missile alert (category 1) - used to keep alerts until next missile event."""
    for entry in persistent_alerts.values():
        if entry["alert"].get("category") == 1:
            age = (datetime.now(local_tz) - entry["first_seen"]).total_seconds()
            if age <= ALERT_PERSIST_SECONDS:
                return True
    return False

# ==========================
# Pikud HaOref Polling Logic
# ==========================
oref_error_logged = False  # Log Oref error only once
oref_first_success = False  # Log first successful fetch
ACTIVE_ALERT_WINDOW_SECONDS = 120  # Alerts within last 2 minutes are considered "active" from Oref API

async def fetch_oref_data():
    """Fetch alert data from Oref history API (works internationally, unlike Alerts.json).
    
    Uses alerts-history.oref.org.il with mode=2 (last month history) which is NOT geo-blocked.
    Detects "active" alerts as those within the last 2 minutes from the history feed.
    Also accumulates history data for stats.
    """
    global oref_active_alerts, oref_last_alert_ids, oref_recent_history, today_real_alerts, oref_error_logged, oref_first_success
    
    now = datetime.now(local_tz)
    
    async with httpx.AsyncClient(verify=False, timeout=15, follow_redirects=True) as http_client:
        try:
            resp = await http_client.get(
                OREF_HISTORY_URL,
                headers=OREF_HISTORY_HEADERS
            )
            body = resp.text.strip().replace('\ufeff', '').replace('\x00', '')
            
            if not body:
                if not oref_error_logged:
                    print(f"⚠️ Oref History API returned empty body (status {resp.status_code})")
                    oref_error_logged = True
                return
            
            try:
                data = json.loads(body)
            except json.JSONDecodeError as je:
                if not oref_error_logged:
                    print(f"⚠️ Oref History API JSON parse error: {je}")
                    print(f"   Status: {resp.status_code}, Content-Type: {resp.headers.get('content-type', 'N/A')}")
                    print(f"   Body preview (first 200 chars): {body[:200]}")
                    oref_error_logged = True
                return
            
            if not isinstance(data, list):
                return
            
            if not oref_first_success:
                oref_first_success = True
                print(f"✅ Oref History API working! Got {len(data)} alert records")
            
            history_items = []
            active_cities = {}  # category -> {title, cat_info, cities: []}
            
            for item in data:
                city = (item.get("data") or "").strip()
                if not city or "בדיקה" in city:
                    continue
                
                # alertDate is ISO like "2025-03-04T22:43:00"
                # time is exact like "22:43:06"
                alert_date_iso = item.get("alertDate", "")
                exact_time = item.get("time", "")
                title = item.get("category_desc", "")
                cat = int(item.get("category", 1))
                cat_info = ALERT_CATEGORIES.get(cat, ALERT_CATEGORIES.get(1, {}))
                
                # Normalize alertDate to "YYYY-MM-DD HH:MM:SS" format
                if exact_time and alert_date_iso:
                    date_part = alert_date_iso[:10]
                    alert_date_normalized = f"{date_part} {exact_time}"
                elif alert_date_iso:
                    alert_date_normalized = alert_date_iso.replace("T", " ")
                else:
                    continue
                
                alert_item = {
                    "alertDate": alert_date_normalized,
                    "title": title,
                    "city": city,
                    "category": cat,
                    "category_info": cat_info,
                }
                history_items.append(alert_item)
                
                # Parse timestamp to check recency
                try:
                    alert_dt = datetime.strptime(alert_date_normalized, "%Y-%m-%d %H:%M:%S")
                    alert_dt = alert_dt.replace(tzinfo=local_tz)
                except Exception:
                    continue
                
                # --- Active alert detection: alerts within last 2 minutes ---
                age_seconds = (now - alert_dt).total_seconds()
                if 0 <= age_seconds <= ACTIVE_ALERT_WINDOW_SECONDS:
                    if cat not in active_cities:
                        active_cities[cat] = {"title": title, "cat_info": cat_info, "cities": []}
                    if city not in active_cities[cat]["cities"]:
                        active_cities[cat]["cities"].append(city)
            
            oref_recent_history = history_items
            
            # --- Process active alerts (from Oref API) and add to persistent system ---
            if active_cities:
                for cat, info in active_cities.items():
                    alert_key = f"oref_{cat}_{','.join(sorted(info['cities']))}"
                    
                    alert_obj = {
                        "id": alert_key,
                        "cities": info["cities"],
                        "title": info["title"],
                        "desc": "",
                        "category": cat,
                        "category_info": info["cat_info"],
                        "timestamp": now.isoformat(),
                        "source": "oref"
                    }
                    
                    # Add to 30-minute persistent alerts
                    add_persistent_alert(alert_obj)
                    
                    if alert_key not in oref_last_alert_ids:
                        oref_last_alert_ids.add(alert_key)
                        if len(oref_last_alert_ids) > 500:
                            oref_last_alert_ids = set(list(oref_last_alert_ids)[-200:])
                        
                        await manager.broadcast({
                            "msg_type": "oref_alert",
                            "alert": alert_obj,
                            "is_new": True
                        })
            
            # --- Check persistent alerts (30-min window) ---
            all_active = get_all_active_alerts()
            prev_had_alerts = len(oref_active_alerts) > 0
            oref_active_alerts = all_active
            
            # If all alerts have expired (were active, now empty), send clear
            if prev_had_alerts and not all_active:
                await manager.broadcast({
                    "msg_type": "oref_clear",
                    "alerts": []
                })
            
            # Reset error flag on success
            if oref_error_logged:
                oref_error_logged = False
                print("✅ Oref History API is now responding successfully")
        
        except Exception as e:
            if not oref_error_logged:
                print(f"⚠️ Oref History API error: {e}")
                oref_error_logged = True


async def oref_polling_loop():
    """Background task: poll Pikud HaOref every N seconds."""
    print("🛡️ מתחיל לאזין לפיקוד העורף (via history API)...")
    while True:
        await fetch_oref_data()
        await asyncio.sleep(OREF_POLL_INTERVAL)

@app.on_event("startup")
async def startup_event():
    # Start Telegram channel scraping (no auth needed)
    asyncio.create_task(telegram_polling_loop())
    
    # Start Pikud HaOref API polling in background
    asyncio.create_task(oref_polling_loop())

@app.get("/")
async def serve_frontend():
    """Serve the main HTML page."""
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "index.html")
    return FileResponse(html_path)

@app.get("/api/latest")
async def get_latest():
    """REST endpoint to get the latest alert data."""
    return latest_event

@app.get("/api/history")
async def get_history():
    """REST endpoint to get alert history."""
    return alert_history

@app.get("/api/oref-alerts")
async def get_oref_alerts():
    """REST endpoint to get current Pikud HaOref active alerts."""
    return oref_active_alerts

@app.get("/api/oref-history")
async def get_oref_history():
    """REST endpoint to get recent Pikud HaOref alert history."""
    return oref_recent_history

@app.get("/api/city-coords")
async def get_city_coords():
    """REST endpoint to get city coordinate mappings for the map."""
    return city_coords

@app.get("/api/stats")
async def get_stats():
    """REST endpoint to get today's forecast vs real alert statistics."""
    return compute_stats()

@app.get("/api/today-messages")
async def get_today_messages():
    """REST endpoint to get all today's channel messages."""
    return today_messages

@app.get("/api/debug")
async def get_debug():
    """Debug endpoint to inspect parsed data."""
    return {
        "today_forecasts_count": len(today_forecasts),
        "today_forecasts": today_forecasts[:20],
        "today_real_alerts_count": len(today_real_alerts),
        "today_real_alerts_sample": today_real_alerts[:30],
        "today_real_alerts_cat1": [a for a in today_real_alerts if a.get("category") == 1][:20],
        "today_messages_count": len(today_messages),
        "today_messages_sample": today_messages[:10],
        "telegram_initialized": telegram_initialized,
        "stats": compute_stats(),
    }

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    # Send initial state for BOTH data sources
    await websocket.send_json({
        "msg_type": "init",
        "telegram": latest_event,
        "oref_alerts": oref_active_alerts,
        "oref_history": oref_recent_history,
        "city_coords": city_coords,
    })
    try:
        while True:
            # Keep connection alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# Mount static files AFTER all routes so it doesn't override them
app.mount("/static", StaticFiles(directory=static_dir), name="static")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)