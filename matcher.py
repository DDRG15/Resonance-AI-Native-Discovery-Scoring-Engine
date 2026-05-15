"""
matcher.py — Match Scoring & Tier Bucketing Engine for Project GEMA.

Scores each JobResult against the SearchConfig and assigns Tier 1/2/3/4.
No external API calls — pure Python logic, runs in milliseconds.

Scoring weights (mathematical path — Tier 1/2/3 only):
    Title match (fuzzy)  : 50 points max
    Salary meets minimum : 25 points
    Must-include present : 15 points max
    Must-exclude absent  : 10 points (penalty applied if found)

Tier 4 (Manual Review) BYPASSES mathematical scoring entirely.
    Triggered when: salary_raw is present but parse_salary_usd() returns None.
    Example inputs: 'Competitive', 'Competitive + equity + 401k', 'DOE'
    These are legitimate salary disclosures in text form — penalizing them
    with a 0-point salary score would bury potentially excellent jobs in Tier 3.
"""

import logging
import re
from typing import Optional

import config
from models import JobResult, SearchConfig, TieredJob

logger = logging.getLogger(__name__)


# =============================================================================
# Scoring Components
# =============================================================================

def _score_title(job_title: str, target_titles: list[str]) -> tuple[int, list[str], list[str]]:
    """
    Fuzzy title match. Returns (score, match_reasons, miss_reasons).
    Max 50 points.

    Strategy:
        Exact match (case-insensitive)  → 50 pts
        All words present               → 35 pts
        Any word present                → 15 pts
        No match                        → 0 pts
    """
    title_lower = job_title.lower()
    best_score = 0
    match_reasons = []
    miss_reasons = []

    for target in target_titles:
        target_lower = target.lower()
        target_words = set(re.split(r"\W+", target_lower)) - {""}

        if target_lower == title_lower:
            best_score = max(best_score, 50)
            match_reasons.append(f"Exact title match: '{target}'")
        elif target_lower in title_lower:
            best_score = max(best_score, 40)
            match_reasons.append(f"Title contains: '{target}'")
        elif all(w in title_lower for w in target_words):
            best_score = max(best_score, 35)
            match_reasons.append(f"All words of '{target}' in title")
        elif any(w in title_lower for w in target_words if len(w) > 2):
            best_score = max(best_score, 15)
            match_reasons.append(f"Partial title match: '{target}'")

    if best_score == 0:
        miss_reasons.append(f"Title '{job_title}' did not match any target")

    return min(best_score, 50), match_reasons, miss_reasons


def _score_salary(
    job: JobResult,
    min_salary: Optional[int],
) -> tuple[int, list[str], list[str]]:
    """
    Salary gate. Returns (score, match_reasons, miss_reasons).
    Max 25 points.

    If salary is not published → 12 pts (Tier 2 weight, not penalized fully).
    Vol 1.3: 'no salary published' → requires secondary review.
    """
    if min_salary is None:
        return 25, ["No salary filter specified"], []

    parsed = job.parse_salary_usd()

    if parsed is None:
        return 12, [], ["Salary not published — manual review required"]

    if parsed >= min_salary:
        return 25, [f"Salary ${parsed:,} meets minimum ${min_salary:,}"], []

    return 0, [], [f"Salary ${parsed:,} below minimum ${min_salary:,}"]


def _score_must_include(
    job: JobResult,
    must_include: list[str],
) -> tuple[int, list[str], list[str]]:
    """
    Must-include keyword check across title + salary_raw + company.
    Max 15 points (proportional if multiple keywords).
    """
    if not must_include:
        return 15, ["No must-include keywords specified"], []

    searchable = " ".join(filter(None, [
        job.title, job.company, job.salary_raw or "", job.source_domain
    ])).lower()

    matched = [kw for kw in must_include if kw.lower() in searchable]
    missing = [kw for kw in must_include if kw.lower() not in searchable]

    score = int(15 * len(matched) / len(must_include))
    match_reasons = [f"Keyword present: '{kw}'" for kw in matched]
    miss_reasons = [f"Required keyword missing: '{kw}'" for kw in missing]

    return score, match_reasons, miss_reasons


def _score_skill_overlap(
    job: JobResult,
    profile: Optional[dict],
) -> tuple[int, list[str], list[str]]:
    """
    Profile skill-overlap bonus. Max 15 points (additive on top of the base 100).

    Searches job title + company + salary_raw for any string in the union of
    profile['core_skills'] and profile['audit_signals']. Score is proportional
    to the fraction of signals that hit, capped at 15.

    Returns (0, [...], []) when profile is None or contains no signals — never
    raises, never penalises a job for a missing profile.
    """
    if not profile:
        return 0, ["No profile loaded — skill overlap skipped"], []

    core_skills   = profile.get("core_skills",   [])
    audit_signals = profile.get("audit_signals", [])
    all_signals   = core_skills + audit_signals

    if not all_signals:
        return 0, ["Profile has no skills or signals — overlap skipped"], []

    searchable = " ".join(filter(None, [
        job.title, job.company, job.salary_raw or ""
    ])).lower()

    hits   = [s for s in all_signals if s.lower() in searchable]
    misses = [s for s in all_signals if s.lower() not in searchable]

    score = min(15, int(15 * len(hits) / len(all_signals)))
    match_reasons = [f"Skill/signal match: '{s}'" for s in hits]
    miss_reasons  = [f"Skill/signal absent: '{s}'" for s in misses[:3]]  # cap noise

    return score, match_reasons, miss_reasons


def _apply_exclusion_penalty(
    job: JobResult,
    must_exclude: list[str],
) -> tuple[int, list[str], list[str]]:
    """
    Must-exclude check. Returns penalty (negative) if any exclusion hit.
    A single hit is a 10-point deduction (from the 10-pt inclusion pool).
    Multiple hits don't stack — one hit is enough to flag.
    """
    if not must_exclude:
        return 0, ["No exclusion keywords specified"], []

    searchable = " ".join(filter(None, [
        job.title, job.company, job.salary_raw or ""
    ])).lower()

    hits = [kw for kw in must_exclude if kw.lower() in searchable]

    if hits:
        return -10, [], [f"Exclusion keyword found: '{kw}'" for kw in hits]

    return 10, [f"No exclusion keywords detected (clean)"], []


# =============================================================================
# Tier Assignment
# =============================================================================

def _assign_tier(score: int) -> str:
    """Maps a 0–100 score to a Tier label per Vol 1.3 spec."""
    if score >= config.TIER1_MIN_SCORE:
        return "Tier 1"
    if score >= config.TIER2_MIN_SCORE:
        return "Tier 2"
    return "Tier 3"


# =============================================================================
# Public Interface
# =============================================================================

def score_job(job: JobResult, search_config: SearchConfig, profile: Optional[dict] = None) -> TieredJob:
    """
    Scores a single JobResult and returns a TieredJob with full audit trail.

    TIER 4 BYPASS:
        Condition:  job.salary_raw is a non-empty string
                    AND job.parse_salary_usd() returns None

        This means the employer disclosed SOMETHING about compensation,
        but it is in prose form ('Competitive', 'DOE', 'Based on experience').
        These are NOT missing salary data — they are human-readable salary
        disclosures that the regex cannot parse as a number.

        Explicit guard — three conditions must ALL be true to trigger bypass:
            1. salary_raw is not None
            2. salary_raw is not an empty string (stripped)
            3. parse_salary_usd() returns None for this non-empty string

        This prevents the edge case where salary_raw=" " (whitespace only)
        from incorrectly triggering Tier 4.

    Score breakdown for Tier 1/2/3 (max 100):
        Title match    : 0–50
        Salary gate    : 0–25
        Must-include   : 0–15
        Exclusion bonus: -10 to +10
    """
    # ── Tier 4 bypass: non-empty text salary that is not a parseable number ──
    salary_is_text = (
        job.salary_raw is not None
        and job.salary_raw.strip() != ""
        and job.parse_salary_usd() is None
    )
    if salary_is_text:
        logger.debug(
            "[T4] Manual Review: '%s' @ %s — salary_raw=%r is non-numeric",
            job.title, job.company, job.salary_raw,
        )
        return TieredJob(
            job=job,
            match_score=-1,
            tier="Tier 4",
            match_reasons=["Salary disclosed as text — bypassed mathematical scoring"],
            miss_reasons=[
                f"Raw salary text: '{job.salary_raw}' — requires manual evaluation"
            ],
        )

    # ── Mathematical scoring path (Tier 1 / 2 / 3) ───────────────────────────
    all_match: list[str] = []
    all_miss:  list[str] = []

    title_score,   tm, tn  = _score_title(job.title, search_config.target_titles)
    salary_score,  sm, sn  = _score_salary(job, search_config.min_salary)
    include_score, im, in_ = _score_must_include(job, search_config.must_include)
    excl_delta,    em, en  = _apply_exclusion_penalty(job, search_config.must_exclude)
    skill_score,   km, kn  = _score_skill_overlap(job, profile)

    all_match.extend(tm + sm + im + em + km)
    all_miss.extend(tn + sn + in_ + en + kn)

    raw_score   = title_score + salary_score + include_score + excl_delta + skill_score
    # Cap at 115: the 15-pt skill bonus can push a strong Tier 2 into Tier 1
    # without disturbing the existing Tier 1 threshold (80 pts).
    final_score = max(0, min(115, raw_score))
    tier        = _assign_tier(final_score)

    logger.debug(
        "[SCORE] '%s' @ %s: %d → %s (T=%d S=%d I=%d X=%d)",
        job.title, job.company, final_score, tier,
        title_score, salary_score, include_score, excl_delta,
    )

    return TieredJob(
        job=job,
        match_score=final_score,
        tier=tier,
        match_reasons=all_match,
        miss_reasons=all_miss,
    )


def score_job_inline(job: JobResult, search_config: SearchConfig) -> str:
    """
    Lightweight tier classification for use inside the scraper's hot path.

    Called during card extraction BEFORE the full bucket_jobs pass to enable
    real-time Tier 1 webhook notifications. Must be fast — no logging overhead
    beyond DEBUG level, no DB writes, returns only the tier string.

    DESIGN: Calls score_job() directly — same logic, no duplication.
    The overhead is microseconds per card. At 100 cards per domain × 4 domains
    = 400 calls. Total overhead: < 5ms. Acceptable.

    Returns: 'Tier 1', 'Tier 2', 'Tier 3', or 'Tier 4'
    """
    return score_job(job, search_config).tier


def bucket_jobs(
    jobs: list[JobResult],
    search_config: SearchConfig,
    db=None,
    profile: Optional[dict] = None,
) -> tuple[list[TieredJob], list[TieredJob], list[TieredJob], list[TieredJob]]:
    """
    Scores and buckets all jobs into four tiers. Returns (t1, t2, t3, t4).

    Tier 4 contains jobs where salary_raw is a non-empty, non-numeric string.
    They bypass mathematical scoring and must be reviewed manually via Notion.

    If db is provided, backfills tier + match_score in the seen_registry
    for the Streamlit dashboard. Tier 4 jobs are stored with match_score=-1.

    Note: score_job_inline() is the lightweight fast-path used during live
    scraping for real-time webhook triggers. bucket_jobs() is the post-scrape
    full pass that populates all display data.
    """
    tier1, tier2, tier3, tier4 = [], [], [], []

    for job in jobs:
        tiered = score_job(job, search_config, profile=profile)
        if tiered.tier == "Tier 1":
            tier1.append(tiered)
        elif tiered.tier == "Tier 2":
            tier2.append(tiered)
        elif tiered.tier == "Tier 3":
            tier3.append(tiered)
        else:
            tier4.append(tiered)

        if db:
            try:
                db.update_tier(job.url, tiered.tier, tiered.match_score)
            except Exception as exc:
                logger.warning("[DB] Could not update tier for %s: %s", job.url, exc)

    logger.info(
        "[BUCKET] T1=%d | T2=%d | T3=%d | T4(manual)=%d",
        len(tier1), len(tier2), len(tier3), len(tier4),
    )
    return tier1, tier2, tier3, tier4
