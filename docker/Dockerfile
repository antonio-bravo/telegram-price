FROM python:3.15-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DEBIAN_FRONTEND=noninteractive \
    DATA_DIR=/app/data

WORKDIR /app

COPY requirements.txt .

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates tzdata \
    && pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium \
    && apt-get autoremove -y && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

COPY monitor.py web_app.py github_sync.py ./
COPY config.json /app/config.json.default
COPY --chmod=755 docker/entrypoint.sh /entrypoint.sh

VOLUME ["/app/data"]

EXPOSE 9999

ENTRYPOINT ["/entrypoint.sh"]