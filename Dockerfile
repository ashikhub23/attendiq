FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# Chromium + matching chromedriver for the Selenium portal automation
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        chromium-driver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements-prod.txt .
RUN pip install --no-cache-dir -r requirements-prod.txt

COPY . .

RUN mkdir -p /app/database /app/uploads /app/generated_reports /app/logs /app/screenshots

ENV PORT=8000
EXPOSE 8000

# 1 worker keeps the Selenium bot thread + SQLite writes in a single process;
# 8 threads handle concurrent polling/streaming requests.
CMD ["sh", "-c", "gunicorn --worker-class gthread --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT run:app"]
