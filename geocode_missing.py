import httpx
import json
import time

# Load existing map data
print("Loading regional_coords_final.json...")
try:
    with open("regional_coords_final.json", "r", encoding="utf-8") as f:
        map_data = json.load(f)
except Exception as e:
    print(f"Failed to load map data: {e}")
    exit(1)

city_coords = map_data.get("CITY_COORDS", {})

# High-priority missing settlements identified or generally needed
missing_targets = [
    "ניר עוז", "עין השלושה", "נחל עוז", "כפר עזה", "בארי", "כיסופים", "מגן", 
    "רעים", "נירים", "עלומים", "סעד", "ארז", "כרמיה", "זיקים", "ניר יצחק", 
    "סופה", "חולית", "כרם שלום", "יבול", "נווה", "שלומית", "יתד", "פרי גן"
]

added = 0
headers = {'User-Agent': 'AlertMapGeocage/1.0 (tavorlavi@example.com)'}

for i, target in enumerate(missing_targets):
    if target in city_coords:
        print(f"Skipping term {i}, already exists.")
        continue
        
    print(f"Geocoding term {i}...")
    try:
        # Nominatim API
        resp = httpx.get(
            f"https://nominatim.openstreetmap.org/search?q={target}, ישראל&format=json&limit=1",
            headers=headers,
            timeout=15.0
        )
        data = resp.json()
        if data and len(data) > 0:
            lat = float(data[0]["lat"])
            lon = float(data[0]["lon"])
            city_coords[target] = [lat, lon]
            print(f" -> Success: {lat}, {lon}")
            added += 1
        else:
            print(f" -> Not found in Nominatim.")
            
        # Nominatim asks for 1 req/sec max rate
        time.sleep(1.1)
    except Exception as e:
        print(f" -> Error: {e}")

if added > 0:
    map_data["CITY_COORDS"] = city_coords
    try:
        with open("regional_coords_final.json", "w", encoding="utf-8") as f:
            json.dump(map_data, f, ensure_ascii=False, separators=(',', ':'))
        print(f"Successfully saved {added} new coordinates to regional_coords_final.json!")
    except Exception as e:
        print(f"Failed to save: {e}")
else:
    print("No new coordinates added.")
