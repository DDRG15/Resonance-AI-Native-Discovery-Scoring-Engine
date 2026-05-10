# GEMA — Intelligent Job Scraping & Extraction Pipeline

> **G**lobal **E**xtraction & **M**atching **A**gent  

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
cd gema
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

### 3. Run

```bash
python -m streamlit run main.py
```

Open `http://localhost:8501` in your browser.

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
├── user_profile.yaml        # CV-derived personalization config
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

## Roadmap

- [ ] Per-domain jitter profiles (some boards need longer waits)
- [ ] Selector staleness alerts in sidebar (wired but not surfaced yet)
- [ ] Multi-page pagination support
- [ ] Credential-based login for wellfound and arc.dev
- [ ] Scheduled runs (cron-style, no manual trigger)
- [ ] Email digest as alternative to Discord

---

Built this because job hunting at scale deserved the same rigor I apply to production systems. This is the first public release and I'll continue to improve it when I have time.
