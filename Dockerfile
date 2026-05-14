FROM mcr.microsoft.com/playwright/python:v1.59.0-noble

WORKDIR /app

# Install Python deps (Playwright system libs are already in the base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# v1.59 split chromium into full + headless-shell; ensure both are present
# regardless of what the base image happens to ship.
RUN python -m playwright install chromium chromium-headless-shell

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
