#!/usr/bin/env python3
"""
Quick Torre.ai job scraper — uses session cookies to search Python remote roles.
Usage:  python quick_torre_scrape.py
Output: torre_results.json  +  printed summary
"""
import json
import sys
from pathlib import Path
import requests

COOKIES_FILE = Path(__file__).parent / "cookies" / "torre.ai.json"
OUT_FILE     = Path(__file__).parent / "torre_results.json"

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":       "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin":       "https://torre.ai",
    "Referer":      "https://torre.ai/search/jobs",
}

SEARCH_PAYLOADS = [
    # Format A — Solarr query string (mirrors the URL query param)
    {
        "url": "https://torre.ai/api/opportunities/_search",
        "body": {
            "q": "(remote:yes and keywords:Python)",
            "size": 100,
            "from": 0,
        },
    },
    # Format B — structured AND filters
    {
        "url": "https://torre.ai/api/opportunities/_search",
        "body": {
            "and": [
                {"remote": "yes"},
                {"skill": {"term": "Python", "experience": "potential-to-develop"}},
            ],
            "size": 100,
            "from": 0,
            "aggregate": False,
        },
    },
    # Format C — torre search v2 endpoint
    {
        "url": "https://torre.ai/api/solarr/search",
        "body": {
            "query": "(remote:yes and keywords:Python)",
            "size": 100,
            "offset": 0,
            "searchType": "opportunities",
        },
    },
]


def load_cookies(path: Path) -> dict[str, str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in data}


def try_search(session: requests.Session, endpoint: dict) -> dict | None:
    try:
        r = session.post(endpoint["url"], json=endpoint["body"], timeout=20)
        print(f"  {endpoint['url']} -> HTTP {r.status_code}")
        if r.status_code == 200:
            return r.json()
        return None
    except Exception as e:
        print(f"  {endpoint['url']} -> ERROR: {e}")
        return None


def extract_jobs(raw: dict) -> list[dict]:
    """Normalize response regardless of which endpoint format succeeded."""
    jobs = []
    # Try common keys Torre uses
    for key in ("results", "opportunities", "hits", "items", "data"):
        if key in raw:
            candidate = raw[key]
            if isinstance(candidate, list):
                jobs = candidate
                break
            if isinstance(candidate, dict) and "hits" in candidate:
                jobs = candidate["hits"]
                break
    return jobs


def format_job(job: dict) -> str:
    title   = job.get("objective") or job.get("title") or "?"
    company = job.get("organizations", [{}])[0].get("name", "?") if job.get("organizations") else "?"
    salary  = job.get("compensation", {}) or {}
    min_s   = salary.get("minAmount") or salary.get("min")
    max_s   = salary.get("maxAmount") or salary.get("max")
    curr    = salary.get("currency", "USD")
    period  = salary.get("periodicity", "")
    sal_str = f"{curr} {min_s}-{max_s}/{period}" if min_s else "Hidden"
    skills  = [s.get("name", "") for s in (job.get("skills") or [])[:6]]
    url_id  = job.get("id") or job.get("publicId") or ""
    url     = f"https://torre.ai/jobs/{url_id}" if url_id else ""
    return (
        f"  [{title}] @ {company}\n"
        f"    Salary: {sal_str}\n"
        f"    Skills: {', '.join(skills)}\n"
        f"    URL:    {url}"
    )


def main() -> None:
    if not COOKIES_FILE.exists():
        print(f"ERROR: {COOKIES_FILE} not found. Save cookies there first.")
        sys.exit(1)

    cookies = load_cookies(COOKIES_FILE)
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(HEADERS)

    print("Trying Torre.ai search endpoints...\n")
    raw = None
    for ep in SEARCH_PAYLOADS:
        raw = try_search(session, ep)
        if raw:
            print(f"\nGot response from: {ep['url']}\n")
            break

    if raw is None:
        print("\nAll API endpoints failed — Torre.ai may require Playwright.")
        print("Next step: docker exec into gema-gema-1 and run playwright scraper.")
        sys.exit(1)

    # Save full raw response
    OUT_FILE.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Full response saved to: {OUT_FILE}\n")

    jobs = extract_jobs(raw)
    print(f"Total jobs found: {len(jobs)}\n")
    print("=" * 60)

    if not jobs:
        print("Response structure (top-level keys):", list(raw.keys()))
        print("\nRaw snippet:")
        print(json.dumps(raw, indent=2, ensure_ascii=False)[:2000])
        return

    for job in jobs:
        print(format_job(job))
        print()


if __name__ == "__main__":
    main()
