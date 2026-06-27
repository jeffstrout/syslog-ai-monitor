# API Reference

The Syslog AI Monitor exposes a small JSON API plus the dashboard, served by
FastAPI on **port 8080**. All endpoints are **unauthenticated** and intended for
a trusted LAN. Base URL: `http://<pi-ip>:8080`.

> **Interactive docs are built in.** FastAPI auto-generates live, explorable
> documentation from the running service:
> - **Swagger UI:** `http://<pi-ip>:8080/docs`
> - **ReDoc:** `http://<pi-ip>:8080/redoc`
> - **OpenAPI schema (JSON):** `http://<pi-ip>:8080/openapi.json`
>
> The reference below mirrors those, with copy-paste examples.

---

## Endpoint summary

| Method | Path | Purpose |
|---|---|---|
| `GET`  | `/` | The dashboard (HTML) |
| `GET`  | `/api/health` | Liveness/health check (200 ok / 503 error) |
| `GET`  | `/api/status` | Buffered-log count + latest finding |
| `GET`  | `/api/latest` | The single most recent finding |
| `GET`  | `/api/history?limit=N` | Recent findings, newest first |
| `GET`  | `/api/weekly` | Latest weekly pattern review |
| `GET`  | `/api/weekly/history?limit=N` | Weekly reviews, newest first |
| `POST` | `/api/run-now` | Run an hourly evaluation immediately |
| `POST` | `/api/run-weekly` | Run the weekly pattern review immediately |
| `GET`  | `/docs`, `/redoc`, `/openapi.json` | Auto-generated API docs |

---

## Data shapes

### `finding`

A stored evaluation record (one per hour, kept for `RETENTION_DAYS`).

| Field | Type | Description |
|---|---|---|
| `id` | integer | Auto-increment row id |
| `ts` | number | Unix epoch (seconds) of the evaluation |
| `overall_status` | string | `ok` \| `warning` \| `error` |
| `summary` | string | One-line human summary of the period |
| `log_count` | integer | Raw log lines evaluated in this period |
| `payload` | object | The full structured AI result (below) |

### `payload`

The structured result returned by Claude.

| Field | Type | Description |
|---|---|---|
| `overall_status` | string | `ok` \| `warning` \| `error` |
| `summary` | string | One-line summary |
| `findings` | array of `issue` | Individual problems found (empty if healthy) |

### `issue`

| Field | Type | Description |
|---|---|---|
| `severity` | string | `info` \| `warning` \| `error` \| `critical` |
| `category` | string | e.g. `VPN Connectivity`, `WAN`, `Wireless` |
| `title` | string | Short title |
| `detail` | string | What is happening and why it matters |
| `evidence` | string | Representative log line(s) |
| `occurrences` | integer | Approx. number of related log lines |
| `recommendation` | string | Suggested action |

### `weekly_review`

A stored weekly pattern review (kept for `RETENTION_DAYS`).

| Field | Type | Description |
|---|---|---|
| `id` | integer | Auto-increment row id |
| `ts` | number | Unix epoch (seconds) the review ran |
| `period_start` | number | Epoch of the earliest finding reviewed |
| `period_end` | number | Epoch of the latest finding reviewed |
| `window_days` | integer | `WEEKLY_WINDOW_DAYS` used for this review |
| `finding_count` | integer | Hourly findings rolled up |
| `payload` | object | The structured review (below) |

### `weekly payload`

| Field | Type | Description |
|---|---|---|
| `period_status` | string | `ok` \| `watch` \| `action` |
| `overall_assessment` | string | 2–4 sentence summary of the period |
| `recurring_issues` | array of `recurring_issue` | Problems seen across many hours/days |
| `trends` | array of `trend` | What's new / increasing / steady / decreasing / resolved |
| `watchlist` | array of string | Things worth keeping an eye on |

### `recurring_issue`

| Field | Type | Description |
|---|---|---|
| `severity` | string | `info` \| `warning` \| `error` \| `critical` |
| `category` | string | e.g. `VPN Connectivity` |
| `title` | string | Short title |
| `days_seen` | integer | Distinct days the issue appeared |
| `frequency` | string | Human description, e.g. `every evening (~18:00)` |
| `detail` | string | What the pattern is |
| `recommendation` | string | Suggested action |

### `trend`

| Field | Type | Description |
|---|---|---|
| `title` | string | Short title |
| `direction` | string | `new` \| `increasing` \| `steady` \| `decreasing` \| `resolved` |
| `detail` | string | What changed over the period |

---

## Endpoints

### `GET /api/health`

Liveness/health check for uptime monitors. Returns `200` when the service and
database are reachable, `503` otherwise.

**Response (200)**

```json
{
  "status": "ok",
  "version": "1.0.0",
  "uptime_seconds": 3725,
  "buffered_logs": 842,
  "findings_stored": 17,
  "last_evaluation_ts": 1750896000.0
}
```

On failure: `503` with `{ "status": "error", "detail": "..." }`.

```bash
curl -s http://<pi-ip>:8080/api/health
```

---

### `GET /api/status`

Lightweight overview used by the dashboard header.

**Response**

```json
{
  "buffered_logs": 1342,
  "latest": {
    "id": 7,
    "ts": 1750896000.0,
    "overall_status": "error",
    "summary": "OpenVPN client stuck in a certificate-failure loop.",
    "log_count": 4985,
    "payload": { "overall_status": "error", "summary": "...", "findings": [ ... ] }
  }
}
```

`latest` is `null` until the first evaluation has run.

```bash
curl -s http://<pi-ip>:8080/api/status
```

---

### `GET /api/latest`

Returns the most recent `finding` object directly, or `null` if none exist yet.

```bash
curl -s http://<pi-ip>:8080/api/latest
```

---

### `GET /api/history`

Returns recent findings, newest first.

**Query params**

| Name | Type | Default | Description |
|---|---|---|---|
| `limit` | integer | `200` | Max findings to return |

**Response**

```json
{
  "findings": [
    {
      "id": 7,
      "ts": 1750896000.0,
      "overall_status": "error",
      "summary": "...",
      "log_count": 4985,
      "payload": {
        "overall_status": "error",
        "summary": "...",
        "findings": [
          {
            "severity": "critical",
            "category": "VPN Connectivity",
            "title": "OpenVPN client persistent connection failure",
            "detail": "TLS certificate verification is failing in a retry loop.",
            "evidence": "openvpn: ... SIGUSR1[soft,tls-error] received, process restarting",
            "occurrences": 28,
            "recommendation": "Re-import a fresh .ovpn profile and verify the CA bundle."
          }
        ]
      }
    }
  ]
}
```

```bash
curl -s "http://<pi-ip>:8080/api/history?limit=50"
```

---

### `GET /api/weekly`

The latest **weekly pattern review** — a rollup of the last `WEEKLY_WINDOW_DAYS`
(default 7) of findings highlighting recurring issues and trends. Returns the
object directly, or `null` if none exist yet.

**Response**

```json
{
  "id": 4,
  "ts": 1750896000.0,
  "period_start": 1750291200.0,
  "period_end": 1750896000.0,
  "window_days": 7,
  "finding_count": 142,
  "payload": {
    "period_status": "action",            // ok | watch | action
    "overall_assessment": "2–4 sentence summary of the period",
    "recurring_issues": [
      {
        "severity": "critical",           // info | warning | error | critical
        "category": "VPN Connectivity",
        "title": "OpenVPN client fails nightly",
        "days_seen": 7,
        "frequency": "every evening (~18:00)",
        "detail": "...",
        "recommendation": "..."
      }
    ],
    "trends": [
      { "title": "QUIC reassembly warnings",
        "direction": "increasing",        // new | increasing | steady | decreasing | resolved
        "detail": "..." }
    ],
    "watchlist": ["...", "..."]
  }
}
```

```bash
curl -s http://<pi-ip>:8080/api/weekly
```

---

### `GET /api/weekly/history`

Recent weekly reviews, newest first.

**Query params:** `limit` (integer, default `30`).

**Response:** `{ "weekly": [ <weekly review>, ... ] }` — each item matches the
`/api/weekly` shape.

```bash
curl -s "http://<pi-ip>:8080/api/weekly/history?limit=10"
```

---

### `POST /api/run-weekly`

Runs the weekly pattern review immediately instead of waiting for the daily
schedule (reads stored findings; does not touch raw logs). Accepts **GET or
POST**. Returns `{ "ran": true, "result": { ...review... } }`, or
`{ "ran": false, "result": null }` if there were no findings in the window or
the model call failed.

```bash
curl -X POST http://<pi-ip>:8080/api/run-weekly
```

---

### `POST /api/run-now`

Runs an evaluation immediately instead of waiting for the hourly schedule:
digests everything buffered, calls Claude, stores the finding, and purges the
evaluated raw logs.

**Response**

```json
{ "ran": true, "result": { "overall_status": "ok", "summary": "...", "findings": [] } }
```

- `ran` is `false` (and `result` is `null`) when there were no logs to evaluate,
  or the model call failed. On failure the raw logs are **kept** for the next run.
- Accepts **GET or POST** for convenience — a plain `curl` or a browser visit
  works, as does `curl -X POST`.

```bash
curl http://<pi-ip>:8080/api/run-now          # GET works
curl -X POST http://<pi-ip>:8080/api/run-now  # POST also works
```

---

## Notes

- **No authentication.** Anyone who can reach port 8080 can read findings and
  trigger `/api/run-now` or `/api/run-weekly`. Keep it on a trusted network, or
  front it with a reverse proxy if you need auth.
- **Timestamps** (`ts`) are Unix epoch seconds (UTC). The dashboard converts
  them to local time in the browser.
- **Retention.** Findings **and weekly reviews** older than `RETENTION_DAYS`
  (default 30) are purged by a nightly job; raw logs never persist beyond one
  evaluation.
