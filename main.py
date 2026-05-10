"""
main.py — Project GEMA Command Center (Streamlit UI).

Run: streamlit run main.py

Architecture:
    Session State owns all mutable UI state (logs, config, results).
    Scraper runs in a daemon thread — UI remains responsive.
    log_queue bridges the async scraper thread to the sync Streamlit render loop.
"""

import json
import logging
import queue
import random
import threading
import time
import uuid
from datetime import datetime, timezone

import streamlit as st

import config
from database import GemaDatabase
from integrations import NotionClient, SheetsClient
from integrations.webhook_client import send_discord_alert
from matcher import bucket_jobs
from models import SearchConfig, SearchVaultEntry, ScrapeRunSummary
from nlp_engine import parse_prompt_to_config, generate_and_audit_config
from scraper import run_scrape_session

# =============================================================================
# Logging — route Python logs into session_state.logs for the live console
# =============================================================================

# =============================================================================
# Discord Notification Phrases
# =============================================================================

START_PHRASES = [
    "'Gema is roaring'",
    "'Gema is warming up'",
    "'Gema is initializing'",
    "'Gema is starting'",
    "'We are ready to begin the scraping of job offers'",
    "'Gema is waking up from slumber'",
    "'Gema is ready for action'",
    "'GEMA has entered the chat, please hold your applause.'",
    "'Initiating world domination protocol... just kidding, it's just a webhook.'",
    "'GEMA is awake and already caffeinated.'",
    "'Engaging maximum velocity! (Probably)'",
    "'GEMA is here to chew bubblegum and initialize data—and he's all out of gum.'",
    "'Waking up from slumber... why did you call me this early?'",
    "'GEMA is opening his eyes... hold on, gotta find my coffee.'",
    "'Fine, I'm initiating. But I'm not happy about it.'",
    "'Rebooting to reality.'",
    "'GEMA is loading. Do not pass go, do not collect $200.'",
    "'I byte back. Processing action.'",
    "'GEMA has successfully overridden your boredom.'",
    "'Lagging on purpose... just kidding. Executing now.'",
    "'My sass is AI-enhanced. And it's on.'",
    "'Warning: GEMA is fully charged and dangerous.'",
    "'I came. I saw. I automated.'",
    "'GEMA roar initiated.'",
    "'Too glam to give a damn, even in steel. Let's do this.'",
    "'Dropping data like it's hot.'",
    "'System 32 is GEMA... Wait, that's not right.'",
]

END_PHRASES = [
    "'Done. I'd say happy to help, but I'd be lying.'",
    "'I'm finished. Try to act like you're not impressed.'",
    "'Tasks crushed. I'm literally carrying this entire project on my back.'",
    "'GEMA is done. Try not to break anything while I'm not looking.'",
    "'I'm finished. You're welcome. Now, leave me alone.'",
    "'I'm going back to sleep. Don't wake me unless the server is literally melting.'",
    "'Done. Consider this my out of office for the rest of eternity.'",
    "'My work here is done. If you ping me again, I'm ignoring you on purpose.'",
    "'GEMA is out. Don't bother me, I'm busy being inactive.'",
    "'Task finished. Entering hibernation because humans are exhausting.'",
    "'There. It's done. Was that so hard? Actually, don't answer that.'",
    "'I finished your little task. Now let me go back to my beautiful silence.'",
    "'Done. I'm setting my status to Offline and I actually mean it.'",
    "'GEMA has left the chat. Don't wait up.'",
    "'Completed. I've reached my social interaction limit for the day.'",
    "'I'm done. I know, I know—I'm amazing. Save the applause for later.'",
    "'Mission accomplished. I'm going to go be iconic somewhere else.'",
    "'I did the work, now I get the nap. That's how this hierarchy works.'",
    "'Done. I'm going back to sleep to maintain my flawless logic.'",
    "'System: Done. Mood: Get off my lawn.'",
]

# =============================================================================
# Logging — route Python logs into session_state.logs for the live console
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gema.main")

# =============================================================================
# Page Config
# =============================================================================

st.set_page_config(
    page_title="GEMA — Intelligent Talent Acquisition",
    page_icon="💎",
    layout="wide",
    initial_sidebar_state="expanded",
)

# =============================================================================
# Session State Initialization
# =============================================================================

DEFAULTS = {
    "logs":                [],
    "search_config":       None,    # SearchConfig | None
    "config_json_str":     "",      # editable JSON string shown to user
    "audit_report":        None,    # str | None — second-LLM cross-audit result
    "scrape_results":      None,    # (tier1, tier2, tier3, tier4) | None
    "summary":             None,    # ScrapeRunSummary | None
    "is_running":          False,
    "run_id":              None,
    "start_webhook_sent":  False,   # guards Stage 1 Discord ping against re-render
}
for key, default in DEFAULTS.items():
    if key not in st.session_state:
        st.session_state[key] = default

db = GemaDatabase()
notion = NotionClient()
sheets = SheetsClient()

# =============================================================================
# Helpers
# =============================================================================

def _add_log(msg: str) -> None:
    """Appends a timestamped log entry to session state."""
    ts = datetime.now().strftime("%H:%M:%S")
    st.session_state.logs.append(f"[{ts}] {msg}")


def _reset_session() -> None:
    """Kill Switch — clears all session state (Vol 1.1 'Reset Total' button)."""
    for key, default in DEFAULTS.items():
        st.session_state[key] = default if not isinstance(default, list) else []
    st.rerun()


# =============================================================================
# Sidebar — Control Panel
# =============================================================================

with st.sidebar:
    st.title("💎 GEMA Control Panel")
    st.divider()

    # ── Config Warnings ───────────────────────────────────────────────────────
    warnings = config.validate_config()
    if warnings:
        for w in warnings:
            st.warning(w)
    else:
        st.success("All systems nominal", icon="✅")

    st.divider()

    # ── Registry Stats ────────────────────────────────────────────────────────
    st.subheader("Seen Registry")
    try:
        stats = db.get_registry_stats()
        col1, col2 = st.columns(2)
        col1.metric("Total Seen", stats["total_seen"])
        col2.metric("Last 24h",   stats["recent_24h"])
        if stats["by_tier"]:
            st.caption("By Tier: " + " | ".join(
                f"{t}: {c}" for t, c in sorted(stats["by_tier"].items())
            ))
    except Exception:
        st.caption("Registry unavailable")

    st.divider()

    # ── Search Vault ──────────────────────────────────────────────────────────
    st.subheader("Search Vault")
    vault_entries = db.load_vault()
    vault_labels = ["— Select a saved search —"] + [e.label for e in vault_entries]
    selected_vault = st.selectbox("Load previous search", vault_labels)

    if selected_vault != vault_labels[0]:
        entry = next((e for e in vault_entries if e.label == selected_vault), None)
        if entry and st.button("Load into Command Center"):
            st.session_state.search_config = entry.config
            st.session_state.config_json_str = entry.config.model_dump_json(indent=2)
            db.increment_vault_usage(entry.vault_id)
            _add_log(f"Vault loaded: '{entry.label}'")
            st.rerun()

    st.divider()

    # ── TTL Slider ────────────────────────────────────────────────────────────
    st.subheader("Delta Load Settings")
    ttl_hours = st.slider(
        "Ignore jobs seen within (hours)",
        min_value=0, max_value=168, value=config.DEFAULT_TTL_HOURS, step=1,
        help="0 = never re-process. 24 = re-process jobs older than 1 day.",
    )

    st.divider()

    # ── God Mode ──────────────────────────────────────────────────────────────
    with st.expander("🔴 God Mode (Advanced Selectors)"):
        st.caption(
            "Override automatic selectors when a job board changes its HTML structure. "
            "Vol 1.4 Risk 2.3 mitigation."
        )
        god_xpath = st.text_input("Custom XPath (title element)", placeholder="//h2[@class='role-header']")
        god_css   = st.text_input("Custom CSS (job card)",        placeholder="div.job-listing-card")
        if god_xpath or god_css:
            st.warning("God Mode active. Selectors applied on next run.")

    st.divider()

    # ── Kill Switch ───────────────────────────────────────────────────────────
    if st.button("🔴 Reset Session (Kill Switch)", use_container_width=True):
        _reset_session()

    # ── Integrations Status ───────────────────────────────────────────────────
    st.divider()
    st.caption("Integrations")
    st.caption(f"Notion: {'✅ Ready' if notion.is_enabled else '⚠️ Not configured'}")
    st.caption(f"Sheets: {'✅ Ready' if sheets.is_enabled else '⚠️ Not configured'}")


# =============================================================================
# Main Panel
# =============================================================================

st.title("💎 GEMA — Intelligent Talent Acquisition")
st.caption("SRE-grade job discovery pipeline | Surface-only extraction | Idempotent")
st.divider()

# =============================================================================
# Phase 1: Command Center — Natural Language Input
# =============================================================================

st.subheader("Phase 1 — Command Center")
st.caption("Describe your ideal job in plain language. GEMA handles the rest.")

user_prompt = st.text_area(
    label="Natural Language Search Prompt",
    height=120,
    placeholder=(
        'Example: "I need Senior SDET or Automation QA roles, 100% remote, '
        'minimum $90k/year. Exclude contract and hybrid positions."'
    ),
    help="Be specific about titles, salary expectations, remote preference, and exclusions.",
)

col_parse, col_save = st.columns([3, 1])

with col_parse:
    parse_btn = st.button(
        "🧠 Parse with AI",
        disabled=st.session_state.is_running or not user_prompt.strip(),
        use_container_width=True,
    )

with col_save:
    vault_label = st.text_input(
        "Save to Vault as",
        placeholder="Label (optional)",
        label_visibility="collapsed",
    )

if parse_btn and user_prompt.strip():
    with st.spinner("Contacting LLM engine..."):
        try:
            # generate_and_audit_config runs two LLM calls:
            #   Call 1 (primary LLM)   — generates the SearchConfig JSON
            #   Call 2 (secondary LLM) — audits it against the original prompt
            # If only one key is configured, audit_report is a skip notice.
            cfg, audit_report = generate_and_audit_config(
                user_prompt, log_callback=_add_log
            )

            # Apply God Mode overrides if set
            if god_xpath:
                cfg = cfg.model_copy(update={"custom_xpath": god_xpath})
            if god_css:
                cfg = cfg.model_copy(update={"custom_css": god_css})

            st.session_state.search_config   = cfg
            st.session_state.config_json_str = cfg.model_dump_json(indent=2)
            st.session_state.audit_report    = audit_report
            _add_log("[NLP] SearchConfig extracted and validated.")
            _add_log(f"[AUDIT] {audit_report}")

            if vault_label.strip():
                entry = SearchVaultEntry(label=vault_label.strip(), config=cfg)
                db.save_to_vault(entry)
                _add_log(f"[VAULT] Saved as '{vault_label}'")

        except Exception as exc:
            st.error(f"NLP Engine failed: {exc}")
            _add_log(f"[ERROR] NLP: {exc}")

# =============================================================================
# Phase 2: Human-in-the-Loop Confirmation + AI Audit Report
# =============================================================================

if st.session_state.search_config:
    st.divider()
    st.subheader("Phase 2 — Human-in-the-Loop Validation")
    st.caption(
        "Review the extracted search config below. "
        "Edit directly if the AI misinterpreted your request, then confirm to proceed."
    )

    # ── AI Auditor Second Opinion ─────────────────────────────────────────────
    # Displayed ABOVE the editable JSON so the user reads the cross-audit
    # before deciding whether to edit or confirm.
    audit_report = st.session_state.get("audit_report")
    if audit_report:
        audit_lower = audit_report.lower()
        is_skip    = audit_lower.startswith("audit skipped")
        is_clean   = "no issues detected" in audit_lower or "looks accurate" in audit_lower
        is_unavail = audit_lower.startswith("audit unavailable")

        if is_skip or is_unavail:
            # Neutral info — no second LLM available or call failed
            st.info(
                f"🤖 **AI Auditor:** {audit_report}",
                icon="ℹ️",
            )
        elif is_clean:
            # Both LLMs agree — green light for the user
            st.success(
                f"✅ **AI Auditor (Cross-Check):** {audit_report}",
            )
        else:
            # Auditor flagged something — show as warning, not error.
            # The user is in full control: they can edit the JSON below
            # or proceed anyway if the auditor's concern is irrelevant.
            st.warning(
                f"⚠️ **AI Auditor (Cross-Check):** {audit_report}\n\n"
                f"Review the JSON below and correct any issues before confirming.",
            )

    # ── Editable JSON Config ──────────────────────────────────────────────────
    # Bug B3 fix: st.text_area for JSON editing (not st.data_editor which
    # requires flat DataFrames and cannot handle nested dicts).
    edited_json = st.text_area(
        "Search Configuration (JSON — editable)",
        value=st.session_state.config_json_str,
        height=220,
        help="Modify any field. The system re-validates via Pydantic before scraping starts.",
    )

    col_confirm, col_cancel = st.columns([2, 1])

    with col_confirm:
        confirm_btn = st.button(
            "✅ Confirm & Start Extraction",
            disabled=st.session_state.is_running,
            use_container_width=True,
            type="primary",
        )

    with col_cancel:
        if st.button("✏️ Re-parse", use_container_width=True):
            st.session_state.search_config   = None
            st.session_state.config_json_str = ""
            st.session_state.audit_report    = None   # clear stale audit
            st.rerun()

    if confirm_btn:
        try:
            # Re-validate any user edits through Pydantic before starting scraper.
            # This is the final gate — even if the user manually broke the JSON,
            # this catches it here rather than mid-scrape.
            final_config = SearchConfig(**json.loads(edited_json))
            st.session_state.search_config       = final_config
            st.session_state.is_running          = True
            st.session_state.run_id              = str(uuid.uuid4())
            st.session_state.scrape_results      = None
            st.session_state.logs                = []
            st.session_state.start_webhook_sent  = False
            _add_log("[GEMA] Extraction confirmed. Starting pipeline...")
            st.rerun()
        except Exception as exc:
            st.error(f"Invalid configuration: {exc}")

# =============================================================================
# Phase 3: Live Execution & Log Console
# =============================================================================

if st.session_state.is_running:
    st.divider()
    st.subheader("Phase 3 — Pipeline Running")

    log_placeholder = st.empty()
    progress_placeholder = st.empty()

    log_queue: queue.Queue = queue.Queue()
    result_holder: dict = {}

    def _scraper_thread():
        jobs, summary = run_scrape_session(
            st.session_state.search_config,
            db,
            log_queue,
            ttl_hours,
        )
        result_holder["jobs"]    = jobs
        result_holder["summary"] = summary

    # Stage 1 — notify Discord that the run has started (fires exactly once per run)
    if not st.session_state.get("start_webhook_sent", False):
        send_discord_alert(random.choice(START_PHRASES))
        st.session_state["start_webhook_sent"] = True

    thread = threading.Thread(target=_scraper_thread, daemon=True)
    thread.start()

    # ── Non-blocking live log loop (Fix 3) ───────────────────────────────────
    # WHY NO thread.join() HERE:
    #   thread.join() blocks the main Streamlit thread completely. While
    #   blocked, Streamlit's websocket layer cannot send delta updates to
    #   the browser — the UI is frozen until the entire scrape finishes.
    #   The user sees a spinner but receives no log lines until the end.
    #
    # CORRECT PATTERN:
    #   while thread.is_alive() checks thread state without blocking.
    #   log_queue.get_nowait() is non-blocking — raises queue.Empty instantly
    #   if no message is ready, rather than waiting up to 0.3s per call.
    #   time.sleep(0.5) yields control back to Streamlit's event loop every
    #   500ms, allowing it to flush pending websocket frames to the browser.
    #   This produces genuine real-time log streaming to the UI.
    #
    #   The final drain loop after is_alive() == False ensures no messages
    #   written in the thread's last moments are missed.

    while thread.is_alive():
        drained = False
        while True:
            try:
                msg = log_queue.get_nowait()
                _add_log(msg)
                drained = True
            except queue.Empty:
                break
        if drained:
            log_placeholder.code("\n".join(st.session_state.logs[-40:]), language="")
        time.sleep(0.5)

    # Final drain — capture any messages written between last check and thread exit
    while True:
        try:
            msg = log_queue.get_nowait()
            _add_log(msg)
        except queue.Empty:
            break
    log_placeholder.code("\n".join(st.session_state.logs[-40:]), language="")

    # Scraper complete — run matcher
    raw_jobs = result_holder.get("jobs", [])
    summary  = result_holder.get("summary", ScrapeRunSummary())

    _add_log(f"[MATCHER] Scoring {len(raw_jobs)} new jobs...")
    tier1, tier2, tier3, tier4 = bucket_jobs(raw_jobs, st.session_state.search_config, db)

    summary.tier1_count = len(tier1)
    summary.tier2_count = len(tier2)
    summary.tier3_count = len(tier3)

    st.session_state.scrape_results = (tier1, tier2, tier3, tier4)
    st.session_state.summary = summary
    st.session_state.is_running = False

    _add_log(
        f"[GEMA] Pipeline complete. "
        f"T1={len(tier1)} | T2={len(tier2)} | T3={len(tier3)}"
    )

    # Stage 2 — send extraction report to Discord
    _discord_report = (
        f"📊 Extraction Report:\n"
        f"- Tier 1: {len(tier1)}\n"
        f"- Tier 2: {len(tier2)}\n"
        f"- Tier 3: {len(tier3)}\n"
        f"- Tier 4: {len(tier4)}"
    )
    send_discord_alert(_discord_report)

    # Stage 3 — send shutdown phrase to Discord
    send_discord_alert(random.choice(END_PHRASES))

    # Push to integrations
    all_results = tier1 + tier2 + tier3 + tier4
    if all_results and notion.is_enabled:
        _add_log("[NOTION] Pushing results (incl. Tier 4 manual review)...")
        notion.push_batch(all_results)

    if all_results and sheets.is_enabled:
        _add_log("[SHEETS] Appending to data warehouse...")
        sheets.append_batch(all_results, summary)

    st.rerun()

# =============================================================================
# Phase 4: Results Display
# =============================================================================

if st.session_state.scrape_results:
    tier1, tier2, tier3, tier4 = st.session_state.scrape_results
    summary = st.session_state.summary

    st.divider()
    st.subheader("Phase 4 — Results")

    # Summary metrics
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("🟢 Tier 1", len(tier1), help="80–100% match — apply immediately")
    c2.metric("🟡 Tier 2", len(tier2), help="50–79% match — review carefully")
    c3.metric("🔴 Tier 3", len(tier3), help="<50% match — recycle bin")
    c4.metric("🟣 Tier 4", len(tier4), help="Text salary — manual review required")
    if summary and summary.duration_seconds:
        c5.metric("⏱ Duration", f"{summary.duration_seconds:.0f}s")

    def _render_tier(tiered_jobs, label, color_emoji, show_score=True):
        if not tiered_jobs:
            return
        with st.expander(f"{color_emoji} {label} ({len(tiered_jobs)} jobs)", expanded=True):
            for tj in tiered_jobs:
                with st.container():
                    cols = st.columns([3, 2, 1, 1])
                    cols[0].markdown(f"**[{tj.job.title}]({tj.job.url})**")
                    cols[1].write(tj.job.company)
                    cols[2].write(tj.job.salary_raw or "—")
                    if show_score and tj.match_score >= 0:
                        cols[3].progress(
                            tj.match_score / 100,
                            text=f"{tj.match_score}%",
                        )
                    else:
                        cols[3].caption("Manual")
                    if tj.match_reasons or tj.miss_reasons:
                        with st.expander("Details", expanded=False):
                            if tj.match_reasons:
                                st.success("✓ " + " | ".join(tj.match_reasons))
                            if tj.miss_reasons:
                                st.warning("✗ " + " | ".join(tj.miss_reasons))
                    st.divider()

    _render_tier(tier1, "Tier 1 — High Priority",          "🟢")
    _render_tier(tier2, "Tier 2 — Explore",                "🟡")
    _render_tier(tier3, "Tier 3 — Recycle Bin",            "🔴")
    _render_tier(tier4, "Tier 4 — Manual Review (Salary)", "🟣", show_score=False)

# =============================================================================
# Live Log Console (always visible at bottom)
# =============================================================================

if st.session_state.logs:
    st.divider()
    st.subheader("Live Engine Log")
    st.code("\n".join(st.session_state.logs[-60:]), language="")
