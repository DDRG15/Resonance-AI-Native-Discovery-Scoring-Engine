import json, requests, re
from pathlib import Path

cookies = {c['name']: c['value'] for c in json.loads(Path('cookies/torre.ai.json').read_text())}
s = requests.Session()
s.cookies.update(cookies)
s.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://torre.ai/',
    'x-requested-with': 'XMLHttpRequest',
})

# 1 -- Try app.torre.ai
print('=== app.torre.ai probes ===')
app_endpoints = [
    ('POST', 'https://app.torre.ai/api/opportunities/_search',
     {"q": "(remote:yes and keywords:Python)", "size": 5, "from": 0}),
    ('GET',  'https://app.torre.ai/api/me', {}),
    ('POST', 'https://app.torre.ai/opportunities/_search',
     {"and": [{"remote": "yes"}, {"skill": {"term": "Python"}}], "size": 5}),
    ('GET',  'https://app.torre.ai/api/bios/me', {}),
]
for method, url, body in app_endpoints:
    try:
        if method == 'POST':
            r = s.post(url, json=body, timeout=8)
        else:
            r = s.get(url, timeout=8)
        ct = r.headers.get('content-type', '?')
        print(f'  {method} {url}')
        print(f'    -> {r.status_code} | {ct}')
        if 'json' in ct and r.status_code == 200:
            print(f'    KEYS: {list(r.json().keys())[:8]}')
        elif r.status_code in (200, 201):
            print(f'    BODY snippet: {r.text[:150]}')
    except Exception as e:
        print(f'  {method} {url} -> ERR: {type(e).__name__}: {e}')

# 2 -- Fetch the page and dump __NUXT__ initial state
print('\n=== SSR initial data ===')
r2 = s.get(
    'https://torre.ai/search/jobs?q=%28remote%3Ayes+and+keywords%3APython%29',
    timeout=15,
    headers={'Accept': 'text/html,application/xhtml+xml'}
)
html = r2.text

# Try to find JSON data blobs in the HTML
# Nuxt 2 pattern
nuxt = re.search(r'window\.__NUXT__\s*=\s*(.+?)</script>', html, re.DOTALL)
if nuxt:
    snippet = nuxt.group(1)[:2000]
    print(f'__NUXT__ found, length: {len(nuxt.group(1))}')
    # Look for "opportunities" or "jobs" data in it
    if 'opportunit' in snippet.lower() or 'jobs' in snippet.lower():
        print('  Contains job data!')
    print(f'  Snippet: {snippet[:500]}')
else:
    print('No __NUXT__ state found')

# Look for any JSON arrays with job objects
job_patterns = re.findall(r'"objective"\s*:\s*"([^"]+)"', html)
company_patterns = re.findall(r'"organizations"\s*:\s*\[\s*\{[^}]*"name"\s*:\s*"([^"]+)"', html)
if job_patterns:
    print(f'\nJob titles found in HTML ({len(job_patterns)}):')
    for t in job_patterns[:20]:
        print(f'  - {t}')
if company_patterns:
    print(f'\nCompanies found in HTML ({len(company_patterns)}):')
    for c in company_patterns[:10]:
        print(f'  - {c}')

if not job_patterns and not company_patterns:
    print('No job data in HTML -- pure client-side render (needs Playwright)')
    # Save a portion of the HTML for manual inspection
    Path('torre_page_snippet.html').write_text(html[:5000], encoding='utf-8')
    print('Saved first 5000 chars to torre_page_snippet.html for inspection')
