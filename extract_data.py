import json
import re

def extract_js_object(content, var_name):
    pattern = rf'const {var_name}\s*=\s*({{.*?}});' if '{' in content else rf'const {var_name}\s*=\s*(\[.*?\]);'
    # Simplified regex for JS object extraction - in practice, we need to handle nested braces
    # But for these specific variables, I can use a simpler approach since I know the structure.
    
    start_marker = f'const {var_name} = '
    start_idx = content.find(start_marker)
    if start_idx == -1:
        return None
    
    start_idx += len(start_marker)
    
    # Track braces
    brace_count = 0
    bracket_count = 0
    end_idx = -1
    
    for i in range(start_idx, len(content)):
        char = content[i]
        if char == '{': brace_count += 1
        elif char == '}': brace_count -= 1
        elif char == '[': bracket_count += 1
        elif char == ']': bracket_count -= 1
        
        if brace_count == 0 and bracket_count == 0 and char in (';', '\n', ','):
            # Potential end
            if i > start_idx + 1:
                end_idx = i
                break
    
    if end_idx == -1:
        return None
        
    js_str = content[start_idx:end_idx].strip()
    if js_str.endswith(';'): js_str = js_str[:-1].strip()
    
    # Basic conversion from JS-like object to JSON
    # This is tricky because JS isn't strict JSON (unquoted keys, etc.)
    # I'll use a hacky way or just manually fix the strings since I have the source.
    
    # For CITY_COORDS, REGION_COORDS, CITY_TO_REGION:
    # They have quoted keys mostly, but let's be careful.
    
    return js_str

with open('index.html', 'r', encoding='utf-8') as f:
    content = f.read()

city_coords_raw = extract_js_object(content, 'CITY_COORDS')
region_coords_raw = extract_js_object(content, 'REGION_COORDS')
region_polygons_raw = extract_js_object(content, 'REGION_POLYGONS')
city_to_region_raw = extract_js_object(content, 'CITY_TO_REGION')

# Manual cleanup to make it JSON compatible
def to_json_val(raw):
    if not raw: return None
    # Replace single quotes with double quotes
    raw = raw.replace("'", '"')
    # Quote keys that are not quoted
    # raw = re.sub(r'(\s*)(\w+):', r'\1"\2":', raw)
    # Actually most keys in the HTML are already quoted because they are Hebrew strings.
    # Exception might be the region keys like NORTH, CENTER etc.
    raw = re.sub(r'(\s*)(NORTH|CENTER|SOUTH|JERUSALEM|WEST_BANK):', r'\1"\2":', raw)
    
    # Remove trailing commas before closing braces/brackets
    raw = re.sub(r',\s*([}\]])', r'\1', raw)
    
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"Error parsing: {e}")
        print(f"Raw snippet: {raw[:100]}...")
        return None

city_coords = to_json_val(city_coords_raw)
region_coords = to_json_val(region_coords_raw)
region_polygons = to_json_val(region_polygons_raw)
city_to_region = to_json_val(city_to_region_raw)

# Load existing high-precision polygons
with open('regional_coords_final.json', 'r', encoding='utf-8') as f:
    final_polys = json.load(f)

# Prefer high-precision polygons from final_polys if they exist
for key in final_polys:
    region_polygons[key] = final_polys[key]

# Build final output
output = {
    "CITY_COORDS": city_coords,
    "REGION_COORDS": region_coords,
    "REGION_POLYGONS": region_polygons,
    "CITY_TO_REGION": city_to_region
}

with open('regional_coords_final.json', 'w', encoding='utf-8') as f:
    json.dump(output, f, ensure_ascii=False, indent=2)

print("Successfully merged data into regional_coords_final.json")
