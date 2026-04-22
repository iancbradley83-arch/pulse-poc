FROM python:3.11-slim

WORKDIR /app

# Build context = repo root (so paths reference `backend/`).
COPY backend/requirements.txt .
# Cache-bust 2026-04-22: slowapi was missed on a prior build despite
# requirements.txt changing. Force a fresh pip install layer.
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY backend/app/ app/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
