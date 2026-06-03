import json, requests, re
from pathlib import Path

cookies = {c['name']: c['value'] for c in json.loads(Path('cookies/torre.ai.json').read_text())}
s = requests.Session()
s.cookies.update(cookies)
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'text/html,application/xhtml+xml',
    'Referer': 'https://torre.ai/'
})

r = s.get('https://torre.ai/search/jobs?q=%28remote%3Ayes+and+keywords%3APython%29', timeout=15)
html = r.text
print(f'Page HTTP: {r.status_code}, HTML length: {len(html)}')

# Look for any torre subdomains or API hints
subdomains = re.findall(r'https?://([a-z0-9\-]+)\.torre\.ai', html)
unique_sub = sorted(set(subdomains))
print(f'\nSubdomains found ({len(unique_sub)}):')
for sub in unique_sub:
    print(f'  {sub}.torre.ai')

# Look for __NUXT__ or __NEXT_DATA__ or window config objects
nuxt_match = re.search(r'window\.__NUXT__\s*=\s*(\{.{0,500})', html)
next_match  = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.{0,1000})', html)
config_match = re.search(r'\"gatewayUrl\":\s*\"([^\"]+)\"', html)
api_match    = re.search(r'\"apiUrl\":\s*\"([^\"]+)\"', html)

if nuxt_match:
    print(f'\nNUXT config snippet: {nuxt_match.group(1)[:300]}')
if next_match:
    print(f'\nNEXT_DATA snippet: {next_match.group(1)[:300]}')
if config_match:
    print(f'\ngatewayUrl: {config_match.group(1)}')
if api_match:
    print(f'\napiUrl: {api_match.group(1)}')

# Try likely subdomain API endpoints
print('\nProbing API subdomain endpoints:')
test_endpoints = [
    'https://api.torre.ai/opportunities/_search',
    'https://services.torre.ai/opportunities/_search',
    'https://gateway.torre.ai/api/opportunities/_search',
    'https://torre.ai/api/v1/opportunities/search',
    'https://torre.ai/api/v2/opportunities/search',
    'https://torre.ai/api/opportunities/search',
]
for url in test_endpoints:
    try:
        resp = s.post(url, json={"q": "Python", "size": 5}, timeout=8)
        print(f'  POST {url} -> {resp.status_code} | content-type: {resp.headers.get("content-type","?")}')
        if resp.status_code == 200 and 'json' in resp.headers.get('content-type',''):
            print(f'    FOUND JSON! keys: {list(resp.json().keys())[:8]}')
    except Exception as e:
        print(f'  POST {url} -> ERR: {type(e).__name__}')
