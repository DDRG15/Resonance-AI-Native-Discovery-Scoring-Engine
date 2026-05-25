"""
run_scrape_now.py — One-shot CLI scrape runner for GEMA.

Runs a full scrape session using the current user_profile.yaml and all
registered boards, then sends a Discord summary. No Streamlit required.

Usage:
    python run_scrape_now.py

Output:
    - Results stored in gema_registry.db (same DB as the UI)
    - Discord notification with Tier 1/2/3 counts + top Tier 1 jobs
    - Console log while running
"""

import logging
import queue
import sys
import yaml
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_scrape_now")


def _load_profile() -> dict:
    path = Path(__file__).parent / "user_profile.yaml"
    if not path.exists():
        logger.warning("user_profile.yaml not found — scoring will use generic defaults.")
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _drain_log_queue(log_q: queue.Queue) -> None:
    while not log_q.empty():
        try:
            msg = log_q.get_nowait()
            logger.info("[SCRAPER] %s", msg)
        except queue.Empty:
            break


def main() -> None:
    from database import GemaDatabase
    from matcher import bucket_jobs
    from models import SearchConfig
    from scraper import run_scrape_session
    from integrations.webhook_client import send_discord_alert
    import selectors_registry

    # ── Profile ──────────────────────────────────────────────────────────────
    profile = _load_profile()
    role    = profile.get("role", "Python Backend Developer")
    logger.info("Profile loaded: %s | %d skills | %d audit signals",
                role,
                len(profile.get("core_skills", [])),
                len(profile.get("audit_signals", [])))

    # ── Search config ─────────────────────────────────────────────────────────
    # No minimum salary — all remote offers accepted.
    # Titles cover backend, SRE, DevOps, and data engineering tracks.
    cfg = SearchConfig(
        target_titles=[
            "Python Backend Developer",
            "Backend Engineer",
            "Python Developer",
            "SRE",
            "Site Reliability Engineer",
            "DevOps Engineer",
            "Data Engineer",
            "Software Engineer Python",
        ],
        must_include=[],
        must_exclude=[],
        min_salary=None,
        target_domains=list(selectors_registry.SELECTORS.keys()),
    )
    logger.info("Search config: %d titles × %d boards",
                len(cfg.target_titles), len(cfg.target_domains))

    # ── Database ──────────────────────────────────────────────────────────────
    db = GemaDatabase()

    # ── Log queue ─────────────────────────────────────────────────────────────
    log_q: queue.Queue = queue.Queue()

    # ── Notify start ──────────────────────────────────────────────────────────
    send_discord_alert(
        f"GEMA scrape iniciado — {len(cfg.target_titles)} títulos × "
        f"{len(cfg.target_domains)} boards. Sin filtro de salario. "
        "Los resultados llegan en unos minutos."
    )

    # ── Run ───────────────────────────────────────────────────────────────────
    logger.info("Launching scrape session...")
    try:
        jobs, summary = run_scrape_session(cfg, db, log_q, ttl_hours=48, profile=profile)
    except Exception as exc:
        logger.error("Scrape session failed: %s", exc)
        send_discord_alert(f"GEMA scrape FAILED: {exc}")
        sys.exit(1)
    finally:
        _drain_log_queue(log_q)

    _drain_log_queue(log_q)
    logger.info("Scrape complete. Raw jobs returned: %d", len(jobs))

    # ── Bucket ────────────────────────────────────────────────────────────────
    t1, t2, t3, t4 = bucket_jobs(jobs, cfg, db, profile=profile)
    logger.info("Tiers — T1: %d  T2: %d  T3: %d  T4: %d", len(t1), len(t2), len(t3), len(t4))

    # ── Build Discord summary ─────────────────────────────────────────────────
    lines = [
        "**GEMA Scrape Complete**",
        f"Total new: **{len(jobs)}** jobs across {len(cfg.target_domains)} boards",
        f"Tier 1 (aplicar ya):  **{len(t1)}**",
        f"Tier 2 (revisar):     **{len(t2)}**",
        f"Tier 3 (recycle bin): **{len(t3)}**",
        f"Tier 4 (manual):      **{len(t4)}**",
        "",
    ]

    if t1:
        lines.append("**Tier 1 — Top matches:**")
        for tj in t1[:8]:  # cap at 8 so message stays under Discord limit
            salary = f" | {tj.job.salary_raw}" if tj.job.salary_raw else ""
            lines.append(
                f"• **{tj.job.title}** @ {tj.job.company} "
                f"(score {tj.match_score}){salary}"
            )
            lines.append(f"  {tj.job.url}")
    elif t2:
        lines.append("**No Tier 1 this run. Top Tier 2:**")
        for tj in t2[:5]:
            lines.append(f"• {tj.job.title} @ {tj.job.company} (score {tj.match_score})")
            lines.append(f"  {tj.job.url}")
    else:
        lines.append("No new matches this run. Boards may be returning cached results.")

    message = "\n".join(lines)

    # Discord has a 2000-char limit per message — trim if needed
    if len(message) > 1900:
        message = message[:1900] + "\n…(truncated — open GEMA UI for full list)"

    send_discord_alert(message)
    logger.info("Discord notification sent.")

    # ── Console summary ───────────────────────────────────────────────────────
    print("\n" + "="*60)
    print(f"  DONE — {len(jobs)} new jobs | T1:{len(t1)} T2:{len(t2)} T3:{len(t3)} T4:{len(t4)}")
    print("="*60 + "\n")


if __name__ == "__main__":
    main()
