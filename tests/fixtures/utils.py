"""Helpers to redact sensitive fields from recorded fixtures/cassettes."""
import re
from typing import Any, Dict

KEY_PATTERNS = [
    re.compile(r"(GROQ|GEMINI|OPENROUTER|COHERE|NOTION|DISCORD|SLACK)_?API_?KEY", re.IGNORECASE),
]


def redact_response(payload: Any) -> Any:
    """Recursively traverse a JSON-like payload and redact values whose keys
    match known secret patterns.

    Returns a new structure with sensitive values replaced by "[REDACTED]".
    """
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if any(rx.search(k) for rx in KEY_PATTERNS):
                out[k] = '[REDACTED]'
            else:
                out[k] = redact_response(v)
        return out
    elif isinstance(payload, list):
        return [redact_response(x) for x in payload]
    else:
        return payload
