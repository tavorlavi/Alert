import re
from datetime import datetime, timedelta

def parse_message_to_alerts(text, default_time=None):
    """
    Given a message text, parse all independent alerts.
    "זוהו שיגורים למרכז" -> location: מרכז, time: None
    "מרכז 3 דקות, צפון 6 דקות" ->
        location: מרכז, relative_time: 3 min -> 3*60s
        location: צפון, relative_time: 6 min -> 6*60s
    """
    
    # First, split the text by newlines or delimiters if there are multiple parts?
    # Actually, a single sentence might be "צפי 14:00 מגיע למרכז".
    pass

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

def extract_alerts_from_text(text):
    alerts = []
    exclude_words = {"שיגור", "שיגורים", "כעת", "אזעקות", "אזעקה", "יירוטים", "חזלש", "מלבנון", "מאיראן", "מעזה", "מתימן", "מעיראק", "מגיע"}
    
    lines = re.split(r'\n|\.', text)
    
    global_clock_time = None
    global_expected_text = None
    
    for line in lines:
        line = line.strip()
        if not line: continue
            
        if any(skip in line for skip in ["http://", "https://", "היכנסו", "פיקוד העורף", "ירי רקטות", "חדירת כלי", "חדירת מחבלים", "ללא התרעה", "מערכות ההגנה"]):
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
        line_clean = re.sub(r'[*_🚨✅⚠️\.]', '', line_clean)
        
        line_areas = []
        for part in re.split(r'[,/|\-\n]|\sו(?=[א-ת])', line_clean):
            area = re.sub(r'\(.*?\)', '', part).strip()
            words = [w for w in area.split() if w not in exclude_words]
            cleaned_area = " ".join(words).strip()
            # remove prefix that aren't parts of words (sometimes)
            cleaned_area = re.sub(r'^(ל|ב|אל\s)(?=[א-ת]{2,})', '', cleaned_area).strip()
            if cleaned_area and len(cleaned_area) >= 2:
                line_areas.append(cleaned_area)
                
        if line_areas:
            alerts.append({
                "areas": line_areas,
                "clock_time": line_clock_time or global_clock_time,
                "expected_time_text": line_expected_text or global_expected_text,
                "expected_seconds": _to_expected_seconds(line_expected_text or global_expected_text)
            })
            
    if not alerts and (global_clock_time or global_expected_text):
        alerts.append({
            "areas": [],
            "clock_time": global_clock_time,
            "expected_time_text": global_expected_text,
            "expected_seconds": _to_expected_seconds(global_expected_text)
        })
        
    # Also if we just have "זוהו שיגורים" without times but locations! It creates an alert without times. Great!
    
    return alerts

print("TEST 1")
print(extract_alerts_from_text("מרכז 3 דקות\nצפון 6 דקות"))
print("TEST 2")
print(extract_alerts_from_text("שיגור מלבנון למרכז\nללא התרעה מוקדמת"))
print("TEST 3")
print(extract_alerts_from_text("צפי 14:05\nמרכז ודרום"))
print("TEST 4")
print(extract_alerts_from_text("זוהו שיגורים לצפון, יכול לקחת 4.5 דקות"))
