"""
Two tasks:
1. Parse FastAPI results already captured in torre_jobs_raw.json
2. Call search.torre.co directly for remaining keywords (now that we know the real API)
"""
import json
import requests
from pathlib import Path
from urllib.parse import urlencode

COOKIES_FILE = Path(__file__).parent / "cookies" / "torre.ai.json"
RAW_FILE     = Path(__file__).parent / "torre_jobs_raw.json"
OUT_MD       = Path(__file__).parent / "torre_jobs_analysis.md"

SEARCH_API = "https://search.torre.co/opportunities/_search"

KEYWORDS = [
    "FastAPI",
    "pytest",
    "Playwright",
    "QA automation",
    "Python backend",
    "Python",
    "asyncio",
    "Pydantic",
]

def load_cookies(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {c["name"]: c["value"] for c in data}


def search_keyword(session: requests.Session, keyword: str, size: int = 50) -> list[dict]:
    """Call Torre's real search API discovered via Playwright interception."""
    params = {
        "currency": "USD",
        "periodicity": "hourly",
        "lang": "es",
        "size": size,
        "aggregate": "false",
        "contextFeature": "job_feed",
    }
    # Try POST with q param
    body = {"q": f"(remote:yes and keywords:{keyword})"}
    try:
        r = session.post(
            SEARCH_API,
            params=params,
            json=body,
            timeout=15
        )
        print(f"  [{keyword}] POST -> {r.status_code} | {r.headers.get('content-type','?')[:40]}")
        if r.status_code == 200 and "json" in r.headers.get("content-type", ""):
            data = r.json()
            return extract_jobs(data)
    except Exception as e:
        print(f"  [{keyword}] POST ERROR: {e}")

    # Try GET with q param
    try:
        r2 = session.get(
            SEARCH_API,
            params={**params, "q": f"(remote:yes and keywords:{keyword})"},
            timeout=15
        )
        print(f"  [{keyword}] GET  -> {r2.status_code} | {r2.headers.get('content-type','?')[:40]}")
        if r2.status_code == 200 and "json" in r2.headers.get("content-type", ""):
            data = r2.json()
            return extract_jobs(data)
    except Exception as e:
        print(f"  [{keyword}] GET  ERROR: {e}")

    return []


def extract_jobs(data: dict | list) -> list[dict]:
    if isinstance(data, list):
        return data
    for key in ("results", "opportunities", "hits", "items", "data"):
        val = data.get(key)
        if isinstance(val, list):
            return val
        if isinstance(val, dict):
            for nk in ("hits", "results"):
                if nk in val and isinstance(val[nk], list):
                    return val[nk]
    return []


def job_to_dict(job: dict) -> dict:
    title   = job.get("objective") or job.get("title") or "?"
    orgs    = job.get("organizations") or []
    company = orgs[0].get("name", "?") if orgs else "?"
    comp    = job.get("compensation") or {}
    min_s   = comp.get("minAmount") or comp.get("min")
    max_s   = comp.get("maxAmount") or comp.get("max")
    curr    = comp.get("currency", "USD")
    period  = comp.get("periodicity", "")
    salary  = f"{curr} {min_s}-{max_s}/{period}" if min_s else "Hidden"
    skills  = [s.get("name", "") for s in (job.get("skills") or [])[:8]]
    jid     = job.get("id") or job.get("publicId") or ""
    url     = f"https://torre.ai/jobs/{jid}" if jid else ""
    remote  = job.get("remote", False)
    return dict(title=title, company=company, salary=salary, skills=skills, url=url, remote=remote, raw_id=jid)


MY_SKILLS = {
    "python", "fastapi", "playwright", "pytest", "asyncio", "pydantic",
    "docker", "github actions", "sql", "sqlite", "rest api", "api", "ci/cd",
    "selenium", "aiohttp", "pandas", "streamlit", "git", "linux",
    "data integrity", "sre", "reliability", "backend"
}

def score_job(job: dict) -> tuple[int, list[str]]:
    skills_lower = [s.lower() for s in job["skills"]]
    title_lower  = job["title"].lower()
    matched = [s for s in skills_lower if any(ms in s for ms in MY_SKILLS)]
    title_hit = any(ms in title_lower for ms in MY_SKILLS)
    score = len(matched) + (2 if title_hit else 0)
    return score, matched


def main():
    cookies = load_cookies(COOKIES_FILE)
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, */*",
        "Referer": "https://torre.ai/",
        "Origin":  "https://torre.ai",
    })

    # Load already-captured FastAPI results
    raw = json.loads(RAW_FILE.read_text(encoding="utf-8"))
    all_jobs: dict[str, list[dict]] = {}

    print("Parsing existing FastAPI results...")
    fastapi_raw = raw.get("FastAPI", [])
    all_jobs["FastAPI"] = [job_to_dict(j) for j in fastapi_raw if isinstance(j, dict) and j.get("objective")]

    # Call API for remaining keywords
    print("\nCalling search.torre.co for remaining keywords...")
    for kw in KEYWORDS:
        if kw == "FastAPI" and all_jobs.get("FastAPI"):
            print(f"  [FastAPI] Using {len(all_jobs['FastAPI'])} cached results")
            continue
        jobs_raw = search_keyword(session, kw)
        all_jobs[kw] = [job_to_dict(j) for j in jobs_raw if isinstance(j, dict) and j.get("objective")]

    # Print summary + build markdown report
    lines = ["# Torre.ai Job Search Results — 2026-05-20", ""]
    lines += ["Results from search.torre.co API with remote:yes filter.", ""]

    all_scored = []
    for kw, jobs in all_jobs.items():
        lines += [f"## {kw} ({len(jobs)} results)", ""]
        if not jobs:
            lines += ["*No results — API may require Playwright for this keyword.*", ""]
            continue
        for j in jobs:
            score, matched = score_job(j)
            all_scored.append((score, kw, j))
            sal = j["salary"]
            skills_str = ", ".join(j["skills"][:5]) if j["skills"] else "none listed"
            lines += [
                f"- **{j['title']}** @ {j['company']}",
                f"  Salary: {sal} | Skills: {skills_str}",
                f"  URL: {j['url']}",
                "",
            ]

    # Top picks sorted by skill match score
    lines += ["---", "# TOP PICKS (by skill match)", ""]
    top = sorted(all_scored, key=lambda x: -x[0])[:20]
    seen_ids = set()
    for score, kw, j in top:
        if j["raw_id"] in seen_ids:
            continue
        seen_ids.add(j["raw_id"])
        _, matched = score_job(j)
        lines += [
            f"- **[{score}pts] {j['title']}** @ {j['company']}",
            f"  Matched skills: {', '.join(matched) or 'title match'}",
            f"  Salary: {j['salary']} | Found via: {kw}",
            f"  {j['url']}",
            "",
        ]

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nReport saved to: {OUT_MD}")
    print("\nSummary:")
    for kw, jobs in all_jobs.items():
        print(f"  {kw}: {len(jobs)} jobs")


if __name__ == "__main__":
    main()
