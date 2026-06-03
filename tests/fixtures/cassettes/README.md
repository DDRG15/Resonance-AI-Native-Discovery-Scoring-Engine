Cassettes (record/playback) — Usage

This directory holds recorded HTTP interactions used by `vcrpy` for integration tests.

Modes
- Playback (default in CI): run tests with `GEMA_TEST_MODE=playback` (CI sets this variable). Tests will not attempt real network calls and will use cassettes.
- Record: to update or create new cassettes locally, run tests with `GEMA_RECORD=1` and ensure you have valid credentials in your local environment (never commit credentials).

Record example:

```bash
# Create or update cassettes for tests matching the pattern
export GEMA_RECORD=1
pytest tests/test_notion_client.py::test_some_flow -q
```

Redaction
- Before committing cassettes, run the redaction helper in `tests/fixtures/utils.py` to remove API keys and PII.
- Prefer `vcrpy` hooks to filter sensitive headers and body fields.

CI
- CI runs with `GEMA_TEST_MODE=playback` and will fail if tests attempt an outgoing HTTP call not covered by a cassette.
