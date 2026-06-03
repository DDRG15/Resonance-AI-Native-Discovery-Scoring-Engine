"""scripts/scan_fixtures.py

Scan test fixtures and cassette directories for likely API keys or secrets.
Exit with code 1 if any probable secret is found.

Usage: python scripts/scan_fixtures.py
"""
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
SEARCH_PATHS = [REPO_ROOT / 'tests', REPO_ROOT / 'tests' / 'fixtures', REPO_ROOT / 'tests' / 'cassettes']

# Heuristics: token-like strings or explicit ENV-like assignments
SECRET_PATTERNS = [
    # common named keys
    re.compile(r"(GROQ|GEMINI|OPENROUTER|COHERE|NOTION|DISCORD|SLACK|GOOGLE)_?API_?KEY\s*[=:]\s*['\"]?[A-Za-z0-9_\-]{8,}['\"]?", re.IGNORECASE),
    # bearer-like or sk- tokens
    re.compile(r"\b(sk-[A-Za-z0-9_\-]{20,})\b"),
    # long hex/base64-like strings
    re.compile(r"\b([A-Za-z0-9_\-/]{32,})\b"),
]

ignore_count = 0
matches = []

for base in SEARCH_PATHS:
    if not base.exists():
        continue
    for p in base.rglob('*'):
        if p.is_file():
            try:
                text = p.read_text(encoding='utf8', errors='ignore')
            except Exception:
                continue
            for rx in SECRET_PATTERNS:
                for m in rx.finditer(text):
                    snippet = m.group(0)
                    # ignore very short noisy matches
                    if len(snippet) < 16:
                        ignore_count += 1
                        continue
                    matches.append((str(p.relative_to(REPO_ROOT)), snippet))

if matches:
    print("Potential secrets found in test files/fixtures:")
    for path, snippet in matches:
        print(f" - {path}: {snippet}")
    print("\nFailing build to avoid leaking secrets into CI. Remove or redact fixtures/cassettes before committing.")
    sys.exit(1)

print("No obvious secrets found in tests/fixtures.")
sys.exit(0)
