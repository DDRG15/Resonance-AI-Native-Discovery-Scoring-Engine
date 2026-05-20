# Resonance -- AI-Native Job Discovery Engine

*Extraction core: GEMA v2.1*

---

## What This Is

Manual job hunting across 16 boards takes 2-3 hours per session. Relevance filtering requires reading every listing. Repeating this daily at the volume required to find quality remote roles is not a sustainable use of engineering time.

GEMA eliminates that. One natural-language prompt triggers concurrent scraping across 16 boards, structured LLM extraction on every listing, and CV-aware scoring that surfaces Tier 1 results in the UI before the scrape finishes.

`Prompt -> SearchConfig -> 16-board scrape -> LLM extraction chain -> 115-point scoring -> tier buckets -> Discord alert`

---

## Architecture

```
User Prompt (natural language)
        |
        v
   NLP Engine          (LLM #1 generates SearchConfig)
        |
        v
   AI Auditor          (LLM #2 cross-checks the config)
        |
        v
   board_probe         (HTTP recon -- classifies Cloudflare/Login Wall/Open per domain)
        |
        v
   asyncio.gather      (16 domains, Semaphore(3) concurrent tabs)
      |         |
   Chromium   camoufox (Firefox/NSS -- remoteok.com Cloudflare Bot Fight Mode bypass)
      |         |
        |
        v
   LLM Extraction Chain   (Groq -> Gemini -> OpenRouter -> Cohere)
        |
        v
   Matcher / Scorer   (CV profile vs job requirements, 115-point model)
        |
        v
   asyncio.Queue -> single _db_writer_task   (zero SQLite lock contention)
        |
        v
   Tier Buckets   (T1: 80+ | T2: 50-79 | T3: 1-49 | T4: manual review)
        |
        v
   Discord Alerts + Excel Export + Notion/Sheets push
```

---

## What Changed in v2.1

**Two browser engines, not one.**
Chromium handles 15 boards. remoteok.com blocks Chromium's BoringSSL JA3 fingerprint at the TLS handshake -- Cloudflare identifies the browser before a single HTTP request completes. Firefox's NSS stack produces a different fingerprint that passes Bot Fight Mode. camoufox v0.4.11 provides the Firefox binary; the scraper routes remoteok.com through it transparently.

**Pre-scrape recon on every target.**
Launching 16 Playwright tabs against hard-blocked domains wastes browser startup time and burns IP reputation against rate-limiting infrastructure. board_probe makes a lightweight HTTP request to each domain's SRP URL before any browser opens, classifies the response (Cloudflare JS / Turnstile / Login Wall / Open), and removes hard-blocked domains from the gather target list. Browser tabs open only for domains that are reachable.

**Session cookies for login-gated boards.**
CookieVault injects session cookies from `cookies/<domain>.json` at BrowserContext creation time. Export from Chrome using EditThisCookie V3, drop the file in `cookies/`, and GEMA authenticates the browser session on the next run. No credential hardcoding.

**Auto-scheduler.**
Scrape sessions fire at configurable intervals (2h/4h/8h/12h/24h) without the UI open. A BackgroundScheduler daemon persists across Streamlit re-renders via `@st.cache_resource`. The scheduler stores a snapshot of SearchConfig at enable time and syncs it from the `update_config()` hook when the user confirms a new config.

**Ephemeral CV identity.**
Upload a PDF, DOCX, TXT, or MD file at runtime. GEMA parses it into a sanitized profile that drives `cv_match_score` and skill-overlap scoring. The profile lives in Streamlit session state only -- nothing is written to disk. A SHA-256 hash guard prevents re-calling the LLM on every re-render. Remove the file from the uploader and the profile is gone immediately.

---

## Capabilities

**16 boards scraped concurrently:**
himalayas.app, trueup.io, remote.co, weworkremotely.com, remoteok.com, workingnomads.com, news.ycombinator.com, wellfound.com, arc.dev, builtin.com, welcometothejungle.com, remotivated.com, posthog.com, greenhouse.com, jobspresso.co, python.org/jobs

**4-provider LLM fallback chain.**
Rate-limit or provider outage on any single provider does not stop a run. The chain falls to the next provider automatically. Groq's free tier rate-limits aggressively under the concurrent extraction load of a 16-board run (50-200 listings per session). Three independent fallback layers mean the probability of all four providers being down simultaneously during a 10-minute scrape window is operationally zero.

**115-point scoring model.**
CV skill overlap + audit signal matches + job quality signals (salary disclosed, remote confirmed, visa sponsorship) minus negative signals (contract-only flag, level mismatch). Tier 1 threshold: 80. Tier 2: 50-79. Tier 3: 1-49. Tier 4: manual review for unstructured salary text.

**God Mode selector override.**
Any board can change its HTML structure. When a domain's selectors are more than 30 days old, `is_selector_stale()` flags it. The sidebar's God Mode panel accepts new selectors at runtime without editing code or restarting the container.

---

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit 1.57 |
| Browser automation | Playwright 1.50 + playwright-stealth 2.0 |
| Firefox bypass | camoufox 0.4.11 |
| LLM providers | Groq, Google Gemini, OpenRouter, Cohere |
| Data validation | Pydantic v2 |
| Database | SQLite (WAL mode, async write queue, SHA-256 URL dedup) |
| Export | Pandas + openpyxl |
| Integrations | Notion API, Google Sheets (gspread) |
| Notifications | Discord Webhooks |
| Retry logic | Tenacity (exponential backoff) |
| Scheduler | APScheduler 3.10 (BackgroundScheduler) |
| Testing | pytest 8.2 (LLM callers mocked -- zero real API calls in CI) |

---

## Setup

### Prerequisites

- Docker Desktop (docker.com/products/docker-desktop)
- A Groq API key (free at console.groq.com) or any supported LLM key

### 1. Clone

```bash
git clone https://github.com/DDRG15/Resonance-AI-Native-Discovery-Scoring-Engine.git
cd Resonance-AI-Native-Discovery-Scoring-Engine
```

### 2. Run

```bash
cp .env.example .env
# Add at minimum: GROQ_API_KEY=gsk_...
docker compose up --build
```

Open http://localhost:8501. The setup wizard appears on first run if no LLM key is configured -- paste your key and click Save. The UI loads immediately after.

First boot downloads the Playwright base image and the camoufox Firefox binary. Total: approximately 1.3 GB. Subsequent starts are near-instant.

### 3. Upload your CV

Expand the **Your Profile** section in the left sidebar. Upload your CV (PDF, DOCX, TXT, or MD). GEMA parses it into a scoring profile that drives `cv_match_score` for the session. No YAML file to edit. No profile stored on disk.

### 4. Add session cookies for login-gated boards (optional)

wellfound.com, arc.dev, and welcometothejungle.com require an authenticated session. Export your session cookies using EditThisCookie V3 (Chrome extension). Save the JSON to `cookies/<domain>.json`.

```
cookies/
  wellfound.com.json
  welcometothejungle.com.json
  arc.dev.json
```

Cookie files are gitignored.

### 5. Notion integration (optional)

1. Create a Notion integration at notion.so/my-integrations and copy the API key
2. Create a database with these columns:

| Column | Type |
|---|---|
| Name | Title |
| Company | Text |
| URL | URL |
| Tier | Select |
| Match Score | Number |
| Salary | Text |
| Comments / Raw Salary | Text |
| Source | Text |
| Status | Select |

3. Share the database with your integration (··· -> Connections in Notion)
4. Copy the database ID from the URL and add to `.env`:

```
NOTION_API_KEY=secret_...
NOTION_DATABASE_ID=...
```

### 6. Google Sheets integration (optional)

1. Create a Google Cloud project, enable the Sheets API, create a service account
2. Download the JSON key to `credentials/google_service_account.json`
3. Share your target sheet with the service account's email (edit access)
4. Add to `.env`:

```
GOOGLE_SHEET_ID=...
GOOGLE_CREDENTIALS_PATH=credentials/google_service_account.json
```

---

## Engineering Decisions

### Why asyncio.Queue instead of direct DB writes per coroutine

16 scraper coroutines completing concurrently against a single SQLite file produce write contention. SQLite WAL mode handles concurrent reads but serializes writes -- if 3 coroutines call `db.write()` simultaneously, 2 block and one times out under load. The `asyncio.Queue` feeds a single `_db_writer_task` that processes results serially. The coroutines never touch the database -- they `put()` into the queue and continue. Zero lock contention.

### Why a 4-provider fallback chain

A single-provider setup fails when that provider has an outage or hits a rate limit. Groq's free tier rate-limits aggressively under the concurrent extraction load of a 16-board scrape (50-200 LLM calls per run). The chain falls from Groq to Gemini on rate-limit, then OpenRouter, then Cohere. All four providers have independent rate limit pools and independent uptime SLAs. A run does not fail unless all four are simultaneously unavailable.

### Why camoufox for remoteok.com

Cloudflare Bot Fight Mode identifies browsers by their TLS ClientHello hash (JA3 fingerprint). Chromium uses BoringSSL, which produces a fingerprint Cloudflare has flagged and blocks before any page content loads. Firefox uses NSS, which produces a different fingerprint that passes Bot Fight Mode. camoufox is a Firefox-based Playwright fork that exposes the NSS stack through the standard AsyncCamoufox API. remoteok.com routes through camoufox; all other boards use Chromium. The routing is a three-line conditional in `scraper.py`.

### Why board_probe before browser launch

Playwright tabs take 300-500ms to initialize. Opening 16 tabs against domains that are hard-blocked wastes that initialization time and fires TLS handshakes against infrastructure that is actively profiling the source IP. board_probe makes one HTTP request per domain, classifies the response (Cloudflare Turnstile / Bot Fight / Login Wall / Open), and returns the classification map in under 10 seconds total. The scraper removes hard-blocked domains before `asyncio.gather()` fires. The cost of the recon pass is always less than the cost of a single blocked browser tab.

### Why APScheduler 3.x instead of 4.x

APScheduler 4.x is alpha. Its API is async-first and incompatible with APScheduler 3.x in every non-trivial way. The Streamlit integration runs synchronous threading -- `BackgroundScheduler` from 3.x is a thread-based daemon that fits the model exactly. Migrating to 4.x buys nothing and requires rewriting the scheduler integration against an unstable API.

---

## Boundary Conditions

**Login-gated board access**
CookieVault injects session cookies exported from Chrome. This is wrong the moment the site rotates its session token -- wellfound rotates every 7 days approximately. The board returns a 302 redirect to the login page instead of search results, and the extraction pass returns zero structured listings. Resolution: re-export the cookie file using EditThisCookie V3 and replace `cookies/wellfound.com.json`.

**Cloudflare Turnstile**
board_probe classifies Turnstile-gated domains as hard-blocked and removes them from the scrape target list before browser launch. Turnstile requires interactive CAPTCHA solving. There is no automated path. This is wrong the moment a target board upgrades from Bot Fight Mode to Turnstile. Resolution: remove the domain from `selectors_registry.py` until a CAPTCHA-solver integration is added.

**Selector drift**
A job board can change its HTML structure without notice. When a domain's selectors are older than 30 days, `is_selector_stale()` flags it in the sidebar. The null extraction rate alert fires at 40% -- if more than 40% of SRP cards return null fields, the selectors are likely stale. Resolution: use God Mode in the sidebar to inject updated selectors without code changes, or update `selectors_registry.py` for a permanent fix.

**SQLite concurrency ceiling**
The async write queue eliminates lock contention for a single-user deployment. This is wrong the moment a second user connects to the same container -- two concurrent scrape sessions produce two `_db_writer_task` instances competing for the same file. Resolution: replace SQLite with PostgreSQL, add `user_id` foreign keys to `searches`, `jobs`, and `vault` tables.

**HN Jobs keyword filtering**
news.ycombinator.com/jobs does not support URL-level keyword filtering. The scraper retrieves all current postings regardless of the title query. The LLM extraction pass and scorer filter the noise downstream, but the scrape volume is always the full board. There is no resolution path short of implementing client-side keyword filtering after retrieval.

---

## Project Structure

```
gema/
|-- main.py                  # Streamlit UI -- command center
|-- scraper.py               # Playwright async engine + camoufox routing
|-- camoufox_scraper.py      # remoteok.com Firefox scraper
|-- nlp_engine.py            # LLM extraction, CV parsing, ephemeral profile generation
|-- matcher.py               # 115-point scoring + tier bucketing
|-- models.py                # Pydantic v2 contracts
|-- config.py                # Centralized env var loading (single source of truth)
|-- database.py              # SQLite WAL-mode async registry
|-- board_probe.py           # Pre-scrape domain recon
|-- cookie_vault.py          # Session cookie injection
|-- scheduler_service.py     # APScheduler BackgroundScheduler singleton
|-- setup_wizard.py          # First-run wizard -- no keys configured -> guided setup
|-- gema_industrial.py       # Headless batch scraper (CLI)
|-- selectors_registry.py    # 16 domain scraping contracts
|-- integrations/
|   |-- webhook_client.py    # Discord + Slack webhook delivery
|   |-- notion_client.py     # Notion API push
|   `-- sheets_client.py     # Google Sheets append
|-- scripts/
|   |-- build_beta.bat       # Builds sealed Docker image + assembles dist/ package
|   |-- start_gema.bat       # Beta launcher (double-click)
|   `-- docker-compose.beta.yml  # Compose file for pre-packaged distribution
|-- cookies/                 # Session cookies (gitignored)
|-- tests/
|   |-- conftest.py
|   |-- test_extraction.py
|   `-- test_cookie_vault.py
|-- .env.example
|-- requirements.txt
`-- Dockerfile
```

---

## Development Log

These are the actual failure sequence, not a sanitized post-mortem.

**playwright-stealth v2 removed stealth_async without a deprecation warning.**
Startup crash: `ImportError: cannot import name 'stealth_async'`. v2.0 replaced the entire API in a major version bump. First attempt: `from playwright_stealth import stealth` -- also crashed, because `stealth` in v2 is a module, not a callable. The correct v2 call is `from playwright_stealth import Stealth; await Stealth().apply_stealth_async(page)`. Never assume a new major version has a compatible API surface.

**load_dotenv() without an anchored path silently fails.**
All API keys showed as unset despite being in `.env`. `load_dotenv()` with no arguments searches the CWD -- when Streamlit runs, the CWD depends on the launch directory, not the project directory. Fix: `load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)`. Any project using `load_dotenv()` without an anchored path has this latent bug in any environment where the launch directory differs from the project root.

**Streamlit executes top-level code on every user interaction.**
Discord was receiving 2-3 start alerts per run. Streamlit re-runs the entire script on every UI event. Code not guarded by session state runs multiple times. Fix: `start_webhook_sent` flag in session state gates the alert. The rule: treat every top-level line in a Streamlit script as event-driven, not initialization code.

**Cohere removed command-r-plus on September 15, 2025 with no grace period.**
The fallback returned 404. The error message was buried in the response body, not the status line. Direct API test confirmed: `{"message": "model 'command-r-plus' was removed on September 15, 2025"}`. Replacement: `command-r-plus-08-2024`. LLM providers deprecate model identifiers on their schedule. Test the actual endpoint with the actual key before assuming the error is in the code.

**MAX_RATE_LIMIT_HITS=5 caused Cloudflare 403s to abort the entire 16-board run.**
Himalayas.app returned a 403. Five consecutive 403s from one domain killed the entire pipeline before any other board was scraped. Raised to 50. Individual domain blocks no longer abort the run. Anti-bot failures are logged and skipped, not treated as pipeline-fatal.

**Session state is unavailable across thread boundaries.**
The daemon thread running the scraper attempted to read `st.session_state.search_config`. Streamlit session state is a UI construct -- it does not exist in worker threads. Fix: capture the value on the main thread at spawn time and pass it via `args=`. The thread function receives `search_config` as a parameter and never touches session state.

**starlette version conflicts between projects are invisible to pip's resolver.**
`streamlit run main.py` crashed with `ImportError: cannot import name 'DEFAULT_EXCLUDED_CONTENT_TYPES' from 'starlette.middleware.gzip'`. starlette 0.38.6 was installed from another project in the same environment. streamlit 1.57 requires starlette>=0.40.0. pip's dependency resolver did not catch this conflict. Fix: explicitly pin `starlette==0.41.3` in `requirements.txt`. Any transitive dependency that has caused a production crash belongs in the pin list.

---

*GEMA is the extraction core. Resonance is where it runs.*

*Actively maintained. New features are added as ideas and time allow.*
