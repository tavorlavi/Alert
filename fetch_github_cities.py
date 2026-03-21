import urllib.request
import json
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

urls = [
    'https://raw.githubusercontent.com/Binternet/israel-cities/main/cities.json',
    'https://raw.githubusercontent.com/ilantnt/cities_coordinate-/main/cities.json',
    'https://raw.githubusercontent.com/hasadna/israel-cities-api/main/cities.json',
    'https://raw.githubusercontent.com/GabMic/israel-cities-and-streets-list/main/cities.json'
]

for u in urls:
    try:
        print(f"Trying {u}")
        req = urllib.request.Request(u, headers={'User-Agent': 'Mozilla/5.0'})
        res = urllib.request.urlopen(req, context=ctx)
        data = json.loads(res.read().decode('utf-8'))
        
        print(f"Loaded {len(data)} cities from {u}.")
        
        output_dict = {}
        for item in data:
            val_str = str(item.values())
            
            # Gabmic/Binternet structures usually have 'name' or 'english_name' and 'lat', 'lng'
            name = item.get("name") or item.get("hebrew") or item.get("שם_ישוב") or item.get("CityName") or item.get("name_he")
            
            # Get lat/lon
            lat = item.get("lat") or item.get("latitude") or item.get("Y")
            lng = item.get("lng") or item.get("lon") or item.get("longitude") or item.get("X")
            
            if name and lat and lng:
                output_dict[name.strip()] = [float(lat), float(lng)]
        
        if output_dict:
            with open("coords_dump.json", "w", encoding="utf-8") as f:
                json.dump(output_dict, f, ensure_ascii=False)
            print(f"Saved {len(output_dict)} coordinates to coords_dump.json from {u}")
            break
            
    except Exception as e:
        print(f"Failed {u}: {e}")
