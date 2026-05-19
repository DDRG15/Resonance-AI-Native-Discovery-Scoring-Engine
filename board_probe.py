"""
board_probe.py — Anti-bot Reconnaissance Module for GEMA.

Classifies each job board's protection type BEFORE the main scrape begins.
Runs a stealth Playwright request per domain, analyzes HTTP response signals,
and returns a ProbeResult with protection type + recommended strategy.

Standalone usage:
    python board_probe.py                          # probe all registered boards
    python board_probe.py remoteok.com wellfound.com   # probe specific boards

Output:
    - Formatted table in stdout
    - probe_results.json saved locally (gitignored)

Protection type signatures:
    cloudflare_js_challenge  — cf-ray header + 403 + "Checking your browser"
    cloudflare_turnstile     — 403/503 + "Just a moment" (harder challenge)
    login_wall               — 200 OK but login page content, no job data
    rate_limited             — HTTP 429 Too Many Requests
    open                     — 200 OK with actual content, no blocking detected
    unknown                  — Unrecognized pattern, manual investigation needed
"""

import asyncio
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import async_playwright

import config

logger = logging.getLogger(__name__)


# =============================================================================
# Protection type constants
# =============================================================================

OPEN                 = "open"
CLOUDFLARE_JS        = "cloudflare_js_challenge"
CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
LOGIN_WALL           = "login_wall"
RATE_LIMITED         = "rate_limited"
UNKNOWN              = "unknown"

_STRATEGY_MAP = {
    OPEN:                 "playwright_stealth_current",
    CLOUDFLARE_JS:        "camoufox_or_proxy_residential",
    CLOUDFLARE_TURNSTILE: "proxy_residential_only",
    LOGIN_WALL:           "session_cookie_injection",
    RATE_LIMITED:         "increase_jitter_exponential",
    UNKNOWN:              "manual_investigation_required",
}

_PROTECTION_LABEL = {
    OPEN:                 "Open — no blocking",
    CLOUDFLARE_JS:        "Cloudflare JS Challenge",
    CLOUDFLARE_TURNSTILE: "Cloudflare Turnstile (hard)",
    LOGIN_WALL:           "Login wall (auth required)",
    RATE_LIMITED:         "HTTP 429 rate limiting",
    UNKNOWN:              "Unknown — needs manual check",
}

# Login-related page keywords (any present → likely a login wall)
_LOGIN_SIGNALS = [
    "sign in", "log in", "log into", "please log in", "create account",
    "join now", "register", "to view jobs", "to see", "your email",
    "password", "forgot your password",
]

# Minimum job-related word density to confirm a real SRP (not a login page)
_JOB_SIGNALS = [
    "engineer", "developer", "remote", "salary", "apply", "hiring",
    "full-time", "part-time", "contract", "job", "role", "position",
]


# =============================================================================
# Data contract
# =============================================================================

@dataclass
class ProbeResult:
    domain: str
    protection_type: str
    recommended_strategy: str
    can_scrape_now: bool
    http_status: int
    notes: str
    cf_headers: list[str] = field(default_factory=list)
    security_cookies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "domain":               self.domain,
            "protection_type":      self.protection_type,
            "recommended_strategy": self.recommended_strategy,
            "can_scrape_now":       self.can_scrape_now,
            "http_status":          self.http_status,
            "notes":                self.notes,
            "cf_headers":           self.cf_headers,
            "security_cookies":     self.security_cookies,
        }


# =============================================================================
# Classification logic
# =============================================================================

def _classify(
    status: int,
    headers: dict,
    body_sample: str,
    cookie_names: list[str],
) -> tuple[str, list[str]]:
    """
    Classify protection type from HTTP response signals.
    Returns (protection_type, list_of_detected_signals).
    body_sample — first 4KB of response body, lowercased.
    """
    signals: list[str] = []
    body = body_sample[:4096]

    # ── Cloudflare header presence ──────────────────────────────────────────
    has_cf_ray      = "cf-ray" in headers
    has_cf_mitigated = "cf-mitigated" in headers
    has_cf_bm       = "__cf_bm" in cookie_names
    has_clearance   = "cf_clearance" in cookie_names

    if has_cf_ray:
        signals.append("cf-ray header present")
    if has_cf_mitigated:
        signals.append("cf-mitigated (Bot Fight Mode active)")
    if has_cf_bm:
        signals.append("__cf_bm cookie (CF Bot Management)")
    if has_clearance:
        signals.append("cf_clearance cookie required")

    # ── Cloudflare challenge body signatures ────────────────────────────────
    if status in (403, 503):
        if "just a moment" in body:
            signals.append("'Just a moment' Turnstile page")
            return CLOUDFLARE_TURNSTILE, signals
        if "checking your browser" in body or "enable javascript" in body:
            signals.append("'Checking your browser' JS challenge page")
            return CLOUDFLARE_JS, signals
        if has_cf_ray:
            signals.append(f"CF 403/503 with cf-ray — Bot Fight Mode blocking")
            return CLOUDFLARE_JS, signals

    # ── DataDome / PerimeterX ───────────────────────────────────────────────
    if "x-datadome-cid" in headers or "datadome" in body:
        signals.append("DataDome protection detected")
        return UNKNOWN, signals
    if any(c.startswith("_px") for c in cookie_names):
        signals.append("PerimeterX (_px*) cookies detected")
        return UNKNOWN, signals

    # ── Rate limiting ────────────────────────────────────────────────────────
    if status == 429:
        retry_after = headers.get("retry-after", "unspecified")
        signals.append(f"HTTP 429 Too Many Requests (Retry-After: {retry_after})")
        return RATE_LIMITED, signals

    # ── Login wall ───────────────────────────────────────────────────────────
    if status == 200:
        login_hits = sum(1 for s in _LOGIN_SIGNALS if s in body)
        job_hits   = sum(1 for s in _JOB_SIGNALS   if s in body)
        if login_hits >= 2 and job_hits < 3:
            signals.append(
                f"Login page detected (login_signals={login_hits}, job_signals={job_hits})"
            )
            return LOGIN_WALL, signals

    # ── Open: 200 with content ───────────────────────────────────────────────
    if status == 200:
        job_hits = sum(1 for s in _JOB_SIGNALS if s in body)
        if has_cf_ray:
            signals.append(f"CF passthrough mode (200 OK, job_signals={job_hits})")
        else:
            signals.append(f"200 OK, no blocking (job_signals={job_hits})")
        return OPEN, signals

    # ── Fallback ─────────────────────────────────────────────────────────────
    signals.append(f"HTTP {status} — pattern not recognized")
    return UNKNOWN, signals


# =============================================================================
# Core probe function
# =============================================================================

async def probe_board(domain: str, timeout_ms: int = 20_000) -> ProbeResult:
    """
    Launch a stealth Playwright tab, navigate to domain root, classify protection.
    Never raises — returns ProbeResult(protection_type=UNKNOWN) on any error.
    """
    url = f"https://{domain}"
    http_status = -1
    response_headers: dict = {}

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=config.USER_AGENT_POOL[0],
                locale="en-US",
                timezone_id="America/New_York",
                ignore_https_errors=config.IGNORE_HTTPS_ERRORS,
                extra_http_headers={
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer":         "https://www.google.com/",
                },
            )
            page = await context.new_page()

            # Capture the first response from this domain
            def _on_response(response):
                nonlocal http_status, response_headers
                if domain in response.url and http_status == -1:
                    http_status = response.status
                    response_headers = {k.lower(): v for k, v in response.headers.items()}

            page.on("response", _on_response)

            try:
                await page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
                body_raw = await page.content()
                body_sample = body_raw.lower()
            except Exception as nav_exc:
                await browser.close()
                return ProbeResult(
                    domain=domain,
                    protection_type=UNKNOWN,
                    recommended_strategy=_STRATEGY_MAP[UNKNOWN],
                    can_scrape_now=False,
                    http_status=http_status,
                    notes=f"Navigation error: {type(nav_exc).__name__}: {nav_exc}",
                )

            cookies_raw    = await context.cookies()
            cookie_names   = [c["name"] for c in cookies_raw]
            cf_headers     = [k for k in response_headers if k.startswith("cf-")]
            security_cookies = [
                c for c in cookie_names
                if any(p in c.lower() for p in ["__cf", "cf_clear", "_px", "datadome", "_abck"])
            ]
            await browser.close()

    except Exception as exc:
        return ProbeResult(
            domain=domain,
            protection_type=UNKNOWN,
            recommended_strategy=_STRATEGY_MAP[UNKNOWN],
            can_scrape_now=False,
            http_status=-1,
            notes=f"Browser launch error: {exc}",
        )

    protection_type, signals = _classify(
        http_status, response_headers, body_sample, cookie_names
    )

    logger.info(
        "[PROBE] %-30s HTTP %d → %-25s | %s",
        domain, http_status, protection_type, " | ".join(signals[:2]),
    )

    return ProbeResult(
        domain=domain,
        protection_type=protection_type,
        recommended_strategy=_STRATEGY_MAP[protection_type],
        can_scrape_now=protection_type in (OPEN, RATE_LIMITED),
        http_status=http_status,
        notes=" | ".join(signals),
        cf_headers=cf_headers,
        security_cookies=security_cookies,
    )


# =============================================================================
# Batch probe
# =============================================================================

async def probe_all_boards(
    domains: Optional[list[str]] = None,
    concurrency: int = 3,
) -> list[ProbeResult]:
    """
    Probe all domains concurrently (max `concurrency` at a time).
    If domains is None, probes all boards in selectors_registry.
    """
    if domains is None:
        from selectors_registry import SELECTOR_REGISTRY
        domains = list(SELECTOR_REGISTRY.keys())

    sem = asyncio.Semaphore(concurrency)

    async def _guarded(domain: str) -> ProbeResult:
        async with sem:
            return await probe_board(domain)

    return await asyncio.gather(*[_guarded(d) for d in domains])


# =============================================================================
# Standalone entrypoint
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    target_domains = sys.argv[1:] if len(sys.argv) > 1 else None
    print("\n🔍 GEMA Board Probe — reconnaissance scan starting...\n")

    results: list[ProbeResult] = asyncio.run(probe_all_boards(target_domains))

    # Sort: blocked first, then open
    results.sort(key=lambda r: (r.can_scrape_now, r.domain))

    # Print table
    print(f"\n{'BOARD':<32} {'HTTP':>5}  {'PROTECTION':<28} {'SCRAPE':>7}  STRATEGY")
    print("─" * 105)
    for r in results:
        status_icon = "✅" if r.can_scrape_now else "❌"
        print(
            f"{r.domain:<32} {r.http_status:>5}  "
            f"{_PROTECTION_LABEL.get(r.protection_type, r.protection_type):<28} "
            f"{status_icon:>7}  {r.recommended_strategy}"
        )

    # Summary
    can_scrape = sum(1 for r in results if r.can_scrape_now)
    blocked    = len(results) - can_scrape
    print(f"\n✅ Scrapeable: {can_scrape}   ❌ Blocked: {blocked}   Total: {len(results)}")

    # Save JSON
    output_path = "probe_results.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in results], f, indent=2, ensure_ascii=False)
    print(f"📄 Full results → {output_path}  (gitignored)\n")
