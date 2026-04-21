import requests
import json
import os

api_key = os.environ.get("URLGENIUS_API_KEY")
BASE = "https://api.urlgeni.us/api/v2"
headers = {"api-key": api_key}

# Get first link ID from the list
print("=== FETCHING FIRST LINK ID ===")
r = requests.get(f"{BASE}/links", headers=headers, params={"page": 1}, timeout=15)
data = r.json()
links = data.get('links', [])
if not links:
    print("No links returned"); exit()
first = links[0]
link_id = first['id']
print(f"First link: id={link_id}, url={first.get('url')}")
print(f"Fields in list response: {list(first.keys())}")

# Fetch single link by ID
print(f"\n=== GET /links/{link_id} ===")
r2 = requests.get(f"{BASE}/links/{link_id}", headers=headers, timeout=10)
print(f"Status: {r2.status_code}")
print(f"Raw (first 2000 chars):\n{r2.text[:2000]}")
try:
    d2 = r2.json()
    print(f"\nTop-level keys: {list(d2.keys()) if isinstance(d2, dict) else type(d2)}")
    if isinstance(d2, dict):
        for k, v in d2.items():
            if not isinstance(v, (dict, list)):
                print(f"  {k}: {v}")
            elif isinstance(v, dict):
                print(f"  {k}: {v}")
            else:
                print(f"  {k}: [list of {len(v)}]")
except Exception as e:
    print(f"JSON error: {e}")

# Also check if /links supports a 'stats=true' or 'include=stats' param
print("\n=== TRYING ?page=1&include=stats ===")
r3 = requests.get(f"{BASE}/links", headers=headers, params={"page": 1, "include": "stats"}, timeout=15)
d3 = r3.json()
sample = (d3.get('links') or [{}])[0]
print(f"Fields with include=stats: {list(sample.keys())}")
