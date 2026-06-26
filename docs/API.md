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
| `POST` | `/api/run-now` | Run an evaluation immediately |
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
  trigger `/api/run-now`. Keep it on a trusted network, or front it with a
  reverse proxy if you need auth.
- **Timestamps** (`ts`) are Unix epoch seconds (UTC). The dashboard converts
  them to local time in the browser.
- **Retention.** Findings older than `RETENTION_DAYS` (default 30) are purged by
  a nightly job; raw logs never persist beyond one evaluation.
