# Syslog AI Monitor

A lightweight, self-hosted service for a **Raspberry Pi 4B** (or any Linux host) that:

- **Receives** syslog from a **UniFi UDM Pro** (UDP/TCP port 514; CEF/SIEM format supported),
- **Evaluates** the accumulated logs **every hour** with **Claude Haiku**, surfacing real errors and anomalies,
- **Shows** the latest status and a 30-day history in a small **web dashboard**,
- **Emails** you when something needs attention,
- **Discards raw logs** after each evaluation — only the AI findings are kept.

It runs as a single Docker container, survives reboots, and is tuned to ignore
routine UniFi internal noise. One command to start.

---

## How it works

```
UDM Pro ──syslog:514──▶ listener ──▶ raw_logs (ephemeral SQLite)
                                          │  every hour (APScheduler)
                                          ▼
                                    pre-process  ──▶ compact digest
                                    (mask IPs/ports, group + count
                                     patterns, sample errors)
                                          │
                                          ▼
                                   Claude Haiku  ──▶ structured findings
                                          │
                       findings table ───┼──▶ web dashboard (port 8080)
                       (30-day history)  └──▶ email alert (if error+)
                                          │
                                   raw logs deleted
```

The **pre-processing** step is what keeps this cheap and fast: UDM Pro firewall
logging can be tens of thousands of near-identical lines per hour, so instead of
sending raw logs to the model, the service masks volatile tokens (IPs, MACs,
ports, timestamps), groups identical message templates with counts, and keeps a
capped sample of elevated-severity lines. That small digest is what Claude sees.

The model is also **tuned to treat routine UniFi internal chatter as benign**
(udapi-server process notifications, mca-ctrl IPC churn, DPI packet-capture rate
messages, wireless retry telemetry, transient ARP/NTP blips, etc.), so
`error`/`warning` status reflects things that actually matter rather than normal
daemon noise. That guidance lives in the system prompt in `app/claude_client.py`.

---

## Quick start (headless Pi)

You need: Docker + Docker Compose, and an [Anthropic API key](https://console.anthropic.com).

```bash
git clone https://github.com/jeffstrout/syslog-ai-monitor.git
cd syslog-ai-monitor
cp .env.example .env
nano .env          # paste your ANTHROPIC_API_KEY (and SMTP settings for email)
docker compose up -d --build
```

Then open **`http://<pi-ip>:8080`** in a browser on your network. (See
[Operations](#operations) below for logs, updates, and reboot behavior.)

---

## Point the UDM Pro at the Pi

In the UniFi Network application (tested on **Network 10.4.x**):

**Settings → CyberSecure → Traffic Logging → Activity Logging (Syslog)**

- Select **SIEM Server**
- **Server Address:** your Pi's LAN IP (e.g. `192.168.1.10`)
- **Port:** `514`
- Click **Apply Changes**.

> **Finding it:** the fastest way is to type **`syslog`** into the Settings search
> box — it jumps straight to the page. On older UniFi versions the equivalent
> setting lived under **Settings → System → Remote Logging**; on current firmware
> it's the CyberSecure path above.

The UDM Pro's SIEM export sends logs in **CEF format** rather than classic syslog.
This monitor parses CEF natively — it reads CEF's own Severity field so firewall
blocks and threat events are surfaced while routine allowed-traffic noise is
de-prioritized. No extra configuration needed.

> **Flow Logging:** "Blocked Traffic Only" is a good low-noise default. "All
> Traffic" sends far more (every allowed connection) — usable, but noisier.

---

## Configuration (`.env`)

See [`.env.example`](.env.example) for the full list. The important ones:

| Variable | Default | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required.** From console.anthropic.com |
| `CLAUDE_MODEL` | `claude-haiku-4-5-20251001` | Evaluation model |
| `EVAL_INTERVAL_MINUTES` | `60` | Evaluation cadence. `60` runs at the **top of every hour** (9:00, 10:00). Divisors of 60 (30, 15, …) also align to the hour |
| `TZ` | `America/Chicago` | Timezone for scheduling + log timestamps. Set to your IANA zone so "top of the hour" means your local time |
| `RETENTION_DAYS` | `30` | How long findings are kept |
| `ALERT_MIN_SEVERITY` | `error` | Email when a finding is this severity or higher |
| `SMTP_HOST` … | — | Leave `SMTP_HOST` blank to disable email |

> **Scheduling:** evaluations run on a wall-clock cron aligned to the hour, so
> after a reboot or update they still land on clean times (not offset from when
> the container started). The startup log prints the next scheduled run.

**Email via Gmail:** set `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, your address
as `SMTP_USER`, and an [App Password](https://myaccount.google.com/apppasswords)
(not your normal password) as `SMTP_PASS`.

> **Ports:** the container listens on `514` and `8080` internally. To use
> different *host* ports, change the left side of the mappings in
> `docker-compose.yml` (e.g. `"5514:514/udp"`), not the `.env` values.

---

## Cost

One Haiku call per hour (~24/day) over a small digest costs only a few cents per
month at typical home-network volume. Haiku 4.5 is priced at $1 / $5 per million
input / output tokens, and each digest is a few thousand tokens.

---

## Web dashboard & API

The dashboard (`http://<pi-ip>:8080/`) shows the latest evaluation and history and
auto-refreshes every minute. It's open on your LAN with no login — intended for a
trusted home network.

A small JSON API backs it:

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/api/health` | Health check (`200` ok / `503` error) |
| `GET`  | `/api/status` | Buffered-log count + latest finding |
| `GET`  | `/api/latest` | The single most recent finding |
| `GET`  | `/api/history?limit=N` | Recent findings, newest first (default 200) |
| `GET`/`POST` | `/api/run-now` | Run an evaluation immediately (testing) |

The dashboard footer also links to these (Health, API docs, History/Status JSON)
and has a **Run evaluation now** button.

**Full API reference:** [`docs/API.md`](docs/API.md) — endpoints, JSON shapes, and
examples.

**Interactive docs are built in** (auto-generated by FastAPI from the running
service):

- Swagger UI: `http://<pi-ip>:8080/docs`
- ReDoc: `http://<pi-ip>:8080/redoc`
- OpenAPI schema: `http://<pi-ip>:8080/openapi.json`

---

## Operations

**View logs / health:**
```bash
docker compose logs -f          # live container logs
docker compose ps               # is it running? (look for "Up")
curl -s http://localhost:8080/api/status   # buffered-log count + latest finding
```

**Update to the latest version:**
```bash
cd syslog-ai-monitor
git pull && docker compose up -d --build
```

**Change settings** (interval, retention, alert threshold, SMTP): edit `.env`, then
`docker compose up -d` to apply.

**Restarts & power loss:** the container runs with `restart: unless-stopped` and
Docker starts on boot, so the stack **comes back automatically after a reboot** —
no commands needed. Your finding **history persists** across reboots and rebuilds
because the SQLite database lives on the `syslog-data` Docker volume. (Raw logs are
never kept beyond one evaluation.) On a Raspberry Pi, a hard power cut can corrupt
the SD card — a small UPS is worthwhile for a 24/7 deployment.

---

## Testing without waiting an hour

Send a test log line and trigger an evaluation manually:

```bash
# send a fake syslog line to the Pi (from any machine on the LAN)
logger -n <pi-ip> -P 514 -d "test critical error: WAN link down"

# force an evaluation now
curl -X POST http://<pi-ip>:8080/api/run-now
```

---

## Local development (without Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add your key; set DB_PATH=./syslog.db
DB_PATH=./syslog.db SYSLOG_PORT=5514 python -m app.main
```

(Port 514 needs root; use a high port like `5514` for local testing.)

---

## Layout

```
app/
  main.py              entrypoint — listener + scheduler + web server
  config.py            env-driven settings
  db.py                SQLite: raw_logs (ephemeral) + findings (history)
  syslog_listener.py   asyncio UDP/TCP :514, RFC 3164/5424 + CEF parsing
  preprocess.py        token masking, grouping, digest builder
  claude_client.py     Haiku call (forced-tool structured output) + noise-tuned prompt
  evaluator.py         hourly job: digest → Claude → store → purge raw
  alerts.py            SMTP email on error/critical findings
  web.py               FastAPI routes + JSON API
  static/index.html    dashboard (no build step)
docs/
  API.md               full JSON API reference
```

## License

MIT — see [LICENSE](LICENSE).
