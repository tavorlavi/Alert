import httpx
import json

query = """
SELECT ?itemLabel ?coord
WHERE {
  VALUES ?type { wd:Q486972 wd:Q184288 wd:Q3329972 wd:Q12558661 wd:Q10864048 wd:Q515 wd:Q2018151 wd:Q689030 }
  ?item wdt:P31 ?type.
  ?item wdt:P17 wd:Q801.
  ?item wdt:P625 ?coord.
  SERVICE wikibase:label { bd:serviceParam wikibase:language "he". }
}
"""

print("Querying Wikidata for Israel coordinates...")
try:
    resp = httpx.get(
        "https://query.wikidata.org/sparql",
        params={'query': query, 'format': 'json'},
        timeout=30.0,
        headers={'User-Agent': 'AlertMapGeocage/1.0 (tavorlavi@example.com)'}
    )
    
    if resp.status_code == 200:
        results = resp.json()["results"]["bindings"]
        print(f"Got {len(results)} cities from Wikidata!")
        
        # Load existing map data
        with open("regional_coords_final.json", "r", encoding="utf-8") as f:
            map_data = json.load(f)
            
        city_coords = map_data.get("CITY_COORDS", {})
        
        parsed = 0
        for b in results:
            name = b["itemLabel"]["value"]
            coord = b["coord"]["value"] # "Point(34.8 32.1)"
            
            # Wikidata cord format: Point(LON LAT)
            if coord.startswith("Point("):
                lon, lat = coord[6:-1].split(" ")
                
                # Check if it looks like English/ID if hebrew label is missing
                if not any("\u0590" <= c <= "\u05ea" for c in name):
                    continue
                
                name_clean = name.replace(" (ישוב)", "").replace(" (עיר)", "").replace(" (קיבוץ)", "").strip()
                
                if name_clean not in city_coords:
                    city_coords[name_clean] = [float(lat), float(lon)]
                    parsed += 1
                    
        map_data["CITY_COORDS"] = city_coords
        
        with open("regional_coords_final.json", "w", encoding="utf-8") as f:
            json.dump(map_data, f, ensure_ascii=False, separators=(',', ':'))
            
        print(f"Successfully added {parsed} NEW kibbutzim and cities to CITY_COORDS mapping!")
    else:
        print("Failed SPARQL:", resp.status_code, resp.text)
        
except Exception as e:
    print("Error:", e)
