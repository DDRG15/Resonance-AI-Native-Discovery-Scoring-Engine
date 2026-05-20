# =============================================================================
# Dockerfile — Project GEMA
# Base: Microsoft Playwright Python image (includes Chromium + all system deps)
#
# TWO BROWSERS ARE INSTALLED:
#   1. Chromium (playwright install chromium) — all boards except remoteok.com
#   2. Firefox via camoufox (python -m camoufox fetch) — remoteok.com only
#      camoufox randomizes TLS/JA3 fingerprints to bypass Cloudflare Bot Fight
#      Mode, which blocks Chromium's BoringSSL fingerprint reliably.
#
# Firefox binary download adds ~530MB to the image. If remoteok.com is not
# in your target_domains, you can comment out the camoufox fetch line to
# save space — the scraper degrades gracefully without it.
# =============================================================================

FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

WORKDIR /app

# Install Python dependencies first (cached layer — only rebuilds on requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install browser binaries
# Chromium: used by Playwright for all job boards except remoteok.com
RUN playwright install chromium

# Firefox via camoufox: used only for remoteok.com Cloudflare bypass (~530MB)
# Set CAMOUFOX_HOME so the binary lands in a predictable, writable location
ENV CAMOUFOX_HOME=/app/.camoufox
RUN python -m camoufox fetch

# Copy source code (after browser installs — source changes don't invalidate browser cache)
COPY . .

# ── Sealed delivery: compile sources to .pyc then remove .py ─────────────────
# This leaves only bytecode in the image — recipients cannot read source code.
# __init__.py files are kept so Python's import machinery can find packages.
# .venv and .camoufox are excluded — no Python sources there anyway.
RUN python -m compileall -q . -b && \
    find . -name "*.py" ! -name "__init__.py" ! -path "./.venv/*" ! -path "./.camoufox/*" -delete

# Streamlit port
EXPOSE 8501

# Disable Streamlit's browser auto-open and email prompt in headless environments
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV STREAMLIT_SERVER_HEADLESS=true

CMD ["python", "-m", "streamlit", "run", "main.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true"]
