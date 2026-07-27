# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-container service for a Raspberry Pi that receives syslog from a UniFi
UDM Pro on port 514, evaluates the accumulated logs hourly with Claude Haiku,
rolls those evaluations up daily into a 7-day pattern review, and serves both
from a dashboard on port 8080. Raw logs are deliberately ephemeral — only the
structured AI findings are retained.

## Commands

**Run under Docker (the deployment path):**
```bash
docker compose up -d --build
docker compose logs -f
```

**Run locally without Docker.** Port 514 needs root, so use a high port:
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # add ANTHROPIC_API_KEY
DB_PATH=./syslog.db SYSLOG_PORT=5514 python -m app.main
```

**Exercise the pipeline without waiting for the hour:**
```bash
logger -n localhost -P 5514 -d "test critical error: WAN link down"
curl -X POST http://localhost:8080/api/run-now
curl -X POST http://localhost:8080/api/run-weekly
```

Both `run-now` and `run-weekly` accept GET as well as POST, so a browser visit
works too.

**There is no test suite, linter, formatter, or build step.** Verification is
manual via the endpoints above plus `docker compose logs -f`. Don't invent
`pytest`/`make` invocations — if you add tests, you are establishing the
convention, not following one.

## Architecture

### One process, three concurrent concerns

`app/main.py` is the whole runtime. The syslog listener (UDP + TCP) and the
Uvicorn web server run as coroutines on a single event loop via
`asyncio.gather`, with APScheduler layered on top for the three jobs (hourly
evaluation, daily weekly-review, nightly retention purge). There is no separate
worker process or queue.

### The two-stage AI pipeline

The critical structural point: **the two Claude calls read different sources.**

```
syslog → raw_logs → build_digest() → Haiku → findings → raw_logs DELETED
                                                 ↓
                    findings (last 7d) → _build_weekly_digest() → Haiku → weekly_summaries
```

The hourly stage (`evaluator.run_evaluation`) is the only thing that ever sees
raw log lines. The weekly stage (`evaluator.run_weekly_review`) reads *stored
findings* and never touches `raw_logs` — it aggregates finding titles into a
days-seen / hours-seen / occurrences table, which is the recurrence signal the
model keys on. Changing what the hourly stage stores therefore changes what the
weekly stage can possibly detect.

### Three tables, three different lifetimes

`app/db.py` — all SQLite, one file, WAL mode:

- `raw_logs` — **ephemeral.** Deleted by `delete_logs_through_id(max_id)` after
  every successful evaluation, and bounded by age regardless of outcome (see the
  invariant below). No time series survives here.
- `findings` — hourly results, kept `RETENTION_DAYS` (default 30).
- `weekly_summaries` — pattern reviews, same retention.

### Cost control is the digest, not the model

`app/preprocess.py` is what makes this cheap: `templatize()` masks volatile
tokens (MAC, IPv6, IP, timestamps, hex, **all bare integers**) so tens of
thousands of near-identical firewall lines collapse into one template plus a
count. Three caps bound what reaches the model: `DIGEST_MAX_TEMPLATES` (60),
`DIGEST_MAX_SAMPLES` (200), `DIGEST_MAX_CHARS` (24000).

Consequence worth knowing: **anything numeric is destroyed before the model sees
it.** That's correct for repetitive log lines but makes the digest path unusable
for metrics, where the numbers are the payload.

### Two unrelated severity scales

Easy to conflate — they are different ladders:

1. **Transport severity** (`syslog_listener`, `preprocess`): syslog numeric
   0=emerg … 7=debug. `severity <= 4` means "elevated" and qualifies a line for
   the elevated-severity sample. For CEF lines, the CEF Severity field
   *overrides* the transport PRI, because UniFi's PRI is generic. Note the
   inversion: higher CEF severity = more serious = **lower** syslog number.
2. **Finding severity** (`config.SEVERITY_ORDER`): `info < warning < error <
   critical`, produced by the model and used by `alerts.maybe_send` against
   `ALERT_MIN_SEVERITY`.

### Where signal-to-noise is tuned

`claude_client.SYSTEM_PROMPT` carries an explicit list of benign UniFi chatter
(udapi-server notification noise, mca-ctrl IPC churn, DPI pcap rate messages,
wireless retry telemetry, transient ARP/NTP blips) that the model must not turn
into findings. If `overall_status` is too noisy or too quiet, that prompt is the
knob — not the code.

## Invariants to preserve

**Never wrap scheduler jobs in a lambda that touches the event loop.**
APScheduler's `AsyncIOExecutor` runs sync jobs in a worker thread with *no
running loop*, so `asyncio.get_event_loop()` raises and the job then **silently
never runs**. This already caused commit `d8b3a2b` ("hourly evaluation never
ran"). Pass sync functions directly, as `main.py` does — the thread pool is
exactly where the blocking Anthropic call and SQLite writes belong.

**Raw logs are kept when Claude fails — but bounded.** `run_evaluation` returns
`None` on a model exception *before* purging, so the window isn't lost and the
next run retries it. Any new job that consumes-then-purges must keep this
ordering. It must also keep the ceiling: `_enforce_raw_log_bound()` drops rows
older than `RAW_LOG_MAX_AGE_HOURS` (24) on *every* outcome, and
`EVAL_MAX_ROWS` (500k) caps one run's batch.

Without those bounds this bit hard on 2026-07-27: the Anthropic account ran out
of credit, every hourly run failed with a 400, and 8.9 days of retained logs grew
`raw_logs` to **15.9M rows / 5.0 GB**. Each run then spent **41 minutes** doing
`fetchall()` over the whole table while holding `db._lock` — blocking the shared
event loop, so the dashboard was dark for two-thirds of every hour and was ~4 days
from never finishing at all. Batch deletion is now by row id
(`delete_logs_through_id`), not timestamp, because a capped batch must not delete
rows newer than the ones it evaluated.

**Evaluation failure must alert on its own.** `alerts.maybe_send` only runs after
a *successful* evaluation, so a broken model call is invisible in the finding
history — nothing is written. `alerts.maybe_send_failure` covers that path, and
`/api/health` reports `status: "degraded"` plus `evaluation.last_success_age_seconds`
so an external check can see it. Note this is inert unless SMTP is configured.

**The DB lock is re-entrant on purpose.** `db._lock` is an `RLock` because
helpers acquire it and may call `_db()` → `init()`, which acquires it again
(e.g. a web request arriving before `main.init()` has run). Downgrading it to a
plain `Lock` reintroduces a startup deadlock — see commit `ad232ac`.

**Structured output uses a forced tool call, not `output_config`.** Both Claude
calls define a JSON schema, pass it as a tool, and force `tool_choice`. This is
deliberate for compatibility with the pinned `anthropic==0.69.0`; the tool_use
block's `.input` is already a validated dict, so no JSON parsing is needed.
Follow this pattern for any new model call. (The comment above `RESULT_SCHEMA`
mentioning `output_config.format` is stale — the code below it does not use it.)

**Scheduling is wall-clock aligned.** `_eval_trigger` maps the interval to a
`CronTrigger` where possible so runs land on clean local times (9:00, 10:00)
rather than drifting from container start. Multiples of 60 → top of the hour;
divisors of 60 → aligned marks; anything else falls back to a plain interval.
`TZ` must be set for "top of the hour" to mean local time rather than UTC.

**No authentication anywhere.** Every endpoint is open, including `/api/run-now`,
which triggers a paid model call. This is an accepted trade-off for a trusted
LAN and is documented as such in `docs/API.md` — but it means any new *write*
endpoint is a genuine change in posture, not a formality.

## Docs

- `README.md` — deployment, UDM Pro configuration, `.env` reference.
- `docs/API.md` — full JSON API reference and payload shapes. FastAPI also
  self-documents at `/docs`, `/redoc`, and `/openapi.json`.
- `docs/fleet-health-design.md` — **design only, nothing implemented.** A plan to
  extend this service into Raspberry Pi fleet health monitoring. Worth reading
  before touching `preprocess.py` or the purge path: it documents why the digest
  and purge paths block metric collection, and why dead-host detection can't ride
  on syslog at all. Its code landmarks are accurate as of this writing.
