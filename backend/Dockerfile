FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium + the OS libraries Playwright needs to drive it. We only
# need chromium (not firefox/webkit) — saves ~250MB of image. `--with-deps`
# installs apt packages too; it must run as root, before USER drop (we don't
# drop user here, so this is fine).
RUN python -m playwright install --with-deps chromium

COPY app/ app/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
