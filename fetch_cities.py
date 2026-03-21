import json
import urllib.request
import ssl

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

try:
    print("Fetching OREF cities.json...")
    req = urllib.request.Request(
        "https://www.oref.org.il/WarningMessages/he/cities.json",
        headers={'User-Agent': 'Mozilla/5.0'}
    )
    with urllib.request.urlopen(req, context=ctx) as response:
        cities_data = json.loads(response.read().decode('utf-8'))
        
    city_to_region = {}
    for item in cities_data:
        city = item.get("name", "").strip()
        region = item.get("mixname", "").strip()
        if not city or not region: continue
        
        if region not in city_to_region:
            city_to_region[region] = []
        city_to_region[region].append(city)

    print("Mapping built. Regions:", len(city_to_region), "Total cities:", sum(len(x) for x in city_to_region.values()))

    print("Loading regional_coords_final.json...")
    map_file = "regional_coords_final.json"
    with open(map_file, "r", encoding="utf-8") as f:
        map_data = json.load(f)
        
    # We need to map the Oref 'mixname' to our internal Region IDs (e.g. district-1)
    region_keys = list(map_data.get("REGION_POLYGONS", {}).keys())
    
    # Actually, we can just save it as a direct map of OREF Region Name -> [cities]
    # And then getRegionID just needs to find which OREF Region Name it is, and we can map that to our polygon ID.
    # Wait! In index.html: `const hasPolygon = regionID && mapData.REGION_POLYGONS[regionID];`
    # So `id` MUST be exactly the key in `REGION_POLYGONS`!!
    
    # If REGION_POLYGONS keys are English ("South", "Center", "Otef") or Hebrew ("דרום", "מרכז")?
    # I will output the keys so I can map them properly.

    map_data["CITY_TO_REGION"] = city_to_region
    
    with open(map_file, "w", encoding="utf-8") as f:
        json.dump(map_data, f, ensure_ascii=False, separators=(',', ':'))
    print("Successfully patched regional_coords_final.json with CITY_TO_REGION!")

except Exception as e:
    print("Error:", e)
