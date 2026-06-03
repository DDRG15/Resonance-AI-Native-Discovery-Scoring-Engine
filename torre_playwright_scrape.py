"""
Torre.ai Playwright scraper — intercepts API calls to capture job search results.
Run inside Docker: docker exec gema-gema-1 python /app/torre_playwright_scrape.py
Or locally:        python torre_playwright_scrape.py
"""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright, Route, Request

COOKIES_FILE = Path(__file__).parent / "cookies" / "torre.ai.json"
OUT_FILE     = Path(__file__).parent / "torre_jobs_raw.json"

SEARCHES = [
    ("FastAPI",          "(remote:yes and keywords:FastAPI)"),
    ("pytest",           "(remote:yes and keywords:pytest)"),
    ("Playwright",       "(remote:yes and keywords:Playwright)"),
    ("QA_automation",    "(remote:yes and keywords:QA automation)"),
    ("Python_backend",   "(remote:yes and keywords:Python backend)"),
    ("Python",           "(remote:yes and keywords:Python)"),
]

def load_cookies(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    result = []
    for c in raw:
        cookie = {
            "name":   c["name"],
            "value":  c["value"],
            "domain": c["domain"].lstrip("."),
            "path":   c.get("path", "/"),
        }
        if c.get("secure"):
            cookie["secure"] = True
        if c.get("sameSite") in ("strict", "lax", "none"):
            cookie["sameSite"] = c["sameSite"].capitalize()
        result.append(cookie)
    return result


async def scrape_search(page, label: str, query: str) -> list[dict]:
    url = f"https://torre.ai/search/jobs?q={quote(query)}"
    captured_jobs: list[dict] = []
    api_calls: list[str] = []

    async def handle_response(response):
        if response.status == 200 and "json" in response.headers.get("content-type", ""):
            url_lower = response.url.lower()
            if any(k in url_lower for k in ("opportunit", "jobs", "search", "solarr")):
                try:
                    body = await response.json()
                    api_calls.append(f"CAPTURED: {response.url}")
                    # Try to extract job list
                    for key in ("results", "opportunities", "hits", "items", "data"):
                        if key in body and isinstance(body.get(key), list):
                            captured_jobs.extend(body[key])
                            break
                        if key in body and isinstance(body.get(key), dict):
                            nested = body[key]
                            for nk in ("hits", "results"):
                                if nk in nested and isinstance(nested[nk], list):
                                    captured_jobs.extend(nested[nk])
                                    break
                    if not captured_jobs and isinstance(body, list):
                        captured_jobs.extend(body)
                except Exception:
                    pass

    page.on("response", handle_response)

    print(f"\n[{label}] Navigating to: {url}")
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
    except Exception:
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(5)

    page.remove_listener("response", handle_response)

    if api_calls:
        print(f"[{label}] API calls captured: {len(api_calls)}")
        for call in api_calls[:5]:
            print(f"  {call}")
    else:
        print(f"[{label}] No API calls captured — trying to extract from DOM")
        # Fallback: try to read job cards from the page
        jobs_from_dom = await page.evaluate("""() => {
            const cards = document.querySelectorAll('[class*="opportunity"], [class*="job-card"], [data-id]');
            return Array.from(cards).slice(0, 50).map(el => ({
                text: el.innerText.trim().slice(0, 200)
            }));
        }""")
        if jobs_from_dom:
            print(f"[{label}] Found {len(jobs_from_dom)} DOM elements")
            captured_jobs = jobs_from_dom

    print(f"[{label}] Jobs collected: {len(captured_jobs)}")
    return captured_jobs


async def main():
    if not COOKIES_FILE.exists():
        print(f"ERROR: {COOKIES_FILE} not found")
        return

    cookies = load_cookies(COOKIES_FILE)
    all_results: dict[str, list] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        )
        await context.add_cookies(cookies)
        page = await context.new_page()

        for label, query in SEARCHES:
            jobs = await scrape_search(page, label, query)
            all_results[label] = jobs

        await browser.close()

    OUT_FILE.write_text(json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n\nSaved to: {OUT_FILE}")

    # Print summary
    print("\n=== SUMMARY ===")
    for label, jobs in all_results.items():
        print(f"  {label}: {len(jobs)} results")


if __name__ == "__main__":
    asyncio.run(main())
