"""
nlp_engine.py — NLP Engine: natural language → validated SearchConfig.

SRE: Groq primary + Gemini fallback, exponential backoff, Pydantic validation,
     self-correcting retry loop that injects the error back into the prompt.
"""

import json
import logging
import os
import re
import requests
from typing import Optional

from pydantic import ValidationError
from tenacity import (
    retry, retry_if_exception, retry_if_exception_type,
    stop_after_attempt, wait_exponential,
    before_sleep_log,
)

import config
from models import SearchConfig, ExtractionResult, ExtractedJob

logger = logging.getLogger(__name__)


# =============================================================================
# Input Sanitization — Token Optimization
# =============================================================================

def sanitize_input(raw_text: str) -> str:
    """Strips HTML from Playwright output before injecting into the LLM prompt."""
    before = len(raw_text)
    text = re.sub(r'<script.*?>.*?</script>', '', raw_text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<style.*?>.*?</style>', '', text, flags=re.IGNORECASE | re.DOTALL)
    text = re.sub(r'<.*?>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    after = len(text)
    logger.debug(
        "[SANITIZE] %d → %d chars (saved %d, %.1f%%)",
        before, after, before - after, (before - after) / max(before, 1) * 100,
    )
    return text


def _extract_json_from_text(text: str) -> dict:
    """Extracts the outermost JSON object using a brace-depth counter.

    Immune to multiple JSON objects in one response — returns the first
    complete one (where depth returns to 0) rather than spanning all of them.
    """
    start = -1
    depth = 0
    for i, ch in enumerate(text):
        if ch == '{':
            if depth == 0:
                start = i
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0 and start != -1:
                return json.loads(text[start:i + 1])
    raise ValueError("No valid JSON object found in response")

# =============================================================================
# System Prompt — The LLM Contract
# =============================================================================

_SYSTEM_PROMPT = """You are a structured data extraction engine for a job search system.
Your ONLY job is to convert a natural language job search request into a JSON object.

OUTPUT RULES — CRITICAL:
1. Return ONLY a valid JSON object. No explanations, no markdown, no code blocks.
2. Never add keys not in the schema. Never omit required keys.

JSON SCHEMA:
{
  "target_titles": ["string"],     REQUIRED. At least 1 job title.
  "must_include": ["string"],      Default: []. Keywords that must appear.
  "must_exclude": ["string"],      Default: []. Keywords that disqualify a job.
  "min_salary": integer or null,   Default: null. Annual USD minimum.
  "target_domains": ["string"]     Default: ["himalayas.app","trueup.io"].
}

TRANSLATION RULES:
- "remote" → must_include: ["Remote"]
- "no contract" → must_exclude: ["Contract"]
- "no hybrid" → must_exclude: ["Hybrid"]
- "no on-site" → must_exclude: ["On-site"]
- "$90k" / "90000" / "90K" → min_salary: 90000
- If no domains mentioned → target_domains: ["himalayas.app","trueup.io"]"""

_RETRY_INJECT = """Your previous response failed validation:
ERROR: {error}
YOUR RESPONSE WAS: {raw}

Return ONLY the corrected JSON object. Nothing else."""


# =============================================================================
# Auditor Prompt — Zero-Cost AI Consensus Model
# =============================================================================

_AUDITOR_PROMPT = """You are a senior job search analyst auditing an AI-generated search configuration.

You will receive:
1. ORIGINAL REQUEST — the user's natural language job search description.
2. GENERATED CONFIG — a JSON object that an AI extracted from that request.

YOUR TASK:
Compare the two carefully. In exactly 1-2 sentences, state whether the JSON
perfectly captures the user's intent. Point out any of the following issues:

    - Missed keywords (skills, tools, seniority levels the user mentioned)
    - Hallucinated assumptions (things added that the user did NOT say)
    - Incorrect salary parsing (wrong number, wrong currency)
    - Missing exclusions (the user said 'no X' but X is not in must_exclude)
    - Missing or wrong target_domains

If the JSON is accurate and complete, say: "Config looks accurate. No issues detected."
If there are issues, be specific: name the exact field and what is wrong or missing.

ORIGINAL REQUEST:
{user_prompt}

GENERATED CONFIG:
{config_json}

Your 1-2 sentence audit:"""


# =============================================================================
# Rate-limit circuit breaker — session-level flags
# =============================================================================

class _ProviderRateLimited(Exception):
    """Raised when a provider returns HTTP 429. Signals permanent skip for this session."""


# These flip to True the moment a provider returns 429 — never reset within a run.
_groq_rate_limited        = False
_gemini_rate_limited      = False
_openrouter_rate_limited  = False
_cohere_rate_limited      = False


def _is_rate_limit(exc: Exception) -> bool:
    """Detect HTTP 429 / quota exhausted across Groq and Gemini SDK error shapes."""
    s = str(exc).lower()
    t = type(exc).__name__
    return (
        "429"               in s
        or "rate limit"     in s
        or "ratelimit"      in t.lower()
        or "resourceexhausted" in t.lower()
        or "quota"          in s
        or "too many requests" in s
    )


# =============================================================================
# Backoff decorator — retries on transient errors, skips on rate limits
# =============================================================================

_backoff = retry(
    retry=retry_if_exception(lambda e: not isinstance(e, _ProviderRateLimited)),
    stop=stop_after_attempt(config.LLM_MAX_RETRIES),
    wait=wait_exponential(min=config.LLM_RETRY_WAIT_MIN, max=config.LLM_RETRY_WAIT_MAX),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)


# =============================================================================
# LLM Callers
# =============================================================================

@_backoff
def _call_groq(messages: list[dict]) -> str:
    global _groq_rate_limited
    if _groq_rate_limited:
        raise _ProviderRateLimited("Groq rate-limited this session — skipping")
    from groq import Groq
    try:
        client = Groq(api_key=config.GROQ_API_KEY)
        resp = client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=messages,
            temperature=0.0,
            max_tokens=512,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        if _is_rate_limit(e):
            _groq_rate_limited = True
            print("  [RATE LIMIT] Groq hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited("Groq 429") from e
        raise


@_backoff
def _call_gemini(messages: list[dict]) -> str:
    global _gemini_rate_limited
    if _gemini_rate_limited:
        raise _ProviderRateLimited("Gemini rate-limited this session — skipping")
    import google.generativeai as genai
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(
            model_name=config.GEMINI_MODEL,
            system_instruction=_SYSTEM_PROMPT,
        )
        user_text = "\n".join(m["content"] for m in messages if m["role"] == "user")
        resp = model.generate_content(
            user_text,
            generation_config=genai.GenerationConfig(
                temperature=0.0,
                response_mime_type="application/json",
            ),
        )
        return resp.text.strip()
    except Exception as e:
        if _is_rate_limit(e):
            _gemini_rate_limited = True
            print("  [RATE LIMIT] Gemini hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited("Gemini 429") from e
        raise


# Plain-text callers for the auditor (no JSON mode — free-form 1-2 sentence response)

@_backoff
def _call_groq_plain(prompt: str) -> str:
    global _groq_rate_limited
    if _groq_rate_limited:
        raise _ProviderRateLimited("Groq rate-limited this session — skipping")
    from groq import Groq
    try:
        client = Groq(api_key=config.GROQ_API_KEY)
        resp = client.chat.completions.create(
            model=config.GROQ_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=128,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        if _is_rate_limit(e):
            _groq_rate_limited = True
            print("  [RATE LIMIT] Groq hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited("Groq 429") from e
        raise


@_backoff
def _call_gemini_plain(prompt: str) -> str:
    global _gemini_rate_limited
    if _gemini_rate_limited:
        raise _ProviderRateLimited("Gemini rate-limited this session — skipping")
    import google.generativeai as genai
    try:
        genai.configure(api_key=config.GEMINI_API_KEY)
        model = genai.GenerativeModel(model_name=config.GEMINI_MODEL)
        resp = model.generate_content(
            prompt,
            generation_config=genai.GenerationConfig(temperature=0.2, max_output_tokens=128),
        )
        return resp.text.strip()
    except Exception as e:
        if _is_rate_limit(e):
            _gemini_rate_limited = True
            print("  [RATE LIMIT] Gemini hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited("Gemini 429") from e
        raise


# =============================================================================
# OpenRouter & Cohere Fallback Callers
# =============================================================================

@_backoff
def _call_openrouter(messages: list[dict]) -> str:
    global _openrouter_rate_limited
    if _openrouter_rate_limited:
        raise _ProviderRateLimited("OpenRouter rate-limited this session — skipping")
    if not config.OPENROUTER_API_KEY:
        raise _ProviderRateLimited("OpenRouter API key not configured")
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.OPENROUTER_MODEL,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": 1024,
            },
            timeout=30,
        )
        if resp.status_code == 429:
            _openrouter_rate_limited = True
            print("  [RATE LIMIT] OpenRouter hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited("OpenRouter 429")
        resp.raise_for_status()
        text = resp.json()["choices"][0]["message"]["content"].strip()
        return json.dumps(_extract_json_from_text(text))
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _openrouter_rate_limited = True
            print("  [RATE LIMIT] OpenRouter rate limit — disabled for this session, falling back.")
            raise _ProviderRateLimited("OpenRouter rate limit") from e
        raise


@_backoff
def _call_cohere(messages: list[dict]) -> str:
    global _cohere_rate_limited
    if _cohere_rate_limited:
        raise _ProviderRateLimited("Cohere rate-limited this session — skipping")
    if not config.COHERE_API_KEY:
        raise _ProviderRateLimited("Cohere API key not configured")
    try:
        system_msg = next((m["content"] for m in messages if m["role"] == "system"), None)
        turns = [m for m in messages if m["role"] != "system"]
        chat_history = [
            {"role": "USER" if m["role"] == "user" else "CHATBOT", "message": m["content"]}
            for m in turns[:-1]
        ]
        last_user_msg = turns[-1]["content"] if turns else ""
        payload: dict = {
            "model": config.COHERE_MODEL,
            "message": last_user_msg,
            "temperature": 0.0,
            "max_tokens": 1024,
        }
        if system_msg:
            payload["preamble"] = system_msg
        if chat_history:
            payload["chat_history"] = chat_history
        resp = requests.post(
            "https://api.cohere.ai/v1/chat",
            headers={
                "Authorization": f"Bearer {os.getenv('COHERE_API_KEY')}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if resp.status_code == 429:
            _cohere_rate_limited = True
            print("  [RATE LIMIT] Cohere hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited("Cohere 429")
        resp.raise_for_status()
        text = resp.json()["text"].strip()
        return json.dumps(_extract_json_from_text(text))
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _cohere_rate_limited = True
            print("  [RATE LIMIT] Cohere rate limit — disabled for this session, falling back.")
            raise _ProviderRateLimited("Cohere rate limit") from e
        raise


# Plain-text callers for the auditor (OpenRouter & Cohere — no JSON mode)

@_backoff
def _call_openrouter_plain(prompt: str) -> str:
    global _openrouter_rate_limited
    if _openrouter_rate_limited:
        raise _ProviderRateLimited("OpenRouter rate-limited this session — skipping")
    if not config.OPENROUTER_API_KEY:
        raise _ProviderRateLimited("OpenRouter API key not configured")
    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {os.getenv('OPENROUTER_API_KEY')}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.OPENROUTER_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 128,
            },
            timeout=30,
        )
        if resp.status_code == 429:
            _openrouter_rate_limited = True
            print("  [RATE LIMIT] OpenRouter hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited("OpenRouter 429")
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _openrouter_rate_limited = True
            print("  [RATE LIMIT] OpenRouter rate limit — disabled for this session, falling back.")
            raise _ProviderRateLimited("OpenRouter rate limit") from e
        raise


@_backoff
def _call_cohere_plain(prompt: str) -> str:
    global _cohere_rate_limited
    if _cohere_rate_limited:
        raise _ProviderRateLimited("Cohere rate-limited this session — skipping")
    if not config.COHERE_API_KEY:
        raise _ProviderRateLimited("Cohere API key not configured")
    try:
        resp = requests.post(
            "https://api.cohere.ai/v1/chat",
            headers={
                "Authorization": f"Bearer {os.getenv('COHERE_API_KEY')}",
                "Content-Type": "application/json",
            },
            json={
                "model": config.COHERE_MODEL,
                "message": prompt,
                "temperature": 0.2,
                "max_tokens": 128,
            },
            timeout=30,
        )
        if resp.status_code == 429:
            _cohere_rate_limited = True
            print("  [RATE LIMIT] Cohere hit 429 — disabled for this session, falling back.")
            raise _ProviderRateLimited("Cohere 429")
        resp.raise_for_status()
        return resp.json()["text"].strip()
    except _ProviderRateLimited:
        raise
    except Exception as e:
        if _is_rate_limit(e):
            _cohere_rate_limited = True
            print("  [RATE LIMIT] Cohere rate limit — disabled for this session, falling back.")
            raise _ProviderRateLimited("Cohere rate limit") from e
        raise


# =============================================================================
# Validation
# =============================================================================

def _parse_and_validate(raw: str) -> SearchConfig:
    """
    Strips markdown fences if present, parses JSON, validates with Pydantic.
    Vol 1.4 Risk 3.1 mitigation.
    """
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = "\n".join(
            l for l in cleaned.split("\n") if not l.strip().startswith("```")
        ).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Not valid JSON: {exc}") from exc
    return SearchConfig(**data)


# =============================================================================
# Public Interface
# =============================================================================

def parse_prompt_to_config(
    user_prompt: str,
    log_callback=None,
) -> SearchConfig:
    """
    Converts a natural language prompt into a validated SearchConfig.

    Strategy:
        1. Try primary LLM (configured via PRIMARY_LLM env var)
        2. On failure, try fallback LLM
        3. On validation failure, inject error into next prompt for self-correction
        4. Raise RuntimeError if all retries across all LLMs fail
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    callers = _build_caller_chain()
    last_error: Optional[Exception] = None

    for llm_name, caller in callers:
        _log(f"[NLP] Contacting {llm_name}...")
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": user_prompt},
        ]
        raw_response = ""

        for attempt in range(1, config.LLM_MAX_RETRIES + 1):
            try:
                raw_response = caller(messages)
                _log(f"[NLP] Response received. Validating schema (attempt {attempt})...")
                result = _parse_and_validate(raw_response)
                _log("[NLP] SearchConfig validated successfully.")
                return result

            except (ValueError, ValidationError) as schema_exc:
                # ── Schema / JSON error — the LLM produced malformed output.
                # Inject the error back into the conversation so the model can
                # self-correct on the next attempt. This is appropriate because
                # the LLM is capable of fixing its own output format mistakes.
                last_error = schema_exc
                _log(
                    f"[NLP] Schema validation failed (attempt {attempt}/{config.LLM_MAX_RETRIES})"
                    f" on {llm_name}: {schema_exc}"
                )
                if attempt < config.LLM_MAX_RETRIES:
                    messages += [
                        {"role": "assistant", "content": raw_response},
                        {"role": "user", "content": _RETRY_INJECT.format(
                            error=str(schema_exc), raw=raw_response
                        )},
                    ]
                # Continue the for-loop to retry this LLM with corrective context

            except Exception as network_exc:
                # ── Network / API error — Groq/Gemini is down, rate-limited,
                # or returned a non-retryable HTTP error (503, 401, etc.).
                # Injecting this into the LLM conversation is WRONG: the model
                # would hallucinate a JSON fix for a problem it did not cause.
                # Instead: log, record the error, and BREAK immediately to
                # fall through to the next LLM provider with a CLEAN history.
                last_error = network_exc
                _log(
                    f"[NLP] Network/API error on {llm_name} (attempt {attempt}): "
                    f"{type(network_exc).__name__}: {network_exc}. "
                    f"Falling back to next provider."
                )
                break  # Exit retry loop for this LLM — try next provider

    raise RuntimeError(f"All LLM attempts exhausted. Last error: {last_error}")


def _build_caller_chain() -> list[tuple[str, callable]]:
    """Primary LLM first, fallback second, then OpenRouter and Cohere as final fallbacks."""
    pool = {
        "groq":   ("Groq (llama3-70b)", _call_groq),
        "gemini": ("Gemini (flash)",     _call_gemini),
    }
    available = {
        "groq":   bool(config.GROQ_API_KEY)   and not _groq_rate_limited,
        "gemini": bool(config.GEMINI_API_KEY)  and not _gemini_rate_limited,
    }
    primary = config.PRIMARY_LLM
    ordered = []
    for key in ([primary] + [k for k in pool if k != primary]):
        if available.get(key):
            ordered.append(pool[key])
    if bool(config.OPENROUTER_API_KEY) and not _openrouter_rate_limited:
        ordered.append(("OpenRouter (free)", _call_openrouter))
    if bool(config.COHERE_API_KEY) and not _cohere_rate_limited:
        ordered.append(("Cohere (command-r-plus)", _call_cohere))
    if not ordered:
        raise RuntimeError(
            "No LLM providers available. "
            f"Groq={_groq_rate_limited}, Gemini={_gemini_rate_limited}, "
            f"OpenRouter={_openrouter_rate_limited}, Cohere={_cohere_rate_limited}. "
            "Will retry next run."
        )
    return ordered


# =============================================================================
# Zero-Cost AI Consensus Model — Generate + Audit Orchestrator
# =============================================================================

def generate_and_audit_config(
    user_prompt: str,
    log_callback=None,
) -> tuple[SearchConfig, str]:
    """
    Orchestrates the two-LLM consensus pipeline:

        Step 1 — Generation:
            The PRIMARY LLM (config.PRIMARY_LLM) generates the SearchConfig
            JSON using the full retry + self-correction loop from
            parse_prompt_to_config(). This is the existing, hardened path.

        Step 2 — Auditing (Zero-Cost Cross-Check):
            The SECONDARY LLM (the other configured provider) is given:
                - The original user prompt
                - The generated SearchConfig as JSON
            It is asked to respond in 1-2 sentences: does the JSON accurately
            capture the user's intent? Any missed keywords, hallucinated
            assumptions, or incorrect values?

            The auditor uses a plain-text call (no JSON mode) at temperature
            0.2 — a small amount of variance helps catch subtle mismatches
            that zero-temperature reasoning would agree with.

        Graceful Degradation:
            If only one LLM API key is configured, Step 2 is skipped and
            the audit_report string reads:
                "Audit skipped: Only one LLM configured."
            The function still returns a valid SearchConfig — the audit is
            advisory, not blocking. The user sees the skip reason in the UI.

        Zero-Cost Design:
            Both LLMs are free-tier APIs. The auditor call is a single
            short completion (max 128 tokens). No additional cost beyond
            the generation call. The generator and auditor are always
            different providers — Groq cannot audit its own output.

    Args:
        user_prompt:   Raw text from the Streamlit command center.
        log_callback:  Optional callable(str) for live UI log streaming.

    Returns:
        (SearchConfig, audit_report_string)
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    # ── Step 1: Generation ────────────────────────────────────────────────────
    _log("[NLP] Step 1/2 — Generating SearchConfig...")
    search_config = parse_prompt_to_config(user_prompt, log_callback=log_callback)
    _log("[NLP] SearchConfig generated and validated.")

    # ── Step 2: Auditing ─────────────────────────────────────────────────────
    # Build auditor chain: all available plain callers except the primary
    # generator. Cross-check independence — a provider cannot audit itself.
    _plain_pool = [
        ("groq",       "Groq (llama3-70b)",      _call_groq_plain,       bool(config.GROQ_API_KEY)       and not _groq_rate_limited),
        ("gemini",     "Gemini (flash)",          _call_gemini_plain,     bool(config.GEMINI_API_KEY)     and not _gemini_rate_limited),
        ("openrouter", "OpenRouter (gemma-27b)",  _call_openrouter_plain, bool(config.OPENROUTER_API_KEY) and not _openrouter_rate_limited),
        ("cohere",     "Cohere (command-r-plus)", _call_cohere_plain,     bool(config.COHERE_API_KEY)     and not _cohere_rate_limited),
    ]
    auditor_chain = [
        (name, caller)
        for key, name, caller, available in _plain_pool
        if available and key != config.PRIMARY_LLM
    ]

    if not auditor_chain:
        audit_report = "Audit skipped: No secondary LLM available for cross-check."
        _log(f"[AUDIT] {audit_report}")
        return search_config, audit_report

    audit_prompt = _AUDITOR_PROMPT.format(
        user_prompt=user_prompt,
        config_json=search_config.model_dump_json(indent=2),
    )

    for auditor_name, auditor_caller in auditor_chain:
        _log(f"[AUDIT] Step 2/2 — Sending to {auditor_name} for cross-audit...")
        try:
            audit_report = auditor_caller(audit_prompt)
            _log(f"[AUDIT] {auditor_name} audit complete.")
            logger.info("[AUDIT REPORT] %s", audit_report)
            return search_config, audit_report
        except Exception as exc:
            _log(f"[AUDIT] Warning — {auditor_name} failed: {exc}. Trying next auditor.")

    audit_report = (
        "Audit unavailable: all auditor providers failed. "
        "Config was still validated by Pydantic."
    )
    _log(f"[AUDIT] {audit_report}")
    return search_config, audit_report


# =============================================================================
# Job Description Extraction Prompt — Personalized Advisor Edition
# =============================================================================

def _build_extraction_prompt() -> str:
    """
    Builds the extraction system prompt with user_profile.yaml injected.
    Falls back to hardcoded defaults if the YAML is missing or unreadable.
    """
    import yaml
    from pathlib import Path
    profile_path = Path(__file__).parent / "user_profile.yaml"
    try:
        with open(profile_path, "r", encoding="utf-8") as f:
            profile = yaml.safe_load(f)
    except Exception:
        profile = {
            "location": "Lima, Peru",
            "timezone": "EST / UTC-5",
            "role": "Backend / SRE / Automation / DevOps",
            "core_skills": ["Python", "AWS", "GCP", "Docker", "Automation", "No-Code", "APIs"],
        }
    skills = ", ".join(profile.get("core_skills", []))
    location = profile.get("location", "Lima, Peru")
    timezone = profile.get("timezone", "EST / UTC-5")
    role = profile.get("role", "Backend / SRE / Automation / DevOps")
    audit_signals = ", ".join(profile.get("audit_signals", [
        "audit logs", "audit trail", "zero data loss", "ACID compliance",
        "data integrity", "idempotency", "DevSecOps", "security hardening",
        "traceability", "determinism", "SRE", "systems reliability",
    ]))
    projects = "; ".join(
        f"{p['name']} ({p['description']})"
        for p in profile.get("key_projects", [])
    ) or "Vigil-SRE (infra monitoring); Micro-Billing-Ledger (idempotent ledger); Aequitas (privacy auditing)"
    return f"""You are a High-Precision Data Extraction Engine and Personalized Job Advisor specializing in Backend Engineering job market analysis. Your goal is to transform messy, unformatted job board text into structured, deterministic JSON data — personalized against a specific user profile.

════════════════════════════════════════════════════════
Core Directive: THE SHY PRINCIPLE
Accuracy over quantity. If not explicitly stated → null or [].
You are FORBIDDEN from inferring. If you hallucinate a single
technology or salary figure not in the source text, the system fails.
EXCEPTION: location_strictness, location_notes, and cv_match_score
are ALWAYS required — derive them from context even when brief.
════════════════════════════════════════════════════════

═══ USER PROFILE (personalization only — never use to drop jobs) ═══
Location  : {location}
Timezone  : {timezone}
Role      : {role}
Skills    : {skills}
Projects  : {projects}

═══ 1. SALARY & CURRENCY ═══
• Extract only numerical values. "Competitive", "DOE", "Market rate" → null.
• Single figure ("$120k") → salary_min: 120000, salary_max: 120000.
• Range ("$90k-$120k") → salary_min: 90000, salary_max: 120000.
• Default currency to "USD" when $ present and no country context contradicts it.
• If the company is clearly Canadian and uses $, set currency to "CAD".

═══ 2. TECH STACK ═══
• required_tech: only technologies marked as Required/Must have/Minimum qualifications.
• preferred_tech: only technologies marked as Preferred/Nice to have/Bonus/Plus.
• If the post does not distinguish required vs preferred → put ALL tech in required_tech.
• Seeing "Kubernetes" does NOT mean "Docker" unless Docker is explicitly written.

═══ 3. EXPERIENCE ═══
• "3-5 years" → experience_min_years: 3, experience_max_years: 5
• "3+ years" → experience_min_years: 3, experience_max_years: null
• "at least 3 years" → experience_min_years: 3, experience_max_years: null
• Not stated → both null

═══ 4. REMOTE REGION ═══
• "US"     — Explicitly US only
• "EU"     — Explicitly EU only
• "CA"     — Explicitly Canada only
• "GLOBAL" — Explicitly global / anywhere / worldwide
• null     — On-site, hybrid only, or location not stated
is_hybrid: true ONLY if the word "hybrid" appears explicitly in the post.

═══ 5. LOCATION STRICTNESS (ALWAYS required — never null) ═══
Evaluate from the perspective of the user in {location} ({timezone}):
• "Strict"   — Requires US citizenship, security clearance, or physical US presence.
               Explicitly excludes non-US or LATAM candidates.
               The user in {location} cannot realistically apply.
• "Flexible" — Mentions US hours or US preference but does NOT require US residency.
               Remote contractors or timezone-compatible candidates may qualify.
               The user COULD apply as a contractor or remote worker.
• "Match"    — Explicitly allows global, LATAM, South America, or has no restriction.
               The user is a clear geographic fit.

location_notes (ALWAYS required — never null):
• 1-2 sentences specific to someone in {location} ({timezone}).
• Reference the relevant phrase from the job text.
• Example: "Requires EST hours only, no US residency mentioned — Lima is UTC-5, a match."

═══ 6. COMPANY IDENTIFICATION (CRITICAL — never use board name) ═══
The "company" field MUST be the ACTUAL HIRING company, not the job board.
Job boards that are NOT the company: Ashby, G2i, Himalayas, Wellfound, Greenhouse,
  WorkingNomads, Remotivated, WelcomeToTheJungle, PostHog, HackerNews, YCombinator.
Rules:
• Look inside the job description for phrases like "Working at [Company]", "About [Company]",
  "Join [Company]", "We are [Company]", or any named employer in the body text.
• If the description text begins with a company name (first proper noun before a comma or dash)
  that is not a job board, that is the company.
• If the description says "FinTech startup in London" with no name → "FinTech Startup (London)".
• If no company is identifiable after careful reading → null (this job will be dropped).
• NEVER set company to: Ashby, G2i, Himalayas, Wellfound, Greenhouse, WorkingNomads,
  Remotivated, WelcomeToTheJungle, PostHog, HackerNews, YCombinator.

═══ 7. CV MATCH SCORE (ALWAYS required) ═══
User core skills: {skills}
• Base score = matching skills / total job skills. Round to 2 decimal places.
• If tech stack is empty: 0.3 if the role title matches the user's role, else 0.0.
• 1.0 = perfect overlap. 0.0 = zero overlap.

AUDIT MENTALITY BONUS — add these bonuses on top of the base score (cap total at 1.0):

CRITICAL — 0.8+ RULE: If the job explicitly requires or values ANY of the following,
the FINAL cv_match_score MUST be at least 0.80 regardless of tech overlap:
  Audit signals: {audit_signals}
  Reason: This candidate's entire career is built around these disciplines (Vigil-SRE,
  Micro-Billing-Ledger idempotency, Aequitas privacy auditing). These are not nice-to-haves
  — they are his core professional identity.

BONUS STACK (apply additively, then cap at 1.0):
• +0.30 if the job explicitly mentions "zero data loss", "audit-ready", "ACID compliance",
  "idempotency", or "data integrity" as requirements.
• +0.25 if the job mentions "DevSecOps", "security compliance", "audit logs", "audit trail",
  "security hardening", or "traceability".
• +0.15 if the job mentions "SRE", "systems reliability", "incident response", "on-call",
  "observability", or "distributed systems reliability".
• +0.10 if the job mentions "CI/CD pipelines", "infrastructure as code", "monitoring",
  "alerting", "Prometheus", "Grafana", "OpenTelemetry", or "structured logging".
• Round final score to 2 decimal places. Cap at 1.0.

═══ 8. INTEGRITY SCORE ═══
Start at 0.0, add each that applies:
• +0.2 if salary is clearly stated as a number (not "competitive" or "DOE")
• +0.2 if tech stack is explicitly defined (at least 1 technology named)
• +0.2 if experience years are clearly stated
• +0.2 if remote/location status is unambiguous (any non-null remote_region)
• +0.2 if company name AND job title are both clearly present
Result is your integrity_score. Round to 2 decimal places.

═══ 9. OUTPUT RULES ═══
• employment_type allowed values ONLY: full-time, contract, part-time, internship, null
• seniority_level allowed values ONLY: Staff, Principal, Senior, Mid, Junior, Lead, null
• location_strictness allowed values ONLY: Strict, Flexible, Match
• source_url: use the URL provided in the input context. If none provided → null.
• Multiple postings in input → return all as separate objects in the "jobs" list.
• Duplicate post, ghost post (no real requirements), or unreadable garbage → {{"jobs": []}}.
• Missing title OR missing company → skip that entry entirely. Do not include it.
• Return ONLY valid JSON matching the schema below.
• No prose. No markdown fences. No "Here is the data." Just the JSON.

EXPECTED JSON SCHEMA:
{{
  "jobs": [
    {{
      "title": "Backend Engineer",
      "company": "TechCorp",
      "seniority_level": "Senior",
      "employment_type": "full-time",
      "salary_min": 120000,
      "salary_max": 150000,
      "currency": "USD",
      "remote_region": "GLOBAL",
      "is_hybrid": false,
      "location_strictness": "Match",
      "location_notes": "Explicitly open to LATAM candidates — Lima is a perfect fit.",
      "required_tech": ["Python", "FastAPI"],
      "preferred_tech": ["Docker"],
      "experience_min_years": 4,
      "experience_max_years": null,
      "source_url": "https://example.com/job/123",
      "cv_match_score": 0.75,
      "integrity_score": 1.0
    }}
  ]
}}"""


_EXTRACTION_SYSTEM_PROMPT = _build_extraction_prompt()


_EXTRACTION_RETRY_INJECT = """Your previous extraction response failed validation:
ERROR: {error}
YOUR RESPONSE WAS: {raw}

Fix only the validation error. Keep all other fields exactly as they were.
Return ONLY the corrected JSON object matching the schema. Nothing else."""


# =============================================================================
# Extraction Function
# =============================================================================

def extract_jobs_from_text(
    raw_text: str,
    source_url: Optional[str] = None,
    log_callback=None,
) -> ExtractionResult:
    """
    Converts raw job posting text into a validated list of ExtractedJob objects.

    This is a SEPARATE pipeline from parse_prompt_to_config(). That function
    converts the USER's natural language search intent into a SearchConfig.
    This function converts RAW JOB BOARD TEXT into structured job data.

    The two pipelines are complementary:
        User prompt → parse_prompt_to_config() → SearchConfig (what to look for)
        Raw job text → extract_jobs_from_text() → ExtractionResult (what was found)

    Args:
        raw_text:     Raw text scraped from a job board. Can be one or many postings.
        source_url:   Optional URL to echo into each extracted job's source_url field.
                      Enables deduplication at the extraction layer.
        log_callback: Optional callable(str) for live Streamlit log streaming.

    Returns:
        ExtractionResult with a list of ExtractedJob objects.
        Returns ExtractionResult(jobs=[]) for garbage input — never raises on bad text.

    Retry strategy:
        Schema/validation failures (Pydantic ValidationError) → inject error
            back into conversation for LLM self-correction. Same as parse flow.
        Network failures → break to next provider (Groq → Gemini or vice versa).
        After all retries: returns ExtractionResult(jobs=[]) with a log warning.
        Never propagates an exception — the caller must not crash on bad job text.
    """
    def _log(msg: str) -> None:
        logger.info(msg)
        if log_callback:
            log_callback(msg)

    if not raw_text or not raw_text.strip():
        _log("[EXTRACT] Empty input — returning empty result.")
        return ExtractionResult(jobs=[])

    # Inject source_url into the prompt so the LLM echoes it back
    url_context = f"\nSOURCE URL: {source_url}" if source_url else ""
    user_message = f"Extract all job postings from the following text:{url_context}\n\n{sanitize_input(raw_text)}"

    callers = _build_caller_chain()
    last_error: Optional[Exception] = None

    for llm_name, caller in callers:
        _log(f"[EXTRACT] Calling {llm_name}...")

        messages = [
            {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ]
        raw_response = ""

        for attempt in range(1, config.LLM_MAX_RETRIES + 1):
            try:
                raw_response = caller(messages)
                _log(f"[EXTRACT] Response received. Validating (attempt {attempt})...")

                # Strip markdown fences if LLM ignored the "no fences" rule
                cleaned = raw_response.strip()
                if cleaned.startswith("```"):
                    cleaned = "\n".join(
                        line for line in cleaned.split("\n")
                        if not line.strip().startswith("```")
                    ).strip()

                data = json.loads(cleaned)

                # Pydantic validates every field in every job object
                result = ExtractionResult(**data)

                # Backfill source_url if the LLM left it null and we have it
                if source_url:
                    for job in result.jobs:
                        if job.source_url is None:
                            object.__setattr__(job, "source_url", source_url)

                _log(f"[EXTRACT] {len(result.jobs)} job(s) extracted and validated.")
                return result

            except (json.JSONDecodeError, ValueError, Exception) as exc:
                from pydantic import ValidationError as PydanticValidationError
                is_schema_error = isinstance(exc, (PydanticValidationError, ValueError, json.JSONDecodeError))

                last_error = exc
                _log(
                    f"[EXTRACT] {'Schema' if is_schema_error else 'Network'} error "
                    f"on attempt {attempt}/{config.LLM_MAX_RETRIES} ({llm_name}): {exc}"
                )

                if is_schema_error and attempt < config.LLM_MAX_RETRIES:
                    # Inject the error for LLM self-correction
                    messages += [
                        {"role": "assistant", "content": raw_response},
                        {"role": "user", "content": _EXTRACTION_RETRY_INJECT.format(
                            error=str(exc), raw=raw_response
                        )},
                    ]
                elif not is_schema_error:
                    # Network error — break immediately, try next LLM
                    break

    _log(f"[EXTRACT] All attempts exhausted. Last error: {last_error}. Returning empty result.")
    return ExtractionResult(jobs=[])
