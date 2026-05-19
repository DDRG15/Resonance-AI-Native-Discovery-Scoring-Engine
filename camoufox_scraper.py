"""
camoufox_scraper.py — Firefox-based scraper for remoteok.com Cloudflare bypass.

WHY THIS MODULE EXISTS:
    remoteok.com is protected by Cloudflare Bot Fight Mode. Playwright's default
    Chromium browser uses BoringSSL (Google's TLS fork), whose JA3 fingerprint
    is reliably identified by Cloudflare as a headless browser. Result: 403.

    camoufox launches a real Firefox binary (NSS TLS stack), randomizes
    fingerprint parameters (user-agent, screen size, locale, timezone), and
    patches navigator.webdriver. Cloudflare sees a plausible Firefox request
    and serves the page normally.

INTERFACE:
    scrape_remoteok(title_queries, cfg) -> list[JobResult]

    Drop-in companion to GemaScraper. Results feed the same scorer/webhook
    pipeline. GemaScraper calls this coroutine and merges results before
    scoring — no special handling needed downstream.
"""

import asyncio
import logging
from urllib.parse import quote_plus

from camoufox.async_api import AsyncCamoufox

from models import JobResult, SearchConfig

logger = logging.getLogger(__name__)

_BASE_URL = "https://remoteok.com/?q={title}"
_WAIT_SELECTOR = "tr.job[data-id]"
_WAIT_TIMEOUT_MS = 20_000

# Selectors as fallback chains — first non-empty result wins
_TITLE_SELECTORS = ["h2[itemprop='title']", "h2.title", "h2"]
_COMPANY_SELECTORS = ["h3[itemprop='name']", "h3.company", "span.company"]
_SALARY_SELECTORS = ["span.salary", "div.salary", "span[class*='salary']"]
_LINK_SELECTORS = ["a.preventLink[href]", "a[href*='/remote-']", "td.company a[href]"]


async def _first_text(card, selectors: list[str]) -> str:
    """Return text from the first selector that matches, or empty string."""
    for sel in selectors:
        try:
            el = card.locator(sel).first
            if await el.count() > 0:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return ""


async def _first_href(card, selectors: list[str]) -> str:
    """Return href from the first selector that matches, or empty string."""
    for sel in selectors:
        try:
            el = card.locator(sel).first
            if await el.count() > 0:
                href = await el.get_attribute("href")
                if href:
                    return href.strip()
        except Exception:
            continue
    return ""


async def _scrape_one_query(
    browser,
    query: str,
    seen_urls: set[str],
) -> list[JobResult]:
    """Scrape a single title query from remoteok.com. Returns new JobResults."""
    url = _BASE_URL.format(title=quote_plus(query))
    results: list[JobResult] = []

    context = await browser.new_context()
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # Check for Cloudflare challenge page
        title_text = await page.title()
        if "just a moment" in title_text.lower() or "cloudflare" in title_text.lower():
            logger.warning("[CAMOUFOX] Cloudflare challenge on %s — skipping query: %s", url, query)
            return []

        try:
            await page.wait_for_selector(_WAIT_SELECTOR, timeout=_WAIT_TIMEOUT_MS)
        except Exception:
            logger.warning("[CAMOUFOX] No job cards found for query '%s' (timeout)", query)
            return []

        cards = page.locator("tr.job[data-id], tr[data-id]")
        count = await cards.count()

        for i in range(count):
            card = cards.nth(i)
            try:
                title = await _first_text(card, _TITLE_SELECTORS)
                company = await _first_text(card, _COMPANY_SELECTORS)
                salary_raw = await _first_text(card, _SALARY_SELECTORS) or None
                href = await _first_href(card, _LINK_SELECTORS)

                if not title or not company or not href:
                    continue

                # href is a relative path like /jobs/123456 — prefix the domain
                if href.startswith("/"):
                    href = "https://remoteok.com" + href

                if href in seen_urls:
                    continue
                seen_urls.add(href)

                results.append(JobResult(
                    title=title,
                    company=company,
                    url=href,
                    salary_raw=salary_raw or None,
                    source_domain="remoteok.com",
                ))
            except Exception as exc:
                logger.debug("[CAMOUFOX] Card parse error (card %d): %s", i, exc)
                continue

        logger.info("[CAMOUFOX] '%s' → %d jobs", query, len(results))
    except Exception as exc:
        logger.warning("[CAMOUFOX] Failed query '%s': %s", query, exc)
    finally:
        await context.close()

    return results


async def scrape_remoteok(
    title_queries: list[str],
    cfg: SearchConfig,
) -> list[JobResult]:
    """
    Scrape remoteok.com for all title queries using camoufox (Firefox).

    Returns a deduplicated list[JobResult] ready for the GEMA scorer pipeline.
    Never raises — returns an empty list on any unrecoverable error.
    """
    if not title_queries:
        return []

    all_results: list[JobResult] = []
    seen_urls: set[str] = set()

    try:
        async with AsyncCamoufox(headless=True) as browser:
            # Sequential per query — remoteok rate-limits concurrent requests
            for query in title_queries:
                batch = await _scrape_one_query(browser, query, seen_urls)
                all_results.extend(batch)
                if len(title_queries) > 1:
                    # Small pause between queries to avoid triggering rate limiter
                    await asyncio.sleep(1.5)
    except Exception as exc:
        logger.error("[CAMOUFOX] Fatal error: %s — returning empty list", exc)
        return []

    logger.info("[CAMOUFOX] Total remoteok.com results: %d", len(all_results))
    return all_results
