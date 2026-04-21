FROM python:3.11-slim

WORKDIR /app

# Build context = repo root (so we reference paths through `backend/`).
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + the OS libraries Playwright needs to drive it. We only
# need chromium (not firefox/webkit) — saves ~250MB of image. `--with-deps`
# installs apt packages too via apt-get; needs root (which we have here).
RUN python -m playwright install --with-deps chromium

COPY backend/app/ app/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
