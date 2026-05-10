# Resonance — AI-Native Discovery & Scoring Engine

> *Powered by the GEMA extraction core*

---

## What Is GEMA?

GEMA is my personal job-hunting automation system. I built it to scrape multiple remote job boards simultaneously using a headless Chromium browser, send every job listing through a chain of LLMs for structured data extraction, score each result against my CV profile, and deliver real-time Discord notifications — all from a Streamlit UI I run locally.

I built it out of frustration. Manually scanning 10+ job boards daily, copy-pasting job descriptions, and filtering noise by hand is a full-time job in itself. GEMA automates the entire pipeline: scrape → extract → score → notify → export.

---

## Architecture

```
User Prompt (natural language)
        │
        ▼
   NLP Engine (LLM #1 generates SearchConfig)
        │
        ▼
   AI Auditor  (LLM #2 cross-checks the config)
        │
        ▼
   Playwright Scraper (16 domains, concurrent tabs)
        │
        ▼
   LLM Extraction Chain (Groq → Gemini → OpenRouter → Cohere)
        │
        ▼
   Matcher / Scorer (CV profile vs job requirements)
        │
        ▼
   Tier Buckets (T1: apply now │ T2: explore │ T3: skip │ T4: manual)
        │
        ▼
   Discord Alerts + Excel Export + Notion/Sheets push
```

---

## Features

- **Natural language search** — I describe what I want in plain English, the LLM parses it into a validated `SearchConfig`
- **4-provider LLM fallback chain** — Groq → Gemini → OpenRouter → Cohere. If one provider rate-limits or goes down, the next one picks up automatically
- **16 job board scrapers** — himalayas.app, trueup.io, remote.co, weworkremotely.com, remoteok.com, workingnomads.com, news.ycombinator.com, wellfound.com, arc.dev, builtin.com, welcometothejungle.com, remotivated.com, posthog.com, greenhouse.com, jobspresso.co, python.org/jobs
- **Anti-bot resilience** — Playwright stealth, User-Agent spoofing, per-domain circuit breakers, jitter between requests
- **Personalized scoring** — matches job tech stacks against a `user_profile.yaml` derived from my actual CV
- **3-stage Discord notifications** — start phrase → extraction report → end phrase
- **Excel export** — one-click `.xlsx` download sorted by match score
- **Notion + Google Sheets** integration for persistent tracking
- **Login-gated sites handled gracefully** — wellfound, arc.dev, welcometothejungle timeout silently and return zero results without crashing the run

---

## Tech Stack

| Layer | Technology |
|---|---|
| UI | Streamlit |
| Browser automation | Playwright + playwright-stealth |
| LLM providers | Groq, Google Gemini, OpenRouter, Cohere |
| Data validation | Pydantic v2 |
| Database | SQLite (WAL mode, async write queue) |
| Export | Pandas + openpyxl |
| Integrations | Notion API, Google Sheets (gspread) |
| Notifications | Discord Webhooks |
| Retry logic | Tenacity (exponential backoff) |
| HTTP | requests, urllib |

---

## Setup

### 1. Clone and install

```bash
git clone <your-repo-url>
cd Resonance-AI-Native-Discovery-Scoring-Engine
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure environment

```bash
cp .env.example .env
# Fill in your API keys in .env
```

Required keys (at minimum one LLM):
```
GROQ_API_KEY=
GEMINI_API_KEY=
OPENROUTER_API_KEY=
COHERE_API_KEY=
DISCORD_WEBHOOK_URL=
```

### 3. Create your profile

This is the part that makes GEMA personal. The extraction prompt is built around a YAML file that describes your skills, location, and projects. Without it, `cv_match_score` falls back to generic defaults and becomes useless.

```bash
cp user_profile.yaml.example user_profile.yaml
# Edit user_profile.yaml with your own CV data
```

Key fields:

| Field | What it does |
|---|---|
| `location` / `timezone` | Used to flag remote-only or timezone-restricted roles |
| `role` | Sets the framing for what counts as a match |
| `core_skills` | Scored against the job's tech stack to produce `cv_match_score` |
| `key_projects` | Injected into the prompt so the LLM understands your engineering identity |
| `audit_signals` | Keywords that, when found in a job description, push the match score above 0.8 |

`user_profile.yaml` is in `.gitignore` — it will never be committed or pushed.

### 4. (Optional) Notion integration

GEMA can push every job result to a Notion database as a Kanban card, color-coded by tier.

1. Create a Notion integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy the API key
2. Create a database in Notion with these exact columns:

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

3. Share the database with your integration (click ··· → Connections in Notion)
4. Copy the database ID from the URL: `notion.so/[workspace]/`**`<database-id>`**`?v=...`
5. Add to `.env`:

```
NOTION_API_KEY=secret_...
NOTION_DATABASE_ID=...
```

If these are not set, the integration is silently disabled — nothing breaks.

### 5. (Optional) Google Sheets integration

GEMA appends every job to a Google Sheet for longitudinal analysis (salary trends, volume by month, etc.).

1. Create a Google Cloud project and enable the Google Sheets API
2. Create a service account and download the JSON key file
3. Place the file at `credentials/google_service_account.json` (or set `GOOGLE_CREDENTIALS_PATH` in `.env`)
4. Share your target Google Sheet with the service account's email address (edit access)
5. Add to `.env`:

```
GOOGLE_SHEET_ID=...
GOOGLE_CREDENTIALS_PATH=credentials/google_service_account.json
```

The sheet will auto-create its header row on first run. If these are not set, the integration is silently disabled.

### 6. Run

```bash
docker compose up --build
```

This runs the official Microsoft Playwright image (handling all Chromium OS dependencies), installs requirements, mounts the SQLite database securely, and serves the UI at `http://localhost:8501`.

---

## Local Deployment via Docker

GEMA doesn't run bare-metal. The production-grade way to run it is through Docker Compose, which handles all system-level dependencies — including the headless Chromium OS libraries that Playwright requires — cleanly and reproducibly.

### Why Docker

Installing Playwright's Chromium dependencies directly on a host machine is fragile. The official Microsoft Playwright base image (`mcr.microsoft.com/playwright/python:v1.50.0-jammy`) ships with every required system library pre-installed. No `apt-get` hunting, no version conflicts, no "it works on my machine" problem.

### Boot the system

```bash
docker compose up --build
```

First boot takes 2–3 minutes (downloads the base image, installs Python deps, installs Chromium). Subsequent starts are near-instant.

### What Docker Compose does

| Concern | How it's handled |
|---|---|
| API keys | `.env` file mounted at runtime — never baked into the image |
| Database | `gema_registry.db` mounted as a persistent volume — survives container restarts with full WAL-mode integrity |
| Chromium | Pre-installed in the base image — no manual `playwright install` |
| Port | Container's `8501` mapped to host's `8501` |

### Access the dashboard

Once the container is up, open your browser and navigate to:

```
http://127.0.0.1:8501
```

or equivalently `http://localhost:8501`. The Streamlit UI loads immediately — no login, no setup screen.

### Stop

```bash
docker compose down
```

The database volume persists. Your seen-jobs registry and search vault survive the shutdown and are immediately available on the next `docker compose up`.

---

## How It Works

1. I type a search in plain English — e.g. *"Backend Python engineer, remote, salary above peanuts $$$k"*
2. The LLM extracts a structured `SearchConfig` (titles, salary gate, domains, etc.)
3. A second LLM audits the config and flags anything suspicious
4. I review the JSON, edit if needed, and hit **Confirm & Start Extraction**
5. Playwright opens up to 3 concurrent tabs and scrapes all registered job boards
6. Each raw listing goes through the LLM extraction chain to pull structured fields
7. The matcher scores every job against my CV profile
8. Results are bucketed into Tiers 1–4 and displayed in the UI
9. Discord gets a start ping, an extraction report, and a closing phrase
10. An Excel file is ready to download at the bottom of the results

---

## The Honest Development Log — Where It Broke and How I Fixed It

This section exists because most READMEs only show you the finished product. This one shows you the wall I hit, repeatedly, and what got me through it.

---

### Bug 1 — playwright-stealth v2 silently removed `stealth_async`

**What happened:** The scraper crashed on startup with `ImportError: cannot import name 'stealth_async' from 'playwright_stealth'`. The library had released version 2.0 which replaced its entire API without a deprecation warning.

**The wrong fix:** I changed `from playwright_stealth import stealth_async` to `from playwright_stealth import stealth`. This also crashed — `stealth` in v2 is a *module*, not a callable.

**The real fix:** I inspected the installed package with `inspect.iscoroutinefunction()` and `dir()` to discover the new API. The correct v2 call is:
```python
from playwright_stealth import Stealth
await Stealth().apply_stealth_async(page)
```

**Lesson:** Never assume a library's new major version has a compatible API. Check `dir()` before guessing.

---

### Bug 2 — `.env` file loading silently failing

**What happened:** All API keys showed as "Not Set" in the sidebar despite being in `.env`. The app was running but had no credentials.

**Root cause:** `load_dotenv()` with no arguments searches the *current working directory*. When Streamlit launches, the CWD is wherever I ran the command from — not necessarily the project folder.

**The fix:**
```python
load_dotenv(dotenv_path=Path(__file__).parent / ".env", override=True)
```
Anchoring the path to `config.py`'s own location makes it CWD-independent.

**Lesson:** `load_dotenv()` with no arguments is a time bomb in any project where the launch directory is unpredictable.

---

### Bug 3 — Streamlit double-firing the Discord start alert

**What happened:** Discord was receiving 2–3 "GEMA is starting" messages per run. Streamlit re-renders the entire script on every user interaction, so any code I didn't guard with session state was running multiple times.

**The fix:** I added a `start_webhook_sent` flag to `DEFAULTS` and guarded the alert:
```python
if not st.session_state.get("start_webhook_sent", False):
    send_discord_alert(random.choice(START_PHRASES))
    st.session_state["start_webhook_sent"] = True
```
The flag also resets in the Kill Switch so the next run gets exactly one ping.

**Lesson:** In Streamlit, treat every top-level line as if it runs on every mouse click. Session state is the only safe place to track "did this already happen."

---

### Bug 4 — Cohere API returning 404 "Model Not Found"

**What happened:** Cohere's fallback was throwing 404 errors. My first assumption was the endpoint URL (`.ai` vs `.com`). Several commits went back and forth on this.

**The real root cause:** `command-r-plus` was **permanently removed by Cohere on September 15, 2025** with no grace period. The error message was buried in the response body, not the status line. I confirmed it by hitting the API directly:
```
{"message": "model 'command-r-plus' was removed on September 15, 2025"}
```

**The fix:** I live-tested every candidate replacement model against the real API key before committing. `command-r-plus-08-2024` returned HTTP 200.

**Lesson:** LLM providers deprecate models without warning. Always test the actual endpoint with the actual key before assuming the code is wrong.

---

### Bug 5 — OpenRouter returning 404 (different cause)

**What happened:** OpenRouter also threw 404s. The model string was `google/gemma-2-27b-it:free`. My assumption was the `:free` suffix was invalid.

**What actually happened:** OpenRouter returned HTTP 200 when I tested directly with `google/gemma-2-27b-it` (without `:free`). Live API test confirmed it before any code changed.

**Lesson:** Test before committing. A wrong assumption + a quick commit = two commits to undo one problem.

---

### Bug 6 — `AttributeError: st.session_state has no attribute 'search_config'` in scraper thread

**What happened:** The daemon thread running the scraper tried to read `st.session_state.search_config` from inside the thread context. Streamlit's session state is not thread-safe — worker threads don't have access to it.

**The fix:** I captured the value on the main thread at spawn time and passed it via `args`:
```python
thread = threading.Thread(
    target=_scraper_thread,
    args=(st.session_state.search_config,),
    daemon=True,
)
```
The thread function receives `search_config` as a parameter and never touches `st.session_state` again.

**Lesson:** Streamlit session state is a UI construct. The moment you cross a thread boundary, treat it as gone.

---

### Bug 7 — Cloudflare 403s aborting the entire pipeline

**What happened:** Himalayas.app returned a Cloudflare 403. The global abort ceiling was set to 5 errors, so one blocked domain killed the entire 14-domain run before it could scrape anything else.

**The fix:** I raised `MAX_RATE_LIMIT_HITS` from 5 to 50 in both `config.py` and `.env`. I also injected a realistic Windows/Chrome User-Agent at browser context creation level:
```python
user_agent=(
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
```

**Lesson:** A pipeline that aborts on the first blocked domain is not production-ready. Anti-bot failures should be logged and skipped, not treated as fatal.

---

### Bug 8 — AI only populating 2 domains out of 14

**What happened:** The LLM generating the `SearchConfig` was only picking 2–3 domains from the registry even though 14 were registered. The scraper would finish in 30 seconds and return almost nothing.

**The fix:** After the LLM generates the config, I immediately override `target_domains` with the full registry:
```python
cfg = cfg.model_copy(update={
    "target_domains": list(selectors_registry.SELECTORS.keys())
})
```
The AI's domain selection is discarded entirely. All 14 boards get hit every run.

**Lesson:** Don't trust an LLM to know which tools are available. Inject that information at the code level, not the prompt level.

---

### Bug 9 — `NameError: name 'os' is not defined` in nlp_engine.py

**What happened:** Switching from `config.OPENROUTER_API_KEY` to `os.getenv('OPENROUTER_API_KEY')` in the API call headers introduced a `NameError` because `import os` was missing from `nlp_engine.py`.

**The fix:** One line: `import os` at the top of the file.

**Plot twist:** A missing `import os` took down the entire fallback chain. One line. The kind of thing that makes you stare at the screen for a second and then laugh at yourself.

**Lesson:** After changing how a module resolves names, always do a quick import smoke test before committing.

---

## Project Structure

```
gema/
├── main.py                  # Streamlit UI — the command center
├── scraper.py               # Playwright async scraper engine
├── nlp_engine.py            # LLM extraction and audit logic
├── matcher.py               # CV-vs-job scoring and tier bucketing
├── models.py                # Pydantic contracts for all data
├── config.py                # Centralized env var loading
├── database.py              # SQLite WAL-mode async registry
├── selectors_registry.py    # 16 domain scraping contracts
├── user_profile.yaml.example  # Template — copy to user_profile.yaml and fill with your CV
├── integrations/
│   ├── webhook_client.py    # Discord + Slack webhook logic
│   ├── notion_client.py     # Notion API push
│   └── sheets_client.py     # Google Sheets append
├── .env.example             # Template — copy to .env
├── requirements.txt
└── README.md
```

---

## Current Limitations

- **Login-gated boards** (wellfound, arc.dev, welcometothejungle) return zero results until I add credentials — handled gracefully but not scraped
- **Cloudflare-heavy boards** (remoteok.com) may still block me despite stealth mode
- **HN Jobs** doesn't support keyword filtering — returns all current postings regardless of my title query
- **Selector drift** — any job board can change its HTML structure; when that happens, I use the God Mode override in the sidebar to inject custom selectors without touching code

---

## What's Next

These are the improvements I know need to happen, roughly in priority order.

### Anti-Bot & Scraping

- **Per-domain jitter profiles** — RemoteOK and Cloudflare-heavy boards need 15–20s delays between requests; python.org can handle 2s. Currently all domains share the same global `JITTER_MIN/MAX` ceiling.
- **Rotating proxy support** — a residential proxy pool would bypass Cloudflare blocks that stealth mode alone can't handle. RemoteOK is effectively dead weight right now because of this.
- **WebGL / Canvas fingerprint randomization** — `playwright-stealth` patches the main vectors but a determined Cloudflare check can still fingerprint via WebGL renderer strings and canvas noise. Proper randomization per-context would close that gap.
- **Multi-page pagination** — the `next_page_btn` selectors are already defined in the registry for most boards. The scraper only hits page 1. Boards like himalayas and weworkremotely have 5–10 pages of results I'm not seeing.

### Login-Gated Boards

Three boards I want to unlock that currently return zero results because they require an account:

- **Wellfound (AngelList)** — best startup job board. Needs session cookie injection or OAuth flow.
- **Arc.dev** — high-signal remote tech roles. Same auth wall problem.
- **Welcome to the Jungle** — strong European remote market. Partial results without auth, full listings require account.

The plan: store encrypted session cookies in `.env` and inject them at `BrowserContext` creation. No credential hardcoding.

### Scheduling

- **Cron-style scheduled runs** — right now GEMA only runs when I manually trigger it. I want it to run at 8am and 6pm daily without me touching the UI. APScheduler wired into the Streamlit app, or a standalone daemon that writes results to the database.

### Notifications

- **Email digest** — a daily HTML email summary as an alternative to Discord. Not everyone wants their phone pinged at 8am by a webhook. Resend or SendGrid for delivery, templated with tier breakdown and top matches.
- **Slack formatting improvements** — the current Block Kit payload works but the layout is dense. Better formatting with salary prominently displayed and a direct apply button.

### UI

- **Sort and filter results** — currently results render in scrape order. I want sortable columns (by score, salary, company), filter by tier, and search within results.
- **Selector staleness warning** — `is_selector_stale()` is already implemented and returns True when a domain's selectors are > 30 days old. It's just not surfaced in the sidebar yet. One `st.warning()` call away.
- **Per-run history** — a tab that shows past runs with their tier counts and timestamps, pulled from the registry stats the DB already tracks.
- **Match reason display** — the `match_reasons` and `miss_reasons` fields are already populated per job but not shown in the UI. An expandable section per card would make the scoring transparent.

### Infrastructure

- **Credential-based Google Sheets auth** — currently requires a service account JSON file. OAuth2 device flow would be cleaner for personal use and removes the need to manage a service account.
- **Multi-user support** — right now the profile is hardcoded to one `user_profile.yaml`. With a login layer and per-user profiles, this could run as a shared tool.

---

Built this because job hunting at scale deserved the same rigor I apply to production systems. This is the first public release and I'll continue to improve it when I have time.
