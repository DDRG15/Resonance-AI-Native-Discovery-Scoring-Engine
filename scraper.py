"""
scraper.py — "The Blitz" Surface Sniper for Project GEMA.

ARCHITECTURE: asyncio.gather() Fan-Out with Semaphore Throttling

    One asyncio.run() call (at the thread boundary in run_scrape_session).
    Inside that single event loop, everything is pure async/await.
    No nested asyncio.run(). No multiple event loops. No thread.join().

    Task grid:
        For N domains and M title queries → N×M concurrent SRP tasks
        All tasks share ONE Browser and ONE BrowserContext (RAM efficiency).
        Each task opens ONE Page (tab) with its own User-Agent.
        Semaphore(SCRAPER_CONCURRENCY) limits active tabs to 3 at any moment.

        Total runtime = time_of_slowest_task
        NOT sum_of_all_tasks (the entire point of gather).

SQLITE CONCURRENCY SOLUTION:
    asyncio.gather() fans out N coroutines that all want to write to SQLite.
    Raw concurrent writes cause 'database is locked' OperationalError under
    WAL mode when the busy_timeout is exceeded.

    Solution: asyncio.Queue write-queue with a single dedicated writer task.

    Scraper tasks → asyncio.Queue.put(JobResult)   [non-blocking, O(1)]
    _db_writer_task → drains queue, calls mark_seen_batch() via run_in_executor
                       [one writer, one connection, zero lock contention]

    READ operations (filter_new_urls) are called via run_in_executor.
    WAL mode allows unlimited concurrent readers — no lock risk for reads.

SESSION DEDUPLICATION:
    Multiple SRP tasks for the same domain + different title queries may
    surface the same job URL (a "Senior SDET" search and an "Automation QA"
    search both return the same job posting). The _session_seen set (with
    atomic check-and-add, safe in asyncio's cooperative threading model)
    prevents duplicate entries without a round-trip to SQLite.

SURFACE SNIPING (The Blitz Rule):
    We extract 100% of surface metadata (title, company, salary, URL)
    directly from the Search Result Page. We NEVER navigate to the job
    detail page. No clicks. No new navigations per job. One page load
    per (domain × title_query) combination yields all available data.

WEBHOOK INTEGRATION:
    Tier 1 matches are scored inline during extraction using the lightweight
    score_job() function. The first Tier 1 hit triggers an immediate webhook
    ping. Subsequent hits buffer for the end-of-session summary.
"""

import asyncio
import logging
import os
import queue
import random
from datetime import datetime, timezone
from enum import Enum
from typing import Optional
from urllib.parse import quote_plus

from playwright.async_api import (
    async_playwright, Browser, BrowserContext, Page, Response,
)
from playwright_stealth import Stealth

import config
from selectors_registry import DomainSelectors, get_selectors
from database import GemaDatabase
from integrations.webhook_client import WebhookClient
from matcher import score_job
from models import JobResult, SearchConfig, ScrapeRunSummary, TieredJob

logger = logging.getLogger(__name__)


# =============================================================================
# Circuit Breaker
# =============================================================================

class CircuitState(Enum):
    CLOSED    = "CLOSED"
    OPEN      = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    """
    Per-domain circuit breaker with null-rate monitoring.

    Trips on:
        a) consecutive null extractions >= null_threshold (from domain config)
        b) null_rate > null_rate_alert_threshold across a full SRP run

    Both conditions independently trip the breaker.
    """

    def __init__(self) -> None:
        self._domains: dict[str, dict] = {}

    def _ensure(self, domain: str, threshold: int = config.CIRCUIT_BREAKER_THRESHOLD) -> None:
        if domain not in self._domains:
            self._domains[domain] = {
                "state":       CircuitState.CLOSED,
                "null_count":  0,
                "total":       0,
                "threshold":   threshold,
            }

    def record_success(self, domain: str) -> None:
        self._ensure(domain)
        self._domains[domain]["null_count"] = 0
        self._domains[domain]["total"] += 1

    def record_null(self, domain: str, threshold: int = config.CIRCUIT_BREAKER_THRESHOLD) -> None:
        self._ensure(domain, threshold)
        d = self._domains[domain]
        d["null_count"] += 1
        d["total"]      += 1
        if d["null_count"] >= d["threshold"]:
            d["state"] = CircuitState.OPEN
            logger.warning(
                "[CIRCUIT] %s OPEN after %d consecutive nulls. Activate God Mode.",
                domain, d["threshold"],
            )

    def check_null_rate(self, domain: str, alert_threshold: float) -> bool:
        """
        Checks if the null rate for this domain exceeds the alert threshold.
        Returns True if the breaker was tripped, False otherwise.
        """
        self._ensure(domain)
        d = self._domains[domain]
        if d["total"] < 5:
            return False   # not enough samples
        null_rate = d["null_count"] / d["total"]
        if null_rate > alert_threshold:
            d["state"] = CircuitState.OPEN
            logger.warning(
                "[DRIFT ALERT] %s: null rate %.0f%% exceeds threshold %.0f%%. "
                "SRP layout may have changed. Open God Mode.",
                domain, null_rate * 100, alert_threshold * 100,
            )
            return True
        return False

    def is_open(self, domain: str) -> bool:
        self._ensure(domain)
        return self._domains[domain]["state"] == CircuitState.OPEN

    def get_open_domains(self) -> list[str]:
        return [d for d, s in self._domains.items() if s["state"] == CircuitState.OPEN]


# =============================================================================
# Selector Utilities
# =============================================================================

def _resolve_selector(domain: str, sc: SearchConfig) -> Optional[DomainSelectors]:
    """
    Returns DomainSelectors for a domain, applying God Mode overrides.

    Uses dataclasses.replace() — the correct API for copying a dataclass
    with field overrides. sel.__dict__ is WRONG: it includes private dunder
    attrs (__dataclass_fields__, __dataclass_params__) that cause
    TypeError: __init__() got unexpected keyword argument on Python 3.10+.
    """
    import dataclasses
    sel = get_selectors(domain)
    if sel is None:
        logger.warning("[GOD MODE REQUIRED] No selectors found for '%s'", domain)
        return None

    if sc.custom_xpath:
        sel = dataclasses.replace(sel, title=[sc.custom_xpath] + sel.title)
        logger.info("[GOD MODE] Custom XPath injected at chain[0]: %s", sc.custom_xpath)

    if sc.custom_css:
        sel = dataclasses.replace(sel, job_card=[sc.custom_css] + sel.job_card)
        logger.info("[GOD MODE] Custom CSS injected at chain[0]: %s", sc.custom_css)

    return sel


async def _try_chain(element, chain: list[str]) -> Optional[str]:
    """
    Tries each CSS/XPath selector in chain, returns the first non-empty text.
    Silently continues on empty or exception — null is a valid fallback signal.

    This is the core of The Blitz's selector drift resilience. A single DOM
    change that breaks chain[0] automatically falls through to chain[1].
    """
    for selector in chain:
        if not selector:
            continue
        try:
            el = await element.query_selector(selector)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    return text
        except Exception:
            continue
    return None


async def _try_chain_attr(element, chain: list[str], attr: str = "href") -> Optional[str]:
    """Like _try_chain but returns an attribute value instead of inner_text."""
    for selector in chain:
        if not selector:
            continue
        try:
            el = await element.query_selector(selector)
            if el:
                val = (await el.get_attribute(attr) or "").strip()
                if val:
                    return val
        except Exception:
            continue
    return None


# =============================================================================
# Jitter
# =============================================================================

async def _jitter(label: str = "") -> None:
    """Stochastic delay to mimic human browsing cadence."""
    delay = random.uniform(config.JITTER_MIN, config.JITTER_MAX)
    logger.debug("Jitter[%s]: %.1fs", label, delay)
    await asyncio.sleep(delay)


# =============================================================================
# GemaScraper — The Blitz Engine
# =============================================================================

class GemaScraper:
    """
    Asynchronous, parallel, surface-only scraper.

    Lifecycle:
        scraper = GemaScraper(search_config, db, log_queue, ttl_hours, webhook)
        jobs, summary = await scraper.run()    # called from asyncio.run()
    """

    def __init__(
        self,
        search_config: SearchConfig,
        db:            GemaDatabase,
        log_queue:     queue.Queue,
        ttl_hours:     int           = 0,
        webhook:       Optional[WebhookClient] = None,
    ) -> None:
        self.config    = search_config
        self.db        = db
        self.log_queue = log_queue
        self.ttl_hours = ttl_hours
        self.webhook   = webhook or WebhookClient()
        self.circuit   = CircuitBreaker()
        self.summary   = ScrapeRunSummary()

        # Semaphore: max N concurrent tabs (default 3 — see SRE analysis)
        self._sem = asyncio.Semaphore(config.SCRAPER_CONCURRENCY)

        # Write queue: scraper tasks enqueue JobResults here.
        # Single _db_writer_task drains it — zero lock contention.
        self._write_q: asyncio.Queue = asyncio.Queue()

        # In-session deduplication set.
        # Safe without a Lock in asyncio because check+add is non-preemptable
        # (no await between them), and asyncio is single-threaded cooperative.
        self._session_seen: set[str] = set()

        # Abort event: set when rate limit hits exceed MAX_RATE_LIMIT_HITS.
        # All running gather tasks check this and return early.
        self._abort_event = asyncio.Event()

        # rate-limit hit counter — tracked on scraper instance, NOT on the
        # Pydantic ScrapeRunSummary model (Pydantic v2 rejects undeclared attrs)
        self._rate_hits: int = 0

        # Collected jobs from all tasks (populated via write queue)
        self._all_jobs: list[JobResult] = []

    def _log(self, msg: str) -> None:
        logger.info(msg)
        self.log_queue.put(msg)

    def _seen_this_session(self, url_hash: str) -> bool:
        """
        Atomic check-and-register for the in-session dedup set.
        Returns True if already seen (skip), False if new (process).
        Safe without asyncio.Lock: no await between check and add.
        """
        if url_hash in self._session_seen:
            return True
        self._session_seen.add(url_hash)
        return False

    # =========================================================================
    # DB Writer Task — the ONLY coroutine that touches SQLite writes
    # =========================================================================

    async def _db_writer_task(self) -> None:
        """
        Single consumer of the write queue. Serializes ALL SQLite inserts.

        Batching policy:
            Flush immediately when batch size reaches DB_WRITE_BATCH_SIZE.
            Flush on asyncio.TimeoutError after DB_WRITE_FLUSH_TIMEOUT seconds
            (catches the end-of-run tail where < BATCH_SIZE jobs remain).
            Flush remaining batch when None sentinel is received.

        run_in_executor wraps the synchronous mark_seen_batch() call so the
        event loop is not blocked during the disk write (typically < 5ms).
        """
        loop   = asyncio.get_event_loop()
        batch: list[JobResult] = []

        async def _flush(b: list) -> None:
            if not b:
                return
            jobs_copy = list(b)
            try:
                await loop.run_in_executor(None, self.db.mark_seen_batch, jobs_copy)
                self._log(f"[DB WRITER] Flushed {len(jobs_copy)} jobs to registry.")
            except Exception as exc:
                self._log(f"[DB WRITER] Write error (jobs NOT lost — in memory): {exc}")
            b.clear()

        while True:
            try:
                item = await asyncio.wait_for(
                    self._write_q.get(),
                    timeout=config.DB_WRITE_FLUSH_TIMEOUT,
                )
                if item is None:   # sentinel — shutdown signal
                    break
                batch.append(item)
                self._all_jobs.append(item)
                if len(batch) >= config.DB_WRITE_BATCH_SIZE:
                    await _flush(batch)

            except asyncio.TimeoutError:
                await _flush(batch)   # periodic flush for tail items

        await _flush(batch)   # final drain before writer exits

    # =========================================================================
    # Main Run — asyncio.gather() Fan-Out
    # =========================================================================

    async def run(self) -> tuple[list[JobResult], ScrapeRunSummary]:
        """
        Orchestrates the full scrape session.

        Single event loop, single asyncio.run() (called from run_scrape_session).
        No nested loops. No nested threads.

        Event loop topology:
            [main event loop]
                ├── _db_writer_task   (single writer, runs for whole session)
                ├── scrape_task(domain=himalayas, title=Senior SDET)
                ├── scrape_task(domain=himalayas, title=Automation QA)
                ├── scrape_task(domain=trueup,    title=Senior SDET)
                └── scrape_task(domain=trueup,    title=Automation QA)
                    (all bounded by Semaphore(3) — max 3 active at once)
        """
        self._log(f"[GEMA] Blitz run {self.summary.run_id[:8]} starting.")
        self.db.backup()

        domains = self.config.target_domains or ["himalayas.app", "trueup.io"]

        async with async_playwright() as pw:
            browser: Browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-extensions",
                    "--disable-background-timer-throttling",
                ],
            )
            # ONE shared context for all tabs — saves ~35MB vs per-tab contexts.
            # Cookies/session don't matter for public SRPs.
            # Per-tab User-Agent set via extra_http_headers on each Page.
            context: BrowserContext = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=os.getenv("IGNORE_HTTPS_ERRORS", "False").lower() == "true",
            )

            # Block 3rd-party assets at context level (applies to all pages)
            _BLOCKED = [
                "**/google-analytics.com/**", "**/googletagmanager.com/**",
                "**/fonts.googleapis.com/**", "**/fonts.gstatic.com/**",
                "**/doubleclick.net/**",       "**/hotjar.com/**",
                "**/segment.io/**",            "**/*.woff",  "**/*.woff2",
            ]
            for pattern in _BLOCKED:
                await context.route(pattern, lambda r, _p=pattern: r.abort())

            try:
                # Start the DB writer task — runs for the entire scrape session
                writer_task = asyncio.create_task(self._db_writer_task())

                # Build task grid: (domain × title_query) pairs
                # Each pair becomes one concurrent Page (tab) under the Semaphore
                async def _bounded(domain: str, title: str):
                    async with self._sem:
                        return await self._scrape_srp(context, domain, title)

                pairs = [
                    (domain, title)
                    for domain in domains
                    for title  in self.config.target_titles
                    if not self.circuit.is_open(domain)
                ]

                self._log(
                    f"[BLITZ] Fan-out: {len(pairs)} SRP tasks across "
                    f"{len(domains)} domain(s) × {len(self.config.target_titles)} title(s). "
                    f"Semaphore(concurrency={config.SCRAPER_CONCURRENCY})."
                )

                # Launch all tasks concurrently under the Semaphore
                results = await asyncio.gather(
                    *[_bounded(d, t) for d, t in pairs],
                    return_exceptions=True,
                )

                # Log any task-level exceptions (don't let one domain kill the run)
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        domain, title = pairs[i]
                        self._log(
                            f"[ERROR] Task ({domain}, '{title}') raised: "
                            f"{type(result).__name__}: {result}"
                        )
                        self.summary.errors.append(str(result))

                # Signal writer to flush and stop
                await self._write_q.put(None)
                await writer_task

            finally:
                await context.close()
                await browser.close()

        # Webhook: flush batch summary (no-op if < 2 Tier 1 hits)
        await self.webhook.flush_summary(self.summary)

        self.summary.new_processed    = len(self._all_jobs)
        self.summary.domains_aborted  = self.circuit.get_open_domains()
        self.summary.completed_at     = datetime.now(timezone.utc)

        self._log(
            f"[GEMA] Blitz complete. "
            f"New={self.summary.new_processed} | "
            f"Seen-skip={self.summary.skipped_seen} | "
            f"TTL-skip={self.summary.skipped_ttl} | "
            f"Errors={len(self.summary.errors)}"
        )
        return self._all_jobs, self.summary

    # =========================================================================
    # SRP Scraper — one tab per (domain × title) pair
    # =========================================================================

    async def _scrape_srp(
        self,
        context: BrowserContext,
        domain:  str,
        title:   str,
    ) -> list[JobResult]:
        """
        Surface Sniper: opens ONE page, loads ONE SRP, extracts ALL cards.
        No navigation beyond the initial goto(). No clicks. Pure surface data.
        """
        if self._abort_event.is_set():
            return []

        sel = _resolve_selector(domain, self.config)
        if sel is None:
            self._log(f"[SKIP] No selectors for '{domain}' — add via God Mode.")
            return []

        url = sel.search_url_template.format(title=quote_plus(title))
        self._log(f"[BLITZ] → {domain} | '{title}' | {url}")

        page: Page = await context.new_page()
        # Per-tab User-Agent rotation — breaks fingerprint correlation
        # without the RAM cost of isolated BrowserContexts
        await page.set_extra_http_headers(
            {"User-Agent": random.choice(config.USER_AGENT_POOL)}
        )
        await Stealth().apply_stealth_async(page)

        jobs: list[JobResult] = []
        try:
            response: Response = await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=config.PAGE_TIMEOUT_MS,
            )

            # Rate limit detection
            if response and response.status in (403, 429):
                self._rate_hits += 1
                self._log(
                    f"[ANTI-BOT] HTTP {response.status} on {domain} "
                    f"({self._rate_hits}/{config.MAX_RATE_LIMIT_HITS})"
                )
                if self._rate_hits >= config.MAX_RATE_LIMIT_HITS:
                    self._abort_event.set()
                    self._log("[ABORT] Rate limit ceiling reached. Aborting all tasks.")
                return []

            # Wait for SRP cards to render (handles React hydration delay)
            try:
                await page.wait_for_selector(
                    sel.wait_for_selector,
                    timeout=config.PAGE_TIMEOUT_MS,
                )
            except Exception:
                self._log(
                    f"[TIMEOUT] wait_for_selector failed on {domain} for '{title}'. "
                    f"Selector: {sel.wait_for_selector!r}"
                )
                return []

            # Small jitter after load — avoids synchronized requests from
            # all parallel tabs hitting the same server at the same ms
            await _jitter(f"{domain}-after-load")

            # Extract all cards from the SRP
            jobs = await self._extract_all_cards(page, domain, sel)

            # Null-rate check after a full SRP extraction
            self.circuit.check_null_rate(domain, sel.null_rate_alert_threshold)

        except Exception as exc:
            self._log(f"[ERROR] {domain}/'{title}': {type(exc).__name__}: {exc}")
            self.summary.errors.append(f"{domain}/{title}: {exc}")
        finally:
            await page.close()

        return jobs

    # =========================================================================
    # Card Extraction — Surface Metadata from SRP DOM
    # =========================================================================

    async def _extract_all_cards(
        self,
        page:   Page,
        domain: str,
        sel:    DomainSelectors,
    ) -> list[JobResult]:
        """
        Iterates over all job card elements on the SRP and extracts metadata.

        Delta Load is applied at the card level:
            1. Extract the card's href (canonical URL)
            2. Check _session_seen (in-memory, O(1), no await)
            3. Check db.is_seen via run_in_executor (DB read, WAL-safe)
            4. If new: extract full metadata, enqueue to write_queue
        """
        loop  = asyncio.get_event_loop()
        jobs: list[JobResult] = []

        # Try fallback chain for the job card container selector
        card_elements = []
        for card_sel in sel.job_card:
            try:
                card_elements = await page.query_selector_all(card_sel)
                if card_elements:
                    break
            except Exception:
                continue

        if not card_elements:
            self._log(f"[NULL-SRP] {domain}: 0 cards found. Check God Mode selectors.")
            self.circuit.record_null(domain, sel.null_threshold)
            return []

        self._log(f"[CARDS] {domain}: {len(card_elements)} cards found on SRP.")
        self.summary.total_urls_found += len(card_elements)

        for card in card_elements:
            if self._abort_event.is_set():
                break
            if self.circuit.is_open(domain):
                self._log(f"[CIRCUIT OPEN] {domain}: stopping card iteration.")
                break

            # ── Step 1: Extract the job URL from card ────────────────────────
            href = await _try_chain_attr(card, sel.link, attr="href")
            if not href:
                # Some boards wrap the entire card in <a> — check the card itself
                try:
                    href = await card.get_attribute("href")
                except Exception:
                    pass

            if not href:
                self.circuit.record_null(domain, sel.null_threshold)
                continue

            canonical = (
                href if href.startswith("http")
                else f"https://{domain}{href}"
            )
            url_hash = GemaDatabase.compute_hash(canonical)

            # ── Step 2: In-session dedup (no DB hit, no await needed) ────────
            if self._seen_this_session(url_hash):
                self.summary.skipped_seen += 1
                continue

            # ── Step 3: Delta Load — DB read via executor (WAL-safe) ─────────
            try:
                is_seen, reason = await loop.run_in_executor(
                    None, self.db.is_seen, canonical, self.ttl_hours
                )
            except Exception as exc:
                logger.warning("[DB READ] is_seen failed for %s: %s", canonical, exc)
                is_seen, reason = False, ""

            if is_seen:
                if "ttl" in reason:
                    self.summary.skipped_ttl += 1
                else:
                    self.summary.skipped_seen += 1
                continue

            # ── Step 4: Surface metadata extraction from card DOM ────────────
            job = await self._extract_card_metadata(card, canonical, domain, sel)

            if job is None:
                self.circuit.record_null(domain, sel.null_threshold)
                if self.circuit.is_open(domain):
                    self._log(
                        f"[CIRCUIT OPEN] {domain}: consecutive nulls exceeded "
                        f"threshold ({sel.null_threshold}). Activate God Mode."
                    )
                continue

            self.circuit.record_success(domain)

            # ── Step 5: Enqueue for DB write (non-blocking) ──────────────────
            await self._write_q.put(job)
            jobs.append(job)

            # ── Step 6: Inline tier scoring → immediate webhook on Tier 1 ────
            if self.webhook.is_enabled:
                try:
                    tiered = score_job(job, self.config)
                    if tiered.tier == "Tier 1":
                        await self.webhook.notify_tier1(tiered)
                except Exception as exc:
                    logger.debug("[WEBHOOK SCORE] Inline scoring error: %s", exc)

        return jobs

    async def _extract_card_metadata(
        self,
        card:   object,
        url:    str,
        domain: str,
        sel:    DomainSelectors,
    ) -> Optional[JobResult]:
        """
        Reads title, company, and salary from one card element.
        Uses fallback chains for each field — survives minor DOM changes.

        Salary is optional: None → Tier 4 routing in matcher.
        Title + Company are required: None on either → return None → circuit null.
        """
        try:
            title   = await _try_chain(card, sel.title)
            company = await _try_chain(card, sel.company)
            salary  = await _try_chain(card, sel.salary) if sel.salary else None

            if not title or not company:
                logger.debug(
                    "[NULL CARD] %s — title=%r company=%r",
                    url, title, company,
                )
                return None

            return JobResult(
                title=title,
                company=company,
                url=url,
                salary_raw=salary,
                source_domain=domain,
            )

        except Exception as exc:
            logger.debug("[EXTRACT ERROR] %s: %s", url, exc)
            return None


# =============================================================================
# Streamlit Bridge
# =============================================================================

def run_scrape_session(
    search_config: SearchConfig,
    db:            GemaDatabase,
    log_queue:     queue.Queue,
    ttl_hours:     int           = 0,
    webhook:       Optional[WebhookClient] = None,
) -> tuple[list[JobResult], ScrapeRunSummary]:
    """
    Synchronous entry point called from main.py's daemon thread.

    THE SINGLE asyncio.run() CALL:
        This is the ONE and ONLY call to asyncio.run() in the entire system.
        It creates a fresh event loop for the daemon thread (Streamlit's main
        thread has no event loop, so there is no conflict).

        Inside GemaScraper.run(), everything is pure async/await + gather.
        No nested asyncio.run(). No asyncio.get_event_loop().run_until_complete().

        The gather tasks share this single event loop — they can communicate
        via asyncio.Queue without cross-loop marshalling errors.

    main.py drives the non-blocking while-loop that drains the log_queue
    and yields to Streamlit's websocket every 500ms. The scraper writes to
    log_queue (thread-safe queue.Queue) from inside the event loop via
    self._log(), which calls queue.Queue.put() (safe from any thread/coroutine).
    """
    if webhook is None:
        webhook = WebhookClient()

    scraper = GemaScraper(search_config, db, log_queue, ttl_hours, webhook)
    return asyncio.run(scraper.run())
