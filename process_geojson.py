import json
import urllib.request

url = "https://raw.githubusercontent.com/apache/superset/master/superset-frontend/plugins/legacy-plugin-chart-country-map/src/countries/israel.geojson"
with urllib.request.urlopen(url) as response:
    data = json.loads(response.read().decode())

regions = {}

for feature in data['features']:
    name = feature['properties']['NAME_1']
    geom = feature['geometry']
    
    if geom['type'] == 'Polygon':
        coords = [[p[1], p[0]] for p in geom['coordinates'][0]]
        # Use a list of polygons even if it's just one
        regions[name] = [coords]
    elif geom['type'] == 'MultiPolygon':
        all_polys = []
        for poly in geom['coordinates']:
            all_polys.append([[p[1], p[0]] for p in poly[0]])
        regions[name] = all_polys

# Big areas as lists of polygons
big_areas = {
    "NORTH": regions.get("HaZafon", []) + regions.get("Haifa", []),
    "CENTER": regions.get("HaMerkaz", []) + regions.get("Tel Aviv", []),
    "SOUTH": regions.get("HaDarom", []),
    "JERUSALEM": regions.get("Jerusalem", [])
}

with open("regional_coords_final.json", "w", encoding="utf-8") as f:
    json.dump(big_areas, f, ensure_ascii=False)
