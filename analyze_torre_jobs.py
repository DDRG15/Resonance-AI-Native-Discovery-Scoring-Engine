"""
Analyzes 152 Torre.ai jobs and writes YES / NO / MAYBE folders.
Run: python analyze_torre_jobs.py
Output: torre_job_analysis/  (folders + index)
"""
import json
import re
from pathlib import Path

RAW_JSON = Path(__file__).parent / "torre_full_results.json"
OUT_DIR  = Path(__file__).parent / "torre_job_analysis"

# ── Diego's actual skills ──────────────────────────────────────────────────
MY_SKILLS = {
    "python", "fastapi", "playwright", "pytest", "asyncio", "pydantic",
    "docker", "docker compose", "github actions", "ci/cd", "sql", "sqlite",
    "rest api", "selenium", "aiohttp", "pandas", "streamlit", "git", "linux",
    "data integrity", "sre", "reliability", "backend", "api testing",
    "unit testing", "automation", "scraping", "llm", "groq", "gemini",
    "openapi", "jinja2", "structured logging", "observability", "testing",
    "bash", "json", "http", "webhook", "fastapi", "pydantic", "devops",
    "postgresql", "debugging", "version control", "circuit breaker",
    "exponential backoff", "asyncio", "aiohttp", "playwright", "pytest",
}

# ── Hard blockers: if required as proficient → NO ──────────────────────────
HARD_BLOCK_SKILLS = {
    # Languages user doesn't know
    "java", "typescript", "angular", "vue.js", "react.js", "react native",
    "next.js", "node.js", "nestjs", "spring boot", "kotlin", "swift",
    "go", "golang", "rust", "c++", "c#", ".net", "perl", "ruby", "php",
    "scala", "r programming",
    # Cloud platforms requiring certification-level knowledge
    "kubernetes", "terraform", "ansible", "cloudformation", "helm",
    "aws lambda", "aws", "amazon web services", "azure", "microsoft azure",
    "google cloud platform", "gcp",
    # ML stack
    "pytorch", "tensorflow", "keras", "scikit-learn", "mlops",
    "amazon sagemaker", "azure machine learning", "vertex ai",
    "machine learning", "deep learning", "neural networks",
}

# ── Soft blockers: if in title AND required → push to MAYBE ───────────────
SOFT_BLOCK_TITLE = {
    "senior", "staff", "principal", "lead", "manager", "architect",
    "fullstack", "full-stack", "full stack",
}

# ── Bonus: strong YES signals ──────────────────────────────────────────────
STRONG_YES_SKILLS = {
    "python", "fastapi", "playwright", "pytest", "asyncio", "pydantic",
    "data integrity", "api testing", "automation", "sre", "reliability",
}


def get_salary_monthly_usd(comp_data: dict | None) -> float | None:
    """Returns monthly USD equivalent or None if hidden/not monthly."""
    if not comp_data:
        return None
    d = comp_data.get("data") or comp_data
    if not d:
        return None
    min_amt = d.get("minAmount")
    if not min_amt:
        return None
    period = d.get("periodicity", "")
    currency = d.get("currency", "USD")
    rate = d.get("conversionRateUSD", 1.0) or 1.0

    usd_min = min_amt * rate
    if period == "monthly":
        return usd_min
    if period == "hourly":
        return usd_min * 160
    if period == "annually":
        return usd_min / 12
    if period in ("project", ""):
        return None  # project pay — can't evaluate as monthly
    return None


def salary_str(comp_data: dict | None) -> str:
    if not comp_data:
        return "Hidden"
    d = comp_data.get("data") or comp_data
    if not d:
        return "Hidden"
    vis = comp_data.get("visible", True) if isinstance(comp_data, dict) else True
    if not vis:
        return "Hidden"
    min_a = d.get("minAmount")
    max_a = d.get("maxAmount")
    if not min_a:
        return "Hidden"
    curr    = d.get("currency", "USD")
    period  = d.get("periodicity", "?")
    neg     = " (negotiable)" if d.get("negotiable") else ""
    max_str = f"{max_a:,.0f}" if max_a else "?"
    return f"{curr} {min_a:,.0f}-{max_str}/{period}{neg}"


def classify_job(job_raw: dict) -> tuple[str, list[str], list[str]]:
    """
    Returns (verdict, reasons_for, reasons_against)
    verdict: "YES" | "NO" | "MAYBE"
    """
    title    = (job_raw.get("objective") or "").lower()
    skills   = job_raw.get("skills") or []
    comp     = job_raw.get("compensation") or {}
    comm     = job_raw.get("commitment", "")
    opp_type = job_raw.get("opportunity", "")

    reasons_for: list[str]     = []
    reasons_against: list[str] = []

    # Build skill dicts for analysis
    skill_names_lower = {s["name"].lower() for s in skills}
    required_skills = {
        s["name"].lower() for s in skills
        if s.get("proficiency") in ("proficient", "expert")
        and s.get("experience") in ("3-plus-years", "1-3-years", "potential-to-develop")
    }
    hard_required = {
        s["name"].lower() for s in skills
        if s.get("proficiency") in ("proficient", "expert")
        and s.get("experience") in ("3-plus-years", "1-3-years")
    }

    # 1 — Hard blockers (skills required as proficient that Diego doesn't have)
    blockers_hit = []
    for skill in hard_required:
        for blocker in HARD_BLOCK_SKILLS:
            if blocker in skill:
                blockers_hit.append(skill)
                break
    # Also check all proficient skills for critical blockers
    for skill in required_skills:
        for blocker in ["kubernetes", "terraform", "aws", "java", "angular", "typescript"]:
            if blocker in skill and skill not in blockers_hit:
                blockers_hit.append(skill)
                break

    if blockers_hit:
        reasons_against.append(f"Hard skill gap: {', '.join(blockers_hit[:4])}")

    # 2 — Check for Python/FastAPI/pytest presence (minimum signal)
    python_present = any(
        s in skill_names_lower for s in ["python", "fastapi", "pytest", "playwright", "asyncio", "pydantic"]
    )
    if python_present:
        matched = [s for s in STRONG_YES_SKILLS if s in skill_names_lower]
        reasons_for.append(f"Core skills present: {', '.join(matched)}")

    # 3 — Salary check
    monthly_usd = get_salary_monthly_usd(comp)
    sal_str_val = salary_str(comp)
    if monthly_usd is not None:
        if monthly_usd < 1200:
            reasons_against.append(f"Salary too low: {sal_str_val} (< $1,200/mo)")
        elif monthly_usd < 1500:
            reasons_against.append(f"Salary borderline: {sal_str_val} ($1,200–$1,500/mo — need extras)")
        else:
            reasons_for.append(f"Salary OK: {sal_str_val}")

    # 4 — Commitment / type
    if opp_type in ("flexible-job",) and "project" in (comp.get("data") or {}).get("periodicity", ""):
        reasons_against.append("Project-based pay (not stable monthly income)")
    if comm == "part-time":
        reasons_against.append("Part-time position")

    # 5 — Seniority signals in title
    senior_hit = [w for w in SOFT_BLOCK_TITLE if w in title]
    if senior_hit:
        reasons_against.append(f"Seniority mismatch in title: {', '.join(senior_hit)}")

    # 6 — Strong positive signals
    strong_matches = [s for s in STRONG_YES_SKILLS if s in skill_names_lower]
    my_matches     = [s for s in MY_SKILLS if s in skill_names_lower]

    # ── VERDICT ──────────────────────────────────────────────────────────
    has_blocker  = bool(blockers_hit)
    has_python   = python_present
    sal_ok       = monthly_usd is None or monthly_usd >= 1200
    sal_good     = monthly_usd is None or monthly_usd >= 1500
    sal_borderline = monthly_usd is not None and 1200 <= monthly_usd < 1500

    if has_blocker and not has_python:
        verdict = "NO"
    elif has_blocker and has_python and len(blockers_hit) <= 1:
        # Python is there but one hard blocker — maybe they can work around it
        verdict = "MAYBE"
    elif has_blocker and has_python and len(blockers_hit) >= 2:
        verdict = "NO"
    elif not has_python:
        verdict = "NO"
    elif not sal_ok:
        verdict = "NO"
    elif len(strong_matches) >= 2 and sal_good and not has_blocker:
        verdict = "YES"
    elif len(strong_matches) >= 1 and not has_blocker:
        verdict = "MAYBE" if sal_borderline else "YES"
    elif has_python and not has_blocker:
        verdict = "MAYBE"
    else:
        verdict = "NO"

    # Soft title mismatch → downgrade YES to MAYBE
    if verdict == "YES" and senior_hit:
        verdict = "MAYBE"
        reasons_against.append("Downgraded YES→MAYBE: seniority flag in title")

    return verdict, reasons_for, reasons_against


def build_job_md(job_raw: dict, verdict: str, reasons_for: list, reasons_against: list,
                 found_via: list[str], priority: bool = False) -> str:
    title   = job_raw.get("objective", "?")
    orgs    = job_raw.get("organizations") or []
    company = orgs[0]["name"] if orgs else "?"
    url     = f"https://torre.ai/jobs/{job_raw.get('id', '')}"
    remote  = job_raw.get("remote", False)
    comm    = job_raw.get("commitment", "?")
    created = (job_raw.get("created") or "")[:10]
    deadline = (job_raw.get("deadline") or "")[:10] or "no deadline"
    tagline = job_raw.get("tagline", "")
    apps    = job_raw.get("finishedApplications")
    comp    = job_raw.get("compensation") or {}
    sal     = salary_str(comp)
    monthly = get_salary_monthly_usd(comp)
    monthly_str = f"~${monthly:,.0f}/mo" if monthly else ""

    skills = job_raw.get("skills") or []
    skills_table = []
    for s in skills:
        name  = s.get("name", "")
        prof  = s.get("proficiency", "")
        exp   = s.get("experience", "")
        prof_map = {
            "proficient": "Required",
            "expert": "Expert",
            "no-experience-interested": "Nice to have",
            "": "?"
        }
        skills_table.append(f"| {name} | {prof_map.get(prof, prof)} | {exp} |")

    priority_banner = "\n> ** PRIORITY APPLICATION — Apply first **\n" if priority else ""

    lines = [
        f"# {verdict} — {title}",
        f"## {company}",
        priority_banner,
        f"- **Verdict:** {verdict}",
        f"- **URL:** {url}",
        f"- **Salary:** {sal} {monthly_str}",
        f"- **Remote:** {'Yes' if remote else 'No'}",
        f"- **Commitment:** {comm}",
        f"- **Posted:** {created} | **Deadline:** {deadline}",
        f"- **Applications so far:** {apps or '?'}",
        f"- **Found via keyword:** {', '.join(found_via)}",
        "",
        f"> {tagline}" if tagline else "",
        "",
        "## Why this verdict",
        "",
        "**For:**" if reasons_for else "",
    ]
    for r in reasons_for:
        lines.append(f"- {r}")
    lines += ["", "**Against:**" if reasons_against else ""]
    for r in reasons_against:
        lines.append(f"- {r}")

    lines += [
        "",
        "## Skills breakdown",
        "",
        "| Skill | Level | Experience |",
        "|-------|-------|------------|",
    ]
    lines += skills_table
    lines += [""]
    return "\n".join(lines)


def safe_filename(title: str, company: str, job_id: str) -> str:
    combined = f"{company}_{title}"
    clean = re.sub(r"[^\w\s\-]", "", combined).strip()
    clean = re.sub(r"\s+", "_", clean)[:60]
    return f"{clean}_{job_id}"


def main():
    data = json.loads(RAW_JSON.read_text(encoding="utf-8"))

    # Deduplicate by raw_id across all keyword searches
    seen: dict[str, tuple[dict, list[str]]] = {}
    for keyword, jobs in data.items():
        for j in jobs:
            raw = j.get("raw") or {}
            jid = raw.get("id") or j.get("raw_id", "")
            if not jid:
                continue
            if jid not in seen:
                seen[jid] = (raw, [keyword])
            else:
                seen[jid][1].append(keyword)

    print(f"Total unique jobs: {len(seen)}")

    # Hardcoded White Hat Gaming priority (captured in scrape)
    # Job id from earlier analysis of the 36 results — search for it
    white_hat_ids = set()
    for jid, (raw, _) in seen.items():
        orgs = raw.get("organizations") or []
        name = orgs[0].get("name", "").lower() if orgs else ""
        if "white hat" in name or "whg" in name:
            white_hat_ids.add(jid)

    # Create output directories
    for folder in ("YES", "NO", "MAYBE"):
        (OUT_DIR / folder).mkdir(parents=True, exist_ok=True)

    counts = {"YES": 0, "NO": 0, "MAYBE": 0}
    index_yes, index_maybe, index_no = [], [], []

    for jid, (raw, keywords) in seen.items():
        is_priority = jid in white_hat_ids
        verdict, reasons_for, reasons_against = classify_job(raw)

        title   = raw.get("objective", "?")
        orgs    = raw.get("organizations") or []
        company = orgs[0]["name"] if orgs else "?"
        comp    = raw.get("compensation") or {}
        sal     = salary_str(comp)
        monthly = get_salary_monthly_usd(comp)
        monthly_str = f"~${monthly:,.0f}/mo" if monthly else ""
        url     = f"https://torre.ai/jobs/{jid}"
        skills  = [s["name"] for s in (raw.get("skills") or [])][:6]

        fname = safe_filename(title, company, jid) + ".md"
        md    = build_job_md(raw, verdict, reasons_for, reasons_against, keywords, is_priority)
        (OUT_DIR / verdict / fname).write_text(md, encoding="utf-8")
        counts[verdict] += 1

        row = f"| {'** PRIORITY**' if is_priority else ''} [{title}]({verdict}/{fname}) | {company} | {sal} {monthly_str} | {', '.join(skills[:4])} | {', '.join(keywords)} |"
        if verdict == "YES":
            index_yes.append(row)
        elif verdict == "MAYBE":
            index_maybe.append(row)
        else:
            index_no.append(row)

    # Write index
    index_lines = [
        "# Torre.ai Job Analysis Index",
        "Date: 2026-05-20 | 152 jobs scraped, deduplicated, analyzed",
        "",
        f"## Summary: {counts['YES']} YES | {counts['MAYBE']} MAYBE | {counts['NO']} NO",
        "",
        "> **White Hat Gaming QA Engineer = PRIORITY** — Apply first.",
        "> Best fit in the entire pool: Playwright direct match, code-based API testing escape hatch,",
        "> data integrity work maps to 'validating databases and microservices' in the JD.",
        "",
        "---",
        "",
        "## YES — Apply these",
        "| Priority | Title | Company | Salary | Key skills | Found via |",
        "|----------|-------|---------|--------|------------|-----------|",
    ]
    index_lines += sorted(index_yes)
    index_lines += [
        "",
        "---",
        "",
        "## MAYBE — Read the full JD first",
        "| Priority | Title | Company | Salary | Key skills | Found via |",
        "|----------|-------|---------|--------|------------|-----------|",
    ]
    index_lines += sorted(index_maybe)
    index_lines += [
        "",
        "---",
        "",
        "## NO — Skip",
        "| Priority | Title | Company | Salary | Key skills | Found via |",
        "|----------|-------|---------|--------|------------|-----------|",
    ]
    index_lines += sorted(index_no)

    (OUT_DIR / "INDEX.md").write_text("\n".join(index_lines), encoding="utf-8")

    print(f"\nResults:")
    print(f"  YES:   {counts['YES']}")
    print(f"  MAYBE: {counts['MAYBE']}")
    print(f"  NO:    {counts['NO']}")
    print(f"\nOutput: {OUT_DIR}")
    print(f"Index:  {OUT_DIR / 'INDEX.md'}")


if __name__ == "__main__":
    main()
