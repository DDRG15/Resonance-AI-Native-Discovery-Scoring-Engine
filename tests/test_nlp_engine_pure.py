"""
tests/test_nlp_engine_pure.py — Tests for pure functions in nlp_engine.py.

No LLM API calls are made. These functions have zero external dependencies:
    - sanitize_input       — HTML stripping / whitespace normalization
    - _extract_json_from_text — brace-depth JSON extractor
    - _is_rate_limit       — HTTP 429 / quota detection across providers
    - reset_rate_limit_flags — module-level rate-limit state reset
    - _build_extraction_prompt — profile dict → system prompt string
"""

import pytest
import nlp_engine


# =============================================================================
# sanitize_input
# =============================================================================

def test_sanitize_input_strips_script_tags():
    raw = "<script>alert('xss')</script>Hello"
    result = nlp_engine.sanitize_input(raw)
    assert "alert" not in result
    assert "Hello" in result


def test_sanitize_input_strips_style_blocks():
    raw = "<style>.body { color: red; }</style>Content"
    result = nlp_engine.sanitize_input(raw)
    assert "color" not in result
    assert "Content" in result


def test_sanitize_input_strips_inline_tags():
    raw = "<h2>Job Title</h2><span class='company'>Acme</span>"
    result = nlp_engine.sanitize_input(raw)
    assert "<h2>" not in result
    assert "<span" not in result
    assert "Job Title" in result
    assert "Acme" in result


def test_sanitize_input_collapses_whitespace():
    raw = "Senior   Engineer\n\n\t  Backend"
    result = nlp_engine.sanitize_input(raw)
    assert "  " not in result
    assert "Senior Engineer" in result


def test_sanitize_input_strips_multiline_script():
    raw = "<script type='text/javascript'>\nvar x = 1;\n</script>After"
    result = nlp_engine.sanitize_input(raw)
    assert "var x" not in result
    assert "After" in result


def test_sanitize_input_empty_string():
    result = nlp_engine.sanitize_input("")
    assert result == ""


def test_sanitize_input_plain_text_unchanged():
    raw = "Senior Backend Engineer at Acme Corp"
    result = nlp_engine.sanitize_input(raw)
    assert result == raw


# =============================================================================
# _extract_json_from_text
# =============================================================================

def test_extract_json_plain_object():
    text = '{"key": "value"}'
    result = nlp_engine._extract_json_from_text(text)
    assert result == {"key": "value"}


def test_extract_json_with_leading_prose():
    text = 'Here is the JSON: {"title": "SRE"}'
    result = nlp_engine._extract_json_from_text(text)
    assert result == {"title": "SRE"}


def test_extract_json_with_trailing_prose():
    text = '{"score": 42} That was the result.'
    result = nlp_engine._extract_json_from_text(text)
    assert result == {"score": 42}


def test_extract_json_returns_first_object_only():
    text = '{"first": 1} {"second": 2}'
    result = nlp_engine._extract_json_from_text(text)
    assert result == {"first": 1}


def test_extract_json_nested_objects():
    text = '{"outer": {"inner": "value"}}'
    result = nlp_engine._extract_json_from_text(text)
    assert result["outer"]["inner"] == "value"


def test_extract_json_raises_on_no_json():
    with pytest.raises(ValueError, match="No valid JSON"):
        nlp_engine._extract_json_from_text("no json here at all")


def test_extract_json_raises_on_empty_string():
    with pytest.raises(ValueError):
        nlp_engine._extract_json_from_text("")


def test_extract_json_handles_code_block_wrapper():
    text = "```json\n{\"jobs\": []}\n```"
    result = nlp_engine._extract_json_from_text(text)
    assert result == {"jobs": []}


# =============================================================================
# _is_rate_limit
# =============================================================================

def test_is_rate_limit_detects_429_in_message():
    exc = Exception("HTTP 429 Too Many Requests")
    assert nlp_engine._is_rate_limit(exc) is True


def test_is_rate_limit_detects_rate_limit_phrase():
    exc = Exception("rate limit exceeded for model")
    assert nlp_engine._is_rate_limit(exc) is True


def test_is_rate_limit_detects_quota():
    exc = Exception("daily quota exhausted")
    assert nlp_engine._is_rate_limit(exc) is True


def test_is_rate_limit_detects_too_many_requests():
    exc = Exception("too many requests from this ip")
    assert nlp_engine._is_rate_limit(exc) is True


def test_is_rate_limit_returns_false_for_unrelated_error():
    exc = Exception("Connection timeout")
    assert nlp_engine._is_rate_limit(exc) is False


def test_is_rate_limit_returns_false_for_5xx():
    exc = Exception("Internal Server Error 500")
    assert nlp_engine._is_rate_limit(exc) is False


def test_is_rate_limit_detects_resource_exhausted_class_name():
    class ResourceExhaustedError(Exception):
        pass
    exc = ResourceExhaustedError("quota")
    assert nlp_engine._is_rate_limit(exc) is True


def test_is_rate_limit_detects_ratelimit_in_class_name():
    class RateLimitError(Exception):
        pass
    exc = RateLimitError("limit hit")
    assert nlp_engine._is_rate_limit(exc) is True


# =============================================================================
# reset_rate_limit_flags
# =============================================================================

def test_reset_rate_limit_flags_clears_all():
    # Set all flags to True
    nlp_engine._groq_rate_limited = True
    nlp_engine._gemini_rate_limited = True
    nlp_engine._openrouter_rate_limited = True
    nlp_engine._cohere_rate_limited = True

    nlp_engine.reset_rate_limit_flags()

    assert nlp_engine._groq_rate_limited is False
    assert nlp_engine._gemini_rate_limited is False
    assert nlp_engine._openrouter_rate_limited is False
    assert nlp_engine._cohere_rate_limited is False


def test_reset_rate_limit_flags_idempotent():
    nlp_engine.reset_rate_limit_flags()
    nlp_engine.reset_rate_limit_flags()

    assert nlp_engine._groq_rate_limited is False
    assert nlp_engine._gemini_rate_limited is False


# =============================================================================
# _build_extraction_prompt
# =============================================================================

def test_build_extraction_prompt_contains_skills():
    profile = {
        "core_skills": ["Python", "FastAPI", "Docker"],
        "location": "Remote",
        "timezone": "UTC-5",
        "role": "Backend Engineer",
        "audit_signals": [],
        "key_projects": [],
    }
    result = nlp_engine._build_extraction_prompt(profile)
    assert "Python" in result
    assert "FastAPI" in result
    assert "Docker" in result


def test_build_extraction_prompt_contains_location():
    profile = {
        "core_skills": [],
        "location": "Buenos Aires, Argentina",
        "timezone": "ART / UTC-3",
        "role": "SRE",
        "audit_signals": [],
        "key_projects": [],
    }
    result = nlp_engine._build_extraction_prompt(profile)
    assert "Buenos Aires" in result
    assert "UTC-3" in result


def test_build_extraction_prompt_uses_defaults_on_empty_profile():
    result = nlp_engine._build_extraction_prompt({})
    # Should not raise — all fields have defaults
    assert isinstance(result, str)
    assert len(result) > 100


def test_build_extraction_prompt_contains_audit_signals():
    profile = {
        "core_skills": [],
        "audit_signals": ["zero downtime", "event sourcing"],
        "key_projects": [],
    }
    result = nlp_engine._build_extraction_prompt(profile)
    assert "zero downtime" in result
    assert "event sourcing" in result


def test_build_extraction_prompt_contains_project_descriptions():
    profile = {
        "core_skills": [],
        "audit_signals": [],
        "key_projects": ["Distributed ledger with ACID guarantees", "SRE monitoring pipeline"],
    }
    result = nlp_engine._build_extraction_prompt(profile)
    assert "Distributed ledger" in result
    assert "SRE monitoring pipeline" in result
