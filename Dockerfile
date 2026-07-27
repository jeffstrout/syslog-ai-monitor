# Multi-arch (works on Raspberry Pi 4B arm64) Python slim image.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Build provenance, injected by CI and surfaced at /api/version. Without this
# there is no way to tell whether a Watchtower auto-update actually landed.
ARG GIT_SHA=dev
ARG BUILD_TIME=""
ENV APP_COMMIT=${GIT_SHA} \
    APP_BUILD_TIME=${BUILD_TIME}

# SQLite lives on a mounted volume.
ENV DB_PATH=/data/syslog.db
VOLUME ["/data"]

# Syslog (UDP+TCP) and the web dashboard.
EXPOSE 514/udp 514/tcp 8080/tcp

CMD ["python", "-m", "app.main"]
