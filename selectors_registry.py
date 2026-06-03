"""
selectors_registry.py — GEMA Domain Selector Registry (The Blitz Edition)

ARCHITECTURAL ROLE:
    The single source of truth for all DOM selectors and URL templates.
    When a job board changes its SRP layout (Selector Drift), update ONE
    entry here. scraper.py logic stays untouched.

SURFACE SNIPING DESIGN:
    Each domain now has a `search_url_template` — a Python format string
    that produces the direct SRP URL from a title query. The scraper jumps
    straight to the results page. No landing page interaction, no clicks.

MULTI-SELECTOR FALLBACK CHAINS:
    Every selector field that was a single string is now a list[str].
    The scraper tries each selector in order and returns the first non-null
    result. This provides automatic resilience to minor DOM changes without
    requiring God Mode intervention for every A/B test the job board runs.

    Fallback priority (enforced in list order):
        1. data-* attributes  — most stable, intentionally added for automation
        2. ARIA attributes     — semantic, unlikely to change
        3. Semantic HTML       — h2/h3 survive CSS refactors
        4. CSS classes         — last resort, changes with design systems

LAST VERIFIED:
    Each entry records the date its selectors were last confirmed against
    the live site. The Streamlit sidebar surfaces a warning when this date
    is > 30 days old, prompting the user to verify or use God Mode.

NULL RATE THRESHOLD:
    If more than `null_rate_alert_threshold` of SRP card extractions return
    null in a single run, the circuit breaker trips and emits a structured
    warning directing the user to God Mode.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class DomainSelectors:
    """
    Complete scraping contract for one job board domain.
    All list fields use fallback chain semantics: try index 0 first,
    fall through to subsequent entries on null/exception.
    """
    domain:                     str

    # URL template for direct SRP access — {title} is URL-encoded by scraper
    search_url_template:        str

    # The SRP element that must be present before extraction starts.
    # Also used as the circuit breaker "is the page alive?" probe.
    wait_for_selector:          str

    # ── Fallback chains (list[str] — first non-null wins) ────────────────────

    # Selector for the repeating job card container element on the SRP.
    # The scraper iterates over all matches.
    job_card:   list[str] = field(default_factory=list)

    # Link selector — evaluated WITHIN each job_card element.
    # Must resolve to an <a> whose href is the canonical job URL.
    link:       list[str] = field(default_factory=list)

    # Title selector — evaluated within each job_card element.
    title:      list[str] = field(default_factory=list)

    # Company selector — evaluated within each job_card element.
    company:    list[str] = field(default_factory=list)

    # Salary selector — optional. None values are acceptable and route to Tier 4.
    salary:     list[str] = field(default_factory=list)

    # Location selector — optional. Extracts raw location string (e.g. "USA", "Anywhere").
    # None → location_raw stays None → location scoring skipped (neutral 0 pts).
    location:   list[str] = field(default_factory=list)

    # ── Metadata ─────────────────────────────────────────────────────────────

    # ISO date the selectors were last verified against the live site.
    # Streamlit sidebar warns if this is > 30 days old.
    last_verified: str = "2026-05-07"

    # Circuit breaker — consecutive null cards before aborting domain.
    null_threshold: int = 5

    # If more than this fraction of cards return null, emit a drift alert.
    null_rate_alert_threshold: float = 0.40

    # Pagination support (for future multi-page extension)
    next_page_btn: Optional[str] = None


# =============================================================================
# DOMAIN REGISTRY
# =============================================================================

SELECTORS: dict[str, DomainSelectors] = {

    # ── Himalayas.app ─────────────────────────────────────────────────────────
    # Clean remote-first board. Stable data-testid attributes since 2023.
    # SRP loads React-rendered cards after DOMContentLoaded (~300ms).
    "himalayas.app": DomainSelectors(
        domain               = "himalayas.app",
        search_url_template  = "https://himalayas.app/jobs?q={title}&remote=true",
        # Verified 2026-05-25: Himalayas migrated from data-testid attrs to
        # Tailwind utility classes + Next.js RSC. Job cards are now <article>
        # elements; the canonical job link follows /companies/{co}/jobs/{slug}.
        # wait_for_selector uses the link pattern (stable URL structure, not CSS).
        wait_for_selector    = "a[href*='/companies/'][href*='/jobs/']",
        last_verified        = "2026-05-25",
        null_threshold       = 5,
        job_card = [
            "article[class*='cursor-pointer']",
            "article[class*='rounded-xl']",
            "article",
        ],
        link = [
            # The overlay link (class: absolute inset-0) covers the whole card
            # for click area — it has an sr-only child "View job" and no visible text.
            # The second link (no absolute class) is the visible title link.
            # Both share the same /companies/.../jobs/... href pattern.
            "a[href*='/companies/'][href*='/jobs/']:not([class*='absolute'])",
            "a[href*='/companies/'][href*='/jobs/']",
        ],
        title = [
            # Same visible link — its direct text content is the job title.
            "a[href*='/companies/'][href*='/jobs/']:not([class*='absolute'])",
            "h2",
            "h3",
        ],
        company = [
            # Company name is rendered as a logo image alt text and/or a monogram.
            # The company-page link (/companies/{slug}) without /jobs/ gives the slug.
            "img[alt*='logo']",
            "a[href*='/companies/']:not([href*='/jobs/'])",
            "span[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "span[class*='compensation']",
            "div[class*='salary']",
        ],
        next_page_btn = "a[aria-label='Next page']",
    ),

    # ── TrueUp.io ─────────────────────────────────────────────────────────────
    # Tech-focused board. React SPA with variable hydration timing.
    # Wait for job-item before extracting — salary often injected via XHR.
    "trueup.io": DomainSelectors(
        domain               = "trueup.io",
        search_url_template  = "https://trueup.io/jobs?q={title}&remote=true",
        wait_for_selector    = "div.job-item",
        last_verified        = "2026-05-07",
        null_threshold       = 5,
        job_card = [
            "div.job-item",
            "li.job-listing",
            "article[data-job]",
        ],
        link = [
            "a.job-link",
            "a[href*='/jobs/']",
            "h2 a",
        ],
        title = [
            "h2.job-title",
            ".position-title",
            "h2",
            "h3",
        ],
        company = [
            ".company-name",
            ".employer",
            "span[class*='company']",
        ],
        salary = [
            ".salary",
            ".compensation-range",
            "span[class*='pay']",
            "span[class*='salary']",
        ],
        next_page_btn = "button.pagination-next",
    ),

    # ── Remote.co ─────────────────────────────────────────────────────────────
    # WordPress-based board. Stable traditional HTML — minimal JS hydration.
    "remote.co": DomainSelectors(
        domain               = "remote.co",
        search_url_template  = "https://remote.co/remote-jobs/search/?search_keywords={title}",
        wait_for_selector    = "li.job_listing",
        last_verified        = "2026-05-07",
        null_threshold       = 5,
        job_card = [
            "li.job_listing",
            ".remote_job",
            "article.job_listing",
        ],
        link = [
            "a.job_listing_link",
            "h2 a",
            "a[href*='/remote-jobs/']",
        ],
        title = [
            ".position",
            "h2 a",
            "h3",
        ],
        company = [
            ".company",
            ".employer-name",
            "span[class*='company']",
        ],
        salary = [
            ".salary",
            "span[class*='salary']",
        ],
        next_page_btn = ".next.page-numbers",
    ),

    # ── WeWorkRemotely ────────────────────────────────────────────────────────
    # Minimal React. Salary rarely published in listings.
    # null_threshold=3: strict — WWR cards are very consistent; nulls = drift.
    "weworkremotely.com": DomainSelectors(
        domain               = "weworkremotely.com",
        search_url_template  = "https://weworkremotely.com/remote-jobs/search?term={title}",
        wait_for_selector    = "li.feature",
        last_verified        = "2026-05-07",
        null_threshold       = 3,
        job_card = [
            "li.feature",
            "li[class*='job']",
        ],
        link = [
            "a[href*='/remote-jobs/']",
            "li.feature > a",
        ],
        title = [
            "span.title",
            "h4.title",
            "h3",
        ],
        company = [
            "span.company",
            "span[class*='company']",
        ],
        salary = [
            "span.salary",
            ".compensation",
        ],
        next_page_btn = "a[rel='next']",
    ),

    # ── RemoteOK ──────────────────────────────────────────────────────────────
    # High anti-bot risk (Cloudflare). Will return 403/429 and be skipped
    # gracefully by the rate-limit handler. Selectors kept for when it works.
    "remoteok.com": DomainSelectors(
        domain               = "remoteok.com",
        search_url_template  = "https://remoteok.com/?q={title}",
        wait_for_selector    = "tr.job[data-id]",
        last_verified        = "2026-05-10",
        null_threshold       = 3,
        job_card = [
            "tr.job[data-id]",
            "tr[data-id]",
        ],
        link = [
            "a.preventLink",
            "a[href*='/remote-']",
            "td.company a",
        ],
        title = [
            "h2[itemprop='title']",
            "h2.title",
            "h2",
        ],
        company = [
            "h3[itemprop='name']",
            "h3.company",
            "span.company",
        ],
        salary = [
            "span.salary",
            "div.salary",
            "span[class*='salary']",
        ],
    ),

    # ── WorkingNomads ─────────────────────────────────────────────────────────
    # AngularJS SPA. Verified 2026-05-30: card IS the <a> element (a.job-desktop).
    # Scraper card-as-link fallback handles href extraction (line 832 scraper.py).
    # Relative URLs (/jobs/slug) resolved to https://workingnomads.com/jobs/slug.
    # search_url uses single-word tags — multi-word tags return 0 results.
    "workingnomads.com": DomainSelectors(
        domain               = "workingnomads.com",
        search_url_template  = "https://www.workingnomads.com/jobs?tag={title}",
        wait_for_selector    = "a.job-desktop",
        last_verified        = "2026-05-30",
        null_threshold       = 5,
        job_card = [
            "a.job-desktop",
        ],
        link = [
            "a.job-desktop",
        ],
        title = [
            "h4",
            "[class*='title']",
            "h3",
        ],
        company = [
            "[class*='company']",
            "span.company-name",
            "p.company",
        ],
        salary = [
            ".salary",
            "span[class*='salary']",
        ],
        location = [
            "div.box",
            "[ng-show*='locations']",
        ],
        next_page_btn = "a[rel='next']",
    ),

    # ── Hacker News Jobs ──────────────────────────────────────────────────────
    # Static HTML, no keyword search — {title} param is ignored by HN.
    # Returns all current job posts from the /jobs page.
    # No structured salary field. Company often embedded in title.
    # Verified 2026-05-30: wait_for_selector must be tr.athing (30 cards). .itemlist = 0.
    "news.ycombinator.com": DomainSelectors(
        domain               = "news.ycombinator.com",
        search_url_template  = "https://news.ycombinator.com/jobs?q={title}",
        wait_for_selector    = "tr.athing",
        last_verified        = "2026-05-30",
        null_threshold       = 10,
        job_card = [
            "tr.athing",
        ],
        link = [
            "span.titleline > a",
            "td.title a",
        ],
        title = [
            "span.titleline > a",
            "td.title a",
        ],
        company = [
            "span.sitestr",
            "span.sitebit a",
        ],
        salary = [],
    ),

    # ── Wellfound (AngelList) ─────────────────────────────────────────────────
    # Requires login to view full job listings. The scraper will timeout on
    # wait_for_selector (login wall renders instead of job cards) and return []
    # gracefully. No action needed — zero results is the expected outcome.
    "wellfound.com": DomainSelectors(
        domain               = "wellfound.com",
        search_url_template  = "https://wellfound.com/jobs?q={title}&remote=true",
        wait_for_selector    = "div[class*='JobListing']",
        last_verified        = "2026-05-10",
        null_threshold       = 2,
        job_card = [
            "div[class*='JobListing']",
            "div[class*='styles_container']",
            "div[data-test='job-row']",
        ],
        link = [
            "a[data-test='job-link']",
            "a[href*='/jobs/']",
            "h2 a",
        ],
        title = [
            "h2[class*='title']",
            "span[class*='title']",
            "h2",
        ],
        company = [
            "h3[class*='company']",
            "span[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "span[class*='compensation']",
        ],
    ),

    # ── Arc.dev ───────────────────────────────────────────────────────────────
    # May require login for full results. Graceful timeout → [] if blocked.
    # Verified 2026-05-30: div[data-testid='job-card'] works (10 cards).
    # Link is first <a> in card: a[href*='/remote-jobs/details/'] (6 of 10 cards).
    # Other links in card are skill tag links (/remote-jobs/postgresql etc) — exclude.
    "arc.dev": DomainSelectors(
        domain               = "arc.dev",
        search_url_template  = "https://arc.dev/remote-jobs?q={title}",
        wait_for_selector    = "div[data-testid='job-card']",
        last_verified        = "2026-05-30",
        null_threshold       = 3,
        job_card = [
            "div[data-testid='job-card']",
            "div[class*='job-card']",
        ],
        link = [
            "a[href*='/remote-jobs/details/']",
            "a[href*='/remote-jobs/']:not([href*='/remote-jobs/ai']):not([href*='/remote-jobs/data']):not([href*='/remote-jobs/python'])",
        ],
        title = [
            "a[href*='/remote-jobs/details/']",
            "h2",
            "h3",
        ],
        company = [
            "div.company-logo img",
            "span[class*='company']",
            "p[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "p[class*='compensation']",
        ],
    ),

    # ── Builtin ───────────────────────────────────────────────────────────────
    # React SPA, tech-focused US board. Filters to remote listings.
    # Verified 2026-05-30: div[data-id] works (25 cards), a[href*='/job/'] confirmed.
    # Has LATAM-specific remote listings — high signal for Diego.
    "builtin.com": DomainSelectors(
        domain               = "builtin.com",
        search_url_template  = "https://builtin.com/jobs/remote?search={title}",
        wait_for_selector    = "div[data-id]",
        last_verified        = "2026-05-30",
        null_threshold       = 5,
        job_card = [
            "div[data-id]",
            "div.job-card",
        ],
        link = [
            "a[href*='/job/']",
            "a[data-id]",
            "h2 a",
        ],
        title = [
            "a[href*='/job/']",
            "h2[class*='job-title']",
            "h2",
        ],
        company = [
            "div[class*='company-name']",
            "span[class*='company']",
            "a[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "div[class*='salary']",
        ],
        next_page_btn = "a[aria-label='Next page']",
    ),

    # ── Welcome to the Jungle ─────────────────────────────────────────────────
    # French-origin React SPA. May require account for full listings.
    # Graceful timeout → [] if blocked by auth wall.
    "welcometothejungle.com": DomainSelectors(
        domain               = "welcometothejungle.com",
        search_url_template  = "https://www.welcometothejungle.com/en/jobs?query={title}&remote=true",
        wait_for_selector    = "article[data-testid='job-card']",
        last_verified        = "2026-05-10",
        null_threshold       = 3,
        job_card = [
            "article[data-testid='job-card']",
            "li[data-testid='search-results-list-item-wrapper']",
            "li[class*='sc-']",
        ],
        link = [
            "a[data-testid='job-card-link']",
            "a[href*='/en/companies/']",
            "article a",
        ],
        title = [
            "h3[class*='title']",
            "h4[class*='title']",
            "h3",
        ],
        company = [
            "span[class*='company']",
            "p[class*='company']",
            "a[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "li[class*='salary']",
        ],
    ),

    # ── Remotivated ───────────────────────────────────────────────────────────
    # Emerging remote job board. Best-effort selectors — verify after first run.
    "remotivated.com": DomainSelectors(
        domain               = "remotivated.com",
        search_url_template  = "https://remotivated.com/jobs?q={title}",
        wait_for_selector    = ".job-card",
        last_verified        = "2026-05-10",
        null_threshold       = 5,
        job_card = [
            ".job-card",
            "div[class*='job']",
            "li[class*='job']",
            "article",
        ],
        link = [
            "a[href*='/jobs/']",
            "h2 a",
            "h3 a",
        ],
        title = [
            "h2",
            "h3",
            ".job-title",
        ],
        company = [
            ".company",
            "span[class*='company']",
        ],
        salary = [
            ".salary",
            "span[class*='salary']",
        ],
    ),

    # ── PostHog Cool Tech Jobs ────────────────────────────────────────────────
    # Curated list by PostHog — no keyword search, {title} param ignored.
    # Next.js static page. Selectors may need God Mode adjustment.
    "posthog.com": DomainSelectors(
        domain               = "posthog.com",
        search_url_template  = "https://posthog.com/cool-tech-jobs?q={title}",
        wait_for_selector    = "main",
        last_verified        = "2026-05-10",
        null_threshold       = 5,
        job_card = [
            "li[class*='job']",
            "div[class*='job']",
            "article",
            "li",
        ],
        link = [
            "a[href*='jobs']",
            "a[href*='careers']",
            "li a",
        ],
        title = [
            "h2",
            "h3",
            "span[class*='title']",
        ],
        company = [
            "span[class*='company']",
            "p[class*='company']",
            "strong",
        ],
        salary = [
            "span[class*='salary']",
            "span[class*='compensation']",
        ],
    ),

    # ── Jobspresso ────────────────────────────────────────────────────────────
    # WordPress + WP Job Manager. Clean HTML, minimal JS. Stable selectors.
    # Verified 2026-05-25: li.job_listing (30 cards), link is a.job_listing-clickbox
    # with absolute URL pattern /job/ (NOT /remote-work/). Title is h3.job_listing-title.
    "jobspresso.co": DomainSelectors(
        domain               = "jobspresso.co",
        search_url_template  = "https://jobspresso.co/remote-work/?search_keywords={title}",
        wait_for_selector    = "li.job_listing",
        last_verified        = "2026-05-25",
        null_threshold       = 5,
        job_card = [
            "li.job_listing",
            "article.job_listing",
        ],
        link = [
            "a.job_listing-clickbox",
            "a[href*='/job/']",
            "h3 a",
        ],
        title = [
            "h3.job_listing-title",
            ".position",
            "h3",
            "h2",
        ],
        company = [
            ".company",
            "span[class*='company']",
        ],
        salary = [
            ".salary",
            "span[class*='salary']",
        ],
        next_page_btn = "a.next",
    ),

    # ── Greenhouse Careers ────────────────────────────────────────────────────
    # Greenhouse's own company career page (not the ATS platform).
    # Static/Next.js. Limited listings but high-signal tech roles.
    "greenhouse.com": DomainSelectors(
        domain               = "greenhouse.com",
        search_url_template  = "https://www.greenhouse.com/careers/opportunities?q={title}",
        wait_for_selector    = "main",
        last_verified        = "2026-05-10",
        null_threshold       = 5,
        job_card = [
            "div[class*='opening']",
            "li[class*='opening']",
            "div.job",
            "li.job",
        ],
        link = [
            "a[href*='/careers/']",
            "a[href*='boards.greenhouse.io']",
            "h2 a",
        ],
        title = [
            "h2",
            "h3",
            "p[class*='title']",
        ],
        company = [
            "span[class*='company']",
            "p[class*='department']",
        ],
        salary = [],
    ),

    # ── Python.org Jobs ───────────────────────────────────────────────────────
    # Official Python job board. Static HTML, highly stable selectors.
    # Keyword search supported via ?q= param. No salary field published.
    "python.org": DomainSelectors(
        domain               = "python.org",
        search_url_template  = "https://www.python.org/jobs/?q={title}",
        wait_for_selector    = "ol.list-recent-jobs",
        last_verified        = "2026-05-10",
        null_threshold       = 5,
        job_card = [
            "ol.list-recent-jobs li",
            "ol.list-recent-jobs article",
        ],
        link = [
            "h2 a",
            "a[href*='/jobs/']",
        ],
        title = [
            "h2 a",
            "h2",
        ],
        company = [
            "span.listing-company-name",
            ".company",
            "span[class*='company']",
        ],
        salary = [],
        next_page_btn = "a[rel='next']",
    ),

    # ── Startup.jobs ──────────────────────────────────────────────────────────
    # Startup-focused board. React SPA with server-side rendered job cards.
    # Remote filter appended via &remote=true. Salary rarely published.
    # Selectors verified 2026-05-25 — run God Mode if null_rate trips.
    "startup.jobs": DomainSelectors(
        domain               = "startup.jobs",
        search_url_template  = "https://startup.jobs/?q={title}&remote=true",
        wait_for_selector    = "div.css-1wts5rl",
        last_verified        = "2026-05-25",
        null_threshold       = 5,
        job_card = [
            "div.css-1wts5rl",
            "div[data-cy='job-card']",
            "li[class*='job']",
            "article[class*='job']",
        ],
        link = [
            "a[data-cy='job-link']",
            "a[href*='/jobs/']",
            "h2 a",
            "h3 a",
        ],
        title = [
            "h2[class*='title']",
            "h3[class*='title']",
            "span[class*='title']",
            "h2",
            "h3",
        ],
        company = [
            "span[class*='company']",
            "p[class*='company']",
            "div[class*='company']",
            "a[class*='company']",
        ],
        salary = [
            "span[class*='salary']",
            "span[class*='compensation']",
            "div[class*='salary']",
        ],
        next_page_btn = "a[aria-label='Next page']",
    ),
}


# =============================================================================
# Lookup API
# =============================================================================

def get_selectors(domain: str) -> Optional[DomainSelectors]:
    """
    Retrieves the DomainSelectors for a domain string.
    Partial-match aware: 'himalayas.app/jobs?q=sdet' resolves correctly.
    Returns None if unknown → caller should prompt God Mode.
    """
    clean = domain.lower().strip().replace("www.", "").rstrip("/").split("/")[0]
    for key, sel in SELECTORS.items():
        if key in clean:
            return sel
    return None


def list_supported_domains() -> list[str]:
    return list(SELECTORS.keys())


def is_selector_stale(sel: DomainSelectors, warn_after_days: int = 30) -> bool:
    """Returns True if selectors are older than warn_after_days."""
    from datetime import date
    try:
        last = date.fromisoformat(sel.last_verified)
        return (date.today() - last).days > warn_after_days
    except ValueError:
        return True
