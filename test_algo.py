import re

def extract_expected_time_text(text):
    """Extract expected duration expressions like '5 דקות' or '35 שניות'."""
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

texts_to_test = [
    "4.5 דקות",
    "עוד דקה",
    "75 שניות",
    "5 דקות"
]

for t in texts_to_test:
    ex = extract_expected_time_text(t)
    sec = _to_expected_seconds(ex)
    print(f"{t!r} -> {ex} -> {sec}s")

def extract_areas_from_text(text):
    areas = []
    seen = set()
    
    # Exclude common non-area words
    exclude_words = {"שיגור", "שיגורים", "כעת", "אזעקות", "יירוטים", "חזלש", "מעקב", "דיווח", "התרעה", "התרעות", "מלבנון", "מאיראן", "מעזה", "מתימן", "מעיראק"}
    
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if any(skip in line.lower() for skip in [
            "http://", "https://", "היכנסו", "פיקוד העורף", "ירי רקטות", "חדירת כלי", "חדירת מחבלים", "ללא התרעה"
        ]):
            continue
            
        # Remove numbers and time units
        line = re.sub(r'(?:(\d+(?:\.\d+)?)\s*)?(דקות|דקה|שניות|שניה)', '', line)
        line = re.sub(r'\d{1,2}:\d{2}', '', line)
        line = re.sub(r'צפי|משך|מגיע|עוד', '', line)
        
        # Remove markdown/emojis (basic)
        line = re.sub(r'[*_🚨✅⚠️]', '', line)
        
        for part in re.split(r'[,/|\-\n]|\sו(?=[א-ת])', line):
            # Clean up words like "ל", "ב", "אל" ?
            area = re.sub(r'\(.*?\)', '', part).strip()
            
            # Split by space and filter words
            words = [w for w in area.split() if w not in exclude_words and len(w) >= 2]
            area = " ".join(words)
            
            # Remove prefixes like "ל" or "ב" if it makes sense? Actually "למרכז" -> "מרכז", "לצפון" -> "צפון"
            # It's better to clean prefixes
            clean_area = re.sub(r'^(ל|ב|אל\s)(?=[א-ת]{2,})', '', area).strip()
            
            if not clean_area or len(clean_area) < 2:
                continue
                
            if clean_area in seen:
                continue
            seen.add(clean_area)
            areas.append(clean_area)
            
    return areas

print(extract_areas_from_text("שיגורים מלבנון לאזור מרכז/ לכיש"))
print(extract_areas_from_text("השיגור מלבנון עוד דקה אזעקה באזור אשקלון/ עוטף עזה"))
print(extract_areas_from_text("שיגור מאיראן לצפון, 3 דקות לאזעקה"))
print(extract_areas_from_text("שיגור כעת מלבנון למרכז\nללא התרעה מקדימה"))
print(extract_areas_from_text("שיגור מלבנון לצפת טבריה בצפון באר שבע"))
print(extract_areas_from_text("דרום ככל הנראה"))
print(extract_areas_from_text("אזעקות מקו רעננה עד רשלצ\nעוד 2 דקות"))

