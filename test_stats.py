import urllib.request, json

r = urllib.request.urlopen('http://localhost:8000/api/stats')
d = json.loads(r.read())
print("=== Summary ===")
print(json.dumps(d['summary'], indent=2, ensure_ascii=False))
print(f"\n=== Comparisons ({len(d.get('comparisons',[]))}) ===")
for c in d.get('comparisons', []):
    print(f"  {c['forecast_time']} -> {c.get('real_time','none')} matched={c['matched']} diff={c.get('diff_minutes')}")
print(f"\n=== Unmatched Alerts ({len(d.get('unmatched_alerts',[]))}) ===")
for u in d.get('unmatched_alerts', []):
    print(f"  {u['real_time']} - {u['title']} ({u['cities_count']} cities)")

# Also check oref-history count
r2 = urllib.request.urlopen('http://localhost:8000/api/oref-history')
history = json.loads(r2.read())
print(f"\n=== Oref History Items: {len(history)} ===")
if history:
    print(f"  First: {history[0].get('alertDate')} - {history[0].get('city')}")
    print(f"  Last:  {history[-1].get('alertDate')} - {history[-1].get('city')}")
