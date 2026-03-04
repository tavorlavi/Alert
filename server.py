import re
import json
import asyncio
import httpx
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from dateutil import tz
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import os
import base64

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
# Configuration: reads from env vars
# ==========================
api_id = int(os.environ.get("TELEGRAM_API_ID", "0"))
api_hash = os.environ.get("TELEGRAM_API_HASH", "")
channel_username = os.environ.get("TELEGRAM_CHANNEL", "shigurimsh")
PORT = int(os.environ.get("PORT", "8000"))

# Restore Telegram session from env var (for cloud deployment)
TELEGRAM_SESSION_B64 = os.environ.get("TELEGRAM_SESSION", "")
if TELEGRAM_SESSION_B64 and not os.path.exists("session.session"):
    with open("session.session", "wb") as f:
        f.write(base64.b64decode(TELEGRAM_SESSION_B64))
    print("✅ Telegram session restored from env var")
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
OREF_HISTORY_URL = "https://alerts-history.oref.org.il/Shared/Ajax/GetAlarmsHistory.aspx?lang=he&mode=3"
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
    13: {"type": "terrorist", "icon": "🔫", "label": "חדירת מחבלים", "color": "red"},
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
# Fetch today's Telegram history on startup
# ==========================
async def fetch_today_telegram_messages():
    """Fetch all messages from the channel for today and extract forecasts."""
    global today_forecasts, today_messages
    
    now = datetime.now(local_tz)
    cutoff = now - timedelta(hours=12)
    
    try:
        entity = await client.get_entity(channel_username)
        messages = await client.get_messages(entity, limit=200, offset_date=None)
        
        today_msgs = []
        today_fcs = []
        
        for msg in messages:
            if not msg.text:
                continue
            # Convert message date to local timezone
            msg_date = msg.date.astimezone(local_tz)
            if msg_date < cutoff:
                break  # Messages are in reverse chronological order
            
            today_msgs.append({
                "text": msg.text,
                "date": msg_date.isoformat(),
                "id": msg.id,
            })
            
            # Try to extract a time from every message
            time_str = extract_time_from_text(msg.text)
            if time_str:
                target_time = get_target_datetime(time_str, reference_time=msg_date)
                today_fcs.append({
                    "text": msg.text,
                    "target_time": target_time.isoformat(),
                    "received_at": msg_date.isoformat(),
                })
                
                # Also add to alert_history if not already there
                exists = any(h.get("target_time") == target_time.isoformat() and h.get("text") == msg.text for h in alert_history)
                if not exists:
                    alert_history.append({
                        "text": msg.text,
                        "target_time": target_time.isoformat(),
                        "received_at": msg_date.isoformat()
                    })
        
        # Sort: newest first
        today_msgs.reverse()
        today_messages = today_msgs
        today_forecasts = today_fcs
        
        # Sort alert_history newest first
        alert_history.sort(key=lambda x: x.get("received_at", ""), reverse=True)
        if len(alert_history) > MAX_HISTORY:
            del alert_history[MAX_HISTORY:]
        
        print(f"📊 נמצאו {len(today_messages)} הודעות היום, {len(today_forecasts)} צפי")
        
    except Exception as e:
        print(f"⚠️ שגיאה בטעינת היסטוריית הודעות: {e}")


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
    alert_rounds = []
    for item in real_alerts_recent:
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
                "category": item.get("category", 1),
            })
    
    # Sort rounds by time
    alert_rounds.sort(key=lambda r: r["time"])
    
    # Find oldest alert time (to know data coverage boundary)
    oldest_alert_time = alert_rounds[0]["time"] if alert_rounds else None
    
    # Build comparison: match each forecast to the closest real alert
    comparisons = []
    used_rounds = set()
    
    for fc in today_forecasts:
        try:
            fc_time = datetime.fromisoformat(fc["target_time"])
            if fc_time.tzinfo is None:
                fc_time = fc_time.replace(tzinfo=local_tz)
        except Exception:
            continue
        
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
            "forecast_time": fc_time.strftime("%H:%M"),
            "received_at": fc.get("received_at", ""),
        }
        
        if best_match and best_diff is not None and best_diff < 7200:  # Within 2 hours
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
    
    # Unmatched real alerts (alerts with no forecast) – only category 1 (ירי רקטות וטילים)
    unmatched_alerts = []
    for idx, rnd in enumerate(alert_rounds):
        if idx not in used_rounds and rnd.get("category") == 14:
            unmatched_alerts.append({
                "real_time": rnd["time_str"],
                "title": rnd["title"],
                "cities_count": rnd["count"],
                "cities": rnd["cities"][:5],
            })
    
    # Summary stats
    total_forecasts = len(today_forecasts)
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


# Telegram Client setup
client = TelegramClient("session", api_id, api_hash)

# ==========================
# Pikud HaOref Polling Logic
# ==========================
oref_error_logged = False  # Log Oref error only once
ACTIVE_ALERT_WINDOW_SECONDS = 120  # Alerts within last 2 minutes are considered "active"

async def fetch_oref_data():
    """Fetch alert data from Oref history API (works internationally, unlike Alerts.json).
    
    Uses alerts-history.oref.org.il with mode=3 (24h history) which is NOT geo-blocked.
    Detects "active" alerts as those within the last 2 minutes from the history feed.
    Also accumulates history data for stats.
    """
    global oref_active_alerts, oref_last_alert_ids, oref_recent_history, today_real_alerts, oref_error_logged
    
    now = datetime.now(local_tz)
    
    async with httpx.AsyncClient(verify=False, timeout=15) as http_client:
        try:
            resp = await http_client.get(
                OREF_HISTORY_URL,
                headers=OREF_HISTORY_HEADERS
            )
            body = resp.text.strip().replace('\ufeff', '').replace('\x00', '')
            
            if not body:
                return
            
            data = resp.json()
            
            if not isinstance(data, list):
                return
            
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
                
                # --- Accumulate alerts from last 12 hours for stats ---
                cutoff_12h = now - timedelta(hours=12)
                if alert_dt >= cutoff_12h:
                    exists = any(
                        a["alertDate"] == alert_date_normalized and a["city"] == city
                        for a in today_real_alerts
                    )
                    if not exists:
                        today_real_alerts.append(alert_item)
            
            oref_recent_history = history_items
            
            # --- Process active alerts ---
            if active_cities:
                new_alerts = []
                for cat, info in active_cities.items():
                    alert_key = f"{cat}_{','.join(sorted(info['cities']))}"
                    
                    alert_obj = {
                        "id": alert_key,
                        "cities": info["cities"],
                        "title": info["title"],
                        "desc": "",
                        "category": cat,
                        "category_info": info["cat_info"],
                        "timestamp": now.isoformat()
                    }
                    new_alerts.append(alert_obj)
                    
                    if alert_key not in oref_last_alert_ids:
                        oref_last_alert_ids.add(alert_key)
                        if len(oref_last_alert_ids) > 500:
                            oref_last_alert_ids = set(list(oref_last_alert_ids)[-200:])
                        
                        await manager.broadcast({
                            "msg_type": "oref_alert",
                            "alert": alert_obj,
                            "is_new": True
                        })
                
                oref_active_alerts = new_alerts
            else:
                if oref_active_alerts:
                    oref_active_alerts = []
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
    # Connect to Telegram in the background
    telegram_ok = False
    try:
        await client.connect()
        is_authorized = await client.is_user_authorized()
        
        if not is_authorized:
            print("❌ Telegram לא מאומת. מחק session.session והרץ auth_telegram.py")
            telegram_ok = False
        else:
            print("✅ Telegram מחובר")
            telegram_ok = True
    except Exception as e:
        print(f"❌ Telegram connection failed: {e}")

    # Fetch today's messages from the channel (only if authorized)
    if telegram_ok:
        await fetch_today_telegram_messages()

    if telegram_ok:
        @client.on(events.NewMessage(chats=channel_username))
        async def handler(event):
            global latest_event
            text = event.raw_text
            msg_date = event.date.astimezone(local_tz) if event.date else datetime.now(local_tz)

            # Add to today's messages
            today_messages.insert(0, {
                "text": text,
                "date": msg_date.isoformat(),
                "id": event.id,
            })

            time_str = extract_time_from_text(text)
            if time_str:

                target_time = get_target_datetime(time_str)
                
                # Update our state
                latest_event = {
                    "text": text,
                    "target_time": target_time.isoformat(),
                    "has_data": True
                }

                # Save to history
                history_entry = {
                    "text": text,
                    "target_time": target_time.isoformat(),
                    "received_at": datetime.now(local_tz).isoformat()
                }
                alert_history.insert(0, history_entry)
                if len(alert_history) > MAX_HISTORY:
                    alert_history.pop()
                
                # Add to today's forecasts for stats
                today_forecasts.append({
                    "text": text,
                    "target_time": target_time.isoformat(),
                    "received_at": msg_date.isoformat(),
                })
                
                # Broadcast to all connected clients
                await manager.broadcast({
                    "msg_type": "telegram_timing",
                    **latest_event
                })

        # Run Telethon in the background without blocking FastAPI
        asyncio.create_task(client.run_until_disconnected())
    
    # Start Pikud HaOref polling in background
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
    # Note: Make sure you have authenticated your Telegram session once 
    # normally before running uvicorn, so the 'session.session' file exists!
    uvicorn.run(app, host="0.0.0.0", port=PORT)