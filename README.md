# Syslog AI Monitor

A lightweight, self-hosted service for a **Raspberry Pi 4B** (or any Linux host) that:

- **Receives** syslog from a **UniFi UDM Pro** (UDP/TCP port 514),
- **Evaluates** the accumulated logs **every hour** with **Claude Haiku**, surfacing real errors and anomalies,
- **Shows** the latest status and a 30-day history in a small **web dashboard**,
- **Emails** you when something needs attention,
- **Discards raw logs** after each evaluation — only the AI findings are kept.

It runs as a single Docker container. One command to start.

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

---

## Quick start (headless Pi)

You need: Docker + Docker Compose, and an [Anthropic API key](https://console.anthropic.com).

```bash
git clone https://github.com/<your-username>/syslog-ai-monitor.git
cd syslog-ai-monitor
cp .env.example .env
nano .env          # paste your ANTHROPIC_API_KEY (and SMTP settings for email)
docker compose up -d --build
```

Then open **`http://<pi-ip>:8080`** in a browser on your network.

Check logs / status:

```bash
docker compose logs -f
```

Update later:

```bash
git pull && docker compose up -d --build
```

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
| `EVAL_INTERVAL_MINUTES` | `60` | How often to evaluate |
| `RETENTION_DAYS` | `30` | How long findings are kept |
| `ALERT_MIN_SEVERITY` | `error` | Email when a finding is this severity or higher |
| `SMTP_HOST` … | — | Leave `SMTP_HOST` blank to disable email |

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

- `http://<pi-ip>:8080/` — dashboard (latest evaluation + history, auto-refreshes)
- `GET /api/status` — buffered log count + latest finding
- `GET /api/history?limit=200` — finding history
- `POST /api/run-now` — trigger an evaluation immediately (handy for testing)

The dashboard is open on your LAN with no login — intended for a trusted home network.

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
  syslog_listener.py   asyncio UDP/TCP :514, RFC 3164/5424 parsing
  preprocess.py        token masking, grouping, digest builder
  claude_client.py     Haiku call with structured-output schema
  evaluator.py         hourly job: digest → Claude → store → purge raw
  alerts.py            SMTP email on error/critical findings
  web.py               FastAPI routes + JSON API
  static/index.html    dashboard (no build step)
```

## License

MIT — see [LICENSE](LICENSE).
