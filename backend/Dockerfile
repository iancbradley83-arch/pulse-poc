# Microsoft's official Playwright image: Ubuntu 22.04 (jammy) + Python 3 +
# Chromium binary + every apt lib it needs already baked in. Skips all the
# distro-rename drama (libasound2t64 on Noble, missing ttf-unifont on Debian
# Trixie, etc.) and the ~180MB chromium download — both are in the image.
# Pinned to playwright 1.50.0 to match requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.50.0-jammy

WORKDIR /app

COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app/ app/

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
