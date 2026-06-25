# Multi-arch (works on Raspberry Pi 4B arm64) Python slim image.
FROM python:3.11-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SQLite lives on a mounted volume.
ENV DB_PATH=/data/syslog.db
VOLUME ["/data"]

# Syslog (UDP+TCP) and the web dashboard.
EXPOSE 514/udp 514/tcp 8080/tcp

CMD ["python", "-m", "app.main"]
