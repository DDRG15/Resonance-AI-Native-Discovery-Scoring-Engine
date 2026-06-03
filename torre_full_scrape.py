"""
Torre.ai full scraper — one new page per keyword to avoid response-listener drift.
Run inside Docker: docker exec gema-gema-1 python /app/torre_full_scrape.py
Outputs: /app/torre_full_results.json  +  /app/torre_full_results.md
"""
import asyncio
import json
from pathlib import Path
from urllib.parse import quote

from playwright.async_api import async_playwright

COOKIES_FILE = Path("/app/cookies/torre.ai.json")
OUT_JSON     = Path("/app/torre_full_results.json")
OUT_MD       = Path("/app/torre_full_results.md")

KEYWORDS = [
    "FastAPI",
    "pytest",
    "Playwright",
    "QA automation",
    "Python backend",
    "Python",
    "asyncio",
    "Pydantic",
    "data integrity",
    "backend Python",
]

MY_SKILLS = {
    "python", "fastapi", "playwright", "pytest", "asyncio", "pydantic",
    "docker", "github actions", "sql", "sqlite", "rest api", "ci/cd",
    "selenium", "aiohttp", "pandas", "streamlit", "git", "linux",
    "data integrity", "sre", "reliability", "backend", "api testing",
    "unit testing", "automation", "devops", "testing", "scraping",
}

SEARCH_API_HOST = "search.torre.co"


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
        samesite = c.get("sameSite", "")
        if samesite.lower() in ("strict", "lax", "none"):
            cookie["sameSite"] = samesite.capitalize()
        result.append(cookie)
    return result


def extract_jobs_from_response(body: dict | list) -> list[dict]:
    if isinstance(body, list):
        return body
    for key in ("results", "opportunities", "hits", "items", "data"):
        val = body.get(key)
        if isinstance(val, list) and val:
            return val
        if isinstance(val, dict):
            for nk in ("hits", "results", "items"):
                if nk in val and isinstance(val.get(nk), list):
                    return val[nk]
    return []


def normalize_job(job: dict) -> dict:
    title   = job.get("objective") or job.get("title") or ""
    orgs    = job.get("organizations") or []
    company = orgs[0].get("name", "") if orgs else ""
    comp    = job.get("compensation") or {}
    min_s   = comp.get("minAmount") or comp.get("min")
    max_s   = comp.get("maxAmount") or comp.get("max")
    curr    = comp.get("currency", "USD")
    period  = comp.get("periodicity", "monthly")
    salary  = f"{curr} {min_s}-{max_s}/{period}" if min_s else "Hidden"
    skills  = [s.get("name", "") for s in (job.get("skills") or [])]
    jid     = job.get("id") or job.get("publicId") or ""
    url     = f"https://torre.ai/jobs/{jid}" if jid else ""
    remote  = job.get("remote", None)
    match_score = job.get("score") or job.get("torreMatch") or job.get("matchScore")
    return dict(
        title=title, company=company, salary=salary,
        skills=skills, url=url, remote=remote,
        match_score=match_score, raw_id=jid, raw=job,
    )


def skill_match_score(job: dict) -> tuple[int, list[str]]:
    skills_lower = [s.lower() for s in job["skills"]]
    title_lower  = job["title"].lower()
    matched = [s for s in skills_lower if any(ms in s for ms in MY_SKILLS)]
    title_hit = any(ms in title_lower for ms in MY_SKILLS)
    score = len(matched) + (2 if title_hit else 0)
    return score, matched


async def scrape_one_keyword(browser, cookies: list[dict], keyword: str) -> list[dict]:
    """Creates a fresh page per keyword to avoid response listener drift."""
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="es",
    )
    await context.add_cookies(cookies)
    page = await context.new_page()

    captured: list[dict] = []
    lock = asyncio.Lock()

    async def on_response(response):
        if SEARCH_API_HOST not in response.url:
            return
        if response.status != 200:
            return
        ct = response.headers.get("content-type", "")
        if "json" not in ct:
            return
        try:
            body = await response.json()
            jobs_raw = extract_jobs_from_response(body)
            async with lock:
                captured.extend(jobs_raw)
        except Exception:
            pass

    page.on("response", on_response)

    url = f"https://torre.ai/search/jobs?q={quote('(remote:yes and keywords:' + keyword + ')')}"
    print(f"  [{keyword}] Navigating...")
    try:
        await page.goto(url, wait_until="networkidle", timeout=35_000)
    except Exception:
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            await asyncio.sleep(6)
        except Exception as e:
            print(f"  [{keyword}] Navigation failed: {e}")

    # Extra wait to catch late XHR calls
    await asyncio.sleep(3)

    await context.close()

    normalized = [normalize_job(j) for j in captured if isinstance(j, dict) and j.get("objective")]
    print(f"  [{keyword}] -> {len(normalized)} jobs captured")
    return normalized


def build_markdown(all_results: dict[str, list[dict]]) -> str:
    lines = [
        "# Torre.ai Job Search — Full Results",
        "Date: 2026-05-20 | Filter: remote:yes | Scraper: Playwright + search.torre.co",
        "",
        "---",
        "",
        "## Quick navigation",
    ]
    for kw in all_results:
        count = len(all_results[kw])
        anchor = kw.lower().replace(" ", "-")
        lines.append(f"- [{kw}](#{anchor}) — {count} results")
    lines += ["", "---", ""]

    # Deduplicate across keywords
    seen_ids: set[str] = set()
    all_jobs_flat: list[tuple[str, dict]] = []

    for kw, jobs in all_results.items():
        lines += [f"## {kw}", f"*{len(jobs)} results*", ""]
        if not jobs:
            lines += ["No results for this keyword.", ""]
            continue
        for j in jobs:
            score, matched = skill_match_score(j)
            match_str = f"{j['match_score']}" if j["match_score"] else "?"
            remote_str = "Yes" if j["remote"] else ("No" if j["remote"] is False else "?")
            skill_list = ", ".join(j["skills"][:8]) if j["skills"] else "not listed"
            lines += [
                f"### {j['title']} @ {j['company']}",
                f"- **Salary:** {j['salary']}",
                f"- **Remote:** {remote_str}",
                f"- **Torre match:** {match_str}",
                f"- **Skills:** {skill_list}",
                f"- **My skill match score:** {score}/10 (matched: {', '.join(matched[:5]) or 'title hit'})",
                f"- **URL:** {j['url']}",
                "",
            ]
            if j["raw_id"] and j["raw_id"] not in seen_ids:
                seen_ids.add(j["raw_id"])
                all_jobs_flat.append((kw, j))

    # TOP PICKS section
    scored = [(skill_match_score(j)[0], kw, j) for kw, j in all_jobs_flat]
    scored.sort(key=lambda x: -x[0])

    lines += ["---", "", "## TOP PICKS (sorted by skill match)", ""]
    rank = 1
    shown = set()
    for score, kw, j in scored[:25]:
        if j["raw_id"] in shown:
            continue
        shown.add(j["raw_id"])
        _, matched = skill_match_score(j)
        match_str = f"{j['match_score']}" if j["match_score"] else "?"
        sal = j["salary"]
        lines += [
            f"### #{rank} [{score}pts] {j['title']} @ {j['company']}",
            f"- Salary: {sal} | Torre match: {match_str} | Found via: {kw}",
            f"- Matched my skills: {', '.join(matched) or '(title match)'}",
            f"- All skills: {', '.join(j['skills'][:8])}",
            f"- {j['url']}",
            "",
        ]
        rank += 1

    lines += [
        "---",
        "",
        "## How to read skill match score",
        "- 2 pts = keyword in job title",
        "- 1 pt  = each skill tag that matches my stack (Python/FastAPI/Playwright/pytest/asyncio/etc.)",
        "- Score >= 4 is worth reading carefully",
        "- Score >= 6 is a strong technical fit — apply",
    ]
    return "\n".join(lines)


async def main():
    if not COOKIES_FILE.exists():
        print(f"ERROR: {COOKIES_FILE} missing")
        return

    cookies = load_cookies(COOKIES_FILE)
    all_results: dict[str, list[dict]] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        print(f"Scraping {len(KEYWORDS)} keywords from Torre.ai...\n")
        for kw in KEYWORDS:
            jobs = await scrape_one_keyword(browser, cookies, kw)
            all_results[kw] = jobs
        await browser.close()

    # Save raw JSON
    OUT_JSON.write_text(json.dumps(all_results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nRaw JSON: {OUT_JSON}")

    # Save markdown report
    md = build_markdown(all_results)
    OUT_MD.write_text(md, encoding="utf-8")
    print(f"Markdown report: {OUT_MD}")

    print("\n=== SUMMARY ===")
    total = 0
    for kw, jobs in all_results.items():
        print(f"  {kw}: {len(jobs)}")
        total += len(jobs)
    print(f"  TOTAL: {total} job records")


if __name__ == "__main__":
    asyncio.run(main())
