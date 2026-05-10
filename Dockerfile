# =============================================================================
# Dockerfile — Project GEMA
# Base: Microsoft Playwright Python image (includes Chromium + all system deps)
# =============================================================================

FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

WORKDIR /app

# Install Python dependencies first (cached layer — only rebuilds on requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium browser binaries (system deps already in base image)
RUN playwright install chromium

# Copy source code
COPY . .

# Streamlit port
EXPOSE 8501

# Disable Streamlit's browser auto-open and email prompt in headless environments
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
ENV STREAMLIT_SERVER_HEADLESS=true

CMD ["python", "-m", "streamlit", "run", "main.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true"]
