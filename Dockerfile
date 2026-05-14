FROM mcr.microsoft.com/playwright/python:v1.49.0-noble

WORKDIR /app

# Install Python deps (Playwright + system libs are already in the base image)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}"]
