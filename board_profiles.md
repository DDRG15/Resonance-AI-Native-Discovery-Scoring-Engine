# GEMA Board Profiles — 2026-05-25

Per-board reference: protection status, selector health, known issues, and recommended actions.
Generated from live probe data captured at 12:36 Lima time, scrape session `bt7dcaryc`.

---

## Quick Status Table

| Board | HTTP | Protection | Scraped | Selector Status | Notes |
|-------|------|------------|---------|-----------------|-------|
| himalayas.app | 200 | CF passthrough | Partial | Verified 2026-05-25 | Circuit-breaks at 3 nulls (threshold bug fixed in f6ecc70) |
| remoteok.com | 200 | CF passthrough | Yes | Verified 2026-05-10 | 1 job found; heavy dedup from prior runs |
| workingnomads.com | 200 | None | Yes | Unverified against live | tag-based search, not keyword |
| weworkremotely.com | 200 | CF bot mgmt | No | Verified selectors but 403 on search | Server blocks search queries; circuit-breaks |
| news.ycombinator.com | 200 | None | Yes | Unverified against live | No keyword filtering; returns all HN jobs |
| builtin.com | 200 | CF bot mgmt | Likely | Unverified against live | |
| arc.dev | 200 | None | Yes | Unverified against live | 9 session cookies injected |
| posthog.com | 200 | None | Yes | Unverified against live | Curated list, {title} param ignored |
| remotivated.com | 200 | None | Yes | Unverified against live | Emerging board, best-effort selectors |
| jobspresso.co | 200 | CF bot mgmt | Likely | Unverified against live | WP-based, stable HTML |
| python.org | 200 | None | Yes | Confirmed 2026-05-25 | `ol.list-recent-jobs` present in live HTML |
| startup.jobs | 200 | CF passthrough | Likely | Unverified against live | CSS class selectors may drift |
| greenhouse.com | 200 | CF bot mgmt | Likely | Unverified against live | Only Greenhouse's own jobs, not ATS platform |
| trueup.io | 403 | CF Turnstile | **BLOCKED** | N/A | Hard-blocked by Turnstile; skip until proxy |
| wellfound.com | 403 | CF JS Challenge | **BLOCKED** | N/A | Bot Fight Mode; skip until camoufox |
| welcometothejungle.com | 202 | Unknown | **BLOCKED** | N/A | 202 response suggests WAF interception |
| remote.co | timeout | Unknown | **BLOCKED** | N/A | Navigation timeout on probe; likely bot protection |

---

## Board Details

### himalayas.app

**Protection:** Cloudflare passthrough (200 OK, cf-ray present)
**Search URL:** `https://himalayas.app/jobs?q={title}&remote=true`
**Cookies:** 13 session cookies loaded from `cookies/himalayas.app.json` (expires 2026-05-27)

**Selector status (verified 2026-05-25 via Playwright probe):**
- `wait_for_selector`: `a[href*='/companies/'][href*='/jobs/']` — WORKS
- `job_card`: `article[class*='cursor-pointer']` — 20 cards found per page
- `link`: `a[href*='/companies/'][href*='/jobs/']:not([class*='absolute'])` — WORKS in isolation; returns title link with correct href
- `title`: same as link — `inner_text()` returns job title correctly
- `company`: `img[alt*='logo']` — returns alt text (e.g., "Fygaro logo"), needs stripping of " logo" suffix; fallback `a[href*='/companies/']:not([href*='/jobs/'])` also present

**DOM structure (2026-05-25):**
Each `<article>` contains two `<a>` elements pointing to the same `/companies/{co}/jobs/{slug}?ref=...` URL:
1. `<a class="absolute inset-0 z-0 rounded-xl">` — invisible overlay link, sr-only text "View job"
2. `<a class="relative text-xl font-medium text-gray-900">` — visible title link, contains job title text

The `:not([class*='absolute'])` filter correctly selects link #2.

**Known issues:**
- Circuit breaker threshold bug: `is_open()` initialized domain with `config.CIRCUIT_BREAKER_THRESHOLD=3` before per-domain `null_threshold=5` could be stored. Fixed in commit `f6ecc70`.
- With threshold=3, the circuit tripped after just 3 null cards across 3 concurrent title searches, killing all 51 remaining titles. With fix: threshold=5 per domain.
- Cookies expire 2026-05-27 — refresh via browser export before next scrape window.

**Action:** Run next scrape with circuit breaker fix to verify jobs flow through.

---

### remoteok.com

**Protection:** Cloudflare passthrough (200 OK, cf-ray present)
**Search URL:** `https://remoteok.com/?q={title}`
**Cookies:** None required (public access)

**Selector status (verified 2026-05-10):**
- `wait_for_selector`: `tr.job[data-id]` — confirmed working (28 cards per page)
- `job_card`: `tr.job[data-id]` — 28 results consistently
- `link`: `a.preventLink` — GEMA primary; used by camoufox for this board
- Uses camoufox browser (Playwright stealth) for improved fingerprint evasion

**Known issues:**
- Deduplication causes 0 new jobs on repeat runs (same 28 cards across all title queries)
- Most title searches return 0 new jobs — remoteok caches a fixed 28-card result set per query regardless of keyword specificity

**Action:** Reduce title count for remoteok (only unique 1-2 per angle). Very high dedup ratio.

---

### workingnomads.com

**Protection:** None (200 OK, clean response)
**Search URL:** `https://www.workingnomads.com/jobs?tag={title}`

**Selector status:** Unverified against live HTML.
- Uses tag-based search (`?tag=`), not keyword search — single-word tags work best
- Multi-word titles likely return 0 results; add single-word tags to target_titles

**Action:** Verify live HTML and test single-word tags ("python", "backend", "fastapi").

---

### weworkremotely.com

**Protection:** Cloudflare bot management (200 on probe, but 403 on actual search requests)
**Search URL:** `https://weworkremotely.com/remote-jobs/search?term={title}`

**Selector status (verified 2026-05-10):**
- `li.feature` — standard WWR job card selector, historically stable
- Returns 403 on search queries despite 200 on probe; server detects keyword search pattern

**Known issues:**
- Probe URL (homepage or generic page) returns 200 but search URL returns 403 for automated requests
- Rate limit circuit-breaks after MAX_RATE_LIMIT_HITS (5) consecutive 403s — now per-domain, not global (fix in `fd908ca`)
- `wait_for_selector` times out with current selector when 403 returns search-blocked HTML

**Action:** Check if a different search URL format bypasses blocking. Alternatively, use cookies. No action needed in the short term — circuit-breaking is correct behavior.

---

### news.ycombinator.com

**Protection:** None (200 OK, static HTML)
**Search URL:** `https://news.ycombinator.com/jobs?q={title}`

**Selector status:** Unverified against live, but HN's job board HTML is extremely stable.
- `tr.athing` — HN standard row selector, unchanged for 10+ years
- No keyword filtering: `?q=` param is ignored, returns all current job posts
- No salary data published

**Notes:** Results are not role-specific — all HN jobs returned regardless of title query. Deduplication prevents double-counting across 51 title queries. Effective for discovering small-team/startup roles not on other boards.

---

### builtin.com

**Protection:** Cloudflare bot management (200 OK, __cf_bm cookie)
**Search URL:** `https://builtin.com/jobs/remote?search={title}`

**Selector status:** Unverified against live HTML.
- `div[data-id]` — guessed selector, may not match actual DOM
- Likely needs God Mode verification

**Action:** Run God Mode on builtin.com to capture actual job card structure.

---

### arc.dev

**Protection:** None detected (200 OK, clean)
**Search URL:** `https://arc.dev/remote-jobs?q={title}`
**Cookies:** 9 session cookies from `cookies/arc.dev.json` — injected automatically

**Selector status:** Unverified against live HTML.
- `div[data-testid='job-card']` — stable testid pattern if present
- Session cookies may enable full listing access (arc.dev restricts some results to logged-in users)

**Action:** Verify testid presence in live HTML. With session cookies, more jobs should be accessible.

---

### posthog.com

**Protection:** None (200 OK, clean)
**Search URL:** `https://posthog.com/cool-tech-jobs?q={title}`

**Selector status:** Unverified.
- Curated list of ~20-30 company job boards, not a searchable job board
- `{title}` param is ignored — returns the same list regardless of query
- All 51 title queries will return the same results; heavy deduplication expected

**Notes:** High-signal source for companies with strong engineering cultures (PostHog selects for growth-stage tech companies). Worth keeping but limit to 1-2 title queries.

**Action:** Reduce to 1 query per session. Verify `li[class*='job']` selector against live page.

---

### remotivated.com

**Protection:** None (200 OK, clean)
**Search URL:** `https://remotivated.com/jobs?q={title}`

**Selector status:** Best-effort, unverified.
- Emerging board with small listing count
- `.job-card` selector is a guess; needs live verification

**Action:** Run God Mode or URL probe to verify actual card structure.

---

### jobspresso.co

**Protection:** Cloudflare bot management (200 OK, __cf_bm cookie)
**Search URL:** `https://jobspresso.co/remote-work/?search_keywords={title}`

**Selector status:** Verified pattern (WP Job Manager boards are highly consistent).
- `li.job_listing` — standard WP Job Manager selector, same as remote.co
- `a[href*='/remote-work/']` — jobspresso-specific link pattern

**Notes:** WordPress + WP Job Manager boards (jobspresso.co, remote.co) share the same HTML structure. Selectors are stable across WP Job Manager instances.

---

### python.org

**Protection:** None (200 OK, clean static HTML)
**Search URL:** `https://www.python.org/jobs/?q={title}`

**Selector status (confirmed 2026-05-25 via urllib probe):**
- `ol.list-recent-jobs` — found 1 instance in live HTML (confirmed)
- `ol.list-recent-jobs li` — job card container (confirmed)
- `h2 a` — job title link (standard HTML pattern, highly stable)
- `span.listing-company-name` — company name (official PSF markup)

**Notes:** No salary data. Listings are Python-specific by definition. Results depend on what companies post directly to the PSF board (typically quality Python-first roles).

---

### startup.jobs

**Protection:** Cloudflare passthrough (200 OK, cf-ray present, 4 job signals)
**Search URL:** `https://startup.jobs/?q={title}&remote=true`

**Selector status:** Unverified against live HTML.
- `div.css-1wts5rl` — CSS hash class selector, HIGH DRIFT RISK (hash changes on rebuild)
- Should be replaced with more stable selectors (`div[data-cy='job-card']`, aria attrs)

**Action:** Verify live HTML and replace CSS hash class with data attribute selector.

---

### greenhouse.com

**Protection:** Cloudflare bot management (200 OK, __cf_bm cookie)
**Search URL:** `https://www.greenhouse.com/careers/opportunities?q={title}`

**Selector status:** Unverified.
- This is Greenhouse's own careers page (a handful of internal roles), NOT the ATS platform used by other companies
- Very small listing count — likely <10 results at any time
- May not be worth including in the scrape rotation

**Action:** Check if this board has significant volume. If <5 unique jobs per run, remove from rotation.

---

### trueup.io

**Protection:** Cloudflare Turnstile (403, cf-ray + cf-mitigated headers)
**Status:** HARD BLOCKED

TrueUp returns 403 with Turnstile challenge on every automated request. Bot Fight Mode is active (cf-mitigated header confirms). Standard Playwright cannot solve Turnstile challenges.

**Unblock options:**
1. Residential proxy + Playwright (bypasses IP-based detection)
2. camoufox with fingerprint randomization
3. Manual cookie export after solving Turnstile in browser

**Action:** Skip until residential proxy or camoufox integration.

---

### wellfound.com

**Protection:** Cloudflare JS Challenge (403, cf-ray)
**Status:** HARD BLOCKED

Bot Fight Mode is active. JS challenge requires Cloudflare to validate browser fingerprint. Standard Playwright fails.

**Unblock options:**
1. camoufox browser — solves JS challenges via fingerprint spoofing
2. Session cookie injection after manual login

**Action:** Priority target for camoufox integration (high-quality startup jobs).

---

### welcometothejungle.com

**Protection:** Unknown (HTTP 202)
**Status:** BLOCKED

HTTP 202 "Accepted" on a page navigation is atypical — likely a WAF or challenge interceptor queuing the request. Pattern not recognized by board_probe classifier.

**Notes:** 202 may indicate an async queue (request accepted, response pending). May need specific headers or cookie state to get actual page content.

**Action:** Manual investigation. Try loading in browser and capturing exact response headers.

---

### remote.co

**Protection:** Unknown (navigation timeout on probe)
**Status:** BLOCKED

Probe timed out during navigation — server accepted connection but did not return a response within 20s. Likely a throttled response (rate limiting via delay, not 403).

**Notes:** WP Job Manager board (same structure as jobspresso.co). If it loads, selectors are correct. The issue is reaching it.

**Action:** Test with longer timeout (60s) or different User-Agent. May work with residential proxy.

---

## Boards with Session Cookies Available

| Board | Cookie File | Expires | Auto-Injected |
|-------|------------|---------|---------------|
| himalayas.app | `cookies/himalayas.app.json` | 2026-05-27 | Yes |
| arc.dev | `cookies/arc.dev.json` | Unknown | Yes |

Both files are gitignored. Refresh before cookies expire.

---

## Known Recurring Issues

**1. Concurrent same-domain requests trigger 403s**
When 3 title searches hit the same board simultaneously, the server sees 3 requests from the same IP within milliseconds. Boards with CF bot mgmt (weworkremotely, jobspresso) detect this pattern and return 403.

Mitigation: Add per-domain inter-request jitter. Currently only tab-level jitter is applied.

**2. CSS hash selectors drift on rebuild**
startup.jobs uses `div.css-1wts5rl` — a hash generated by CSS-in-JS tools. This hash changes every time the site rebuilds. When it drifts, wait_for_selector times out and the board returns 0 jobs without logging the actual cause.

Mitigation: Replace hash selectors with `data-*` or ARIA attributes. Use God Mode to re-capture after drift.

**3. HN and PostHog don't filter by keyword**
Both boards return the same results regardless of the 51 title queries. With deduplication, only the first query produces results — the other 50 are wasted requests.

Mitigation: Set `target_domains` overrides to run these boards with only 1 query.

**4. remoteok.com dedup saturation**
RemoteOK returns the same 28-card page regardless of keyword specificity. After the first scrape, all 28 are seen. Every subsequent query returns 0 new jobs.

Mitigation: Run remoteok at most once per 24-48h TTL window, not 51 times per session.
