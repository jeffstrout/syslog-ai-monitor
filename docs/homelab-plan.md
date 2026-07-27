# Homelab Fleet Management — Plan

**Status:** plan, nothing implemented.
**Date:** 2026-07-27
**Supersedes scope of:** `fleet-health-design.md` (which remains correct on the
syslog/metrics blockers; this doc decides the surrounding architecture).

Decisions ratified 2026-07-27:

1. **Complement Home Assistant** — HA owns live state; this project owns control
   and intelligence.
2. **Contract + shared Python library** — one appliance contract for all; a
   reusable package shared by the two Python apps.
3. **New `homelab-standards` repo** — single source of truth for the contract.

---

## Fleet inventory

| # | Host | IP | Model | OS | Repo | Agent-able |
|---|------|-----|-------|-----|------|-----------|
| 1 | Home Assistant | `.13`? | Pi 5 | HAOS | — | ❌ no shell/cron |
| 2 | Split Flap Display | `.17` | Pi 4B | Pi OS | `split-flap` | ✅ |
| 3 | HVAC Monitor | `.69` | Pi 5 | Pi OS | `ac-monitor` | ✅ |
| 4 | FlightAware | `.105`? | Pi 4B | PiAware (Debian) | — | ✅ likely |
| 5 | Syslog Monitor | **`.44`** | Pi 4B | Pi OS | `syslog-ai-monitor` | ✅ (manager) |

An ARP sweep found exactly five Raspberry Pi MACs — `.13`, `.17`, `.44`, `.69`,
`.105` — matching the five appliances. **`.13` and `.105` are inferred**; confirm
which is HA and which is FlightAware.

**`.44` is not `.125`.** The syslog Pi moved; `fleet-health-design.md` still
records `.125` as a reservation "pinned so the UDM Pro SIEM target keeps
working." **The UDM Pro is nonetheless still delivering** — verified 2026-07-27,
~37 lines/sec arriving at `.44` — so the export was not lost, only the pin. Re-pin
the reservation anyway so a future lease change doesn't break it.

---

## Architecture: two planes

The single most important decision. These do not overlap.

```
   ┌─────────────────── STATE PLANE — Home Assistant (.13) ──────────────────┐
   │  live telemetry · availability (MQTT LWT) · alerting · notifications    │
   └────────▲──────────────▲───────────────▲──────────────▲─────────────────┘
            │ MQTT         │ MQTT          │ MQTT         │ MQTT
        split-flap     ac-monitor      flightaware    syslog-monitor
            │              │               │              │
   ┌────────▼──────────────▼───────────────▼──────────────▼─────────────────┐
   │  CONTROL + INTELLIGENCE PLANE — Syslog Monitor (.44)                    │
   │  fleet inventory · config UI · version/rollout · log ingest · AI eval   │
   └────────────────────────────────────────────────────────────────────────┘
```

**Why not build state in the manager:** `ac-monitor/mqtt_out.py` already publishes
Home Assistant discovery *with LWT availability*. MQTT LWT is exactly the
"absence of traffic is the signal" dead-device detection that
`fleet-health-design.md` proves syslog structurally cannot do. That problem is
already solved — in the project that looked least finished. Rebuilding it in the
manager would duplicate working code and add a second thing to trust.

**What the manager uniquely owns** (HA does none of this well):

- **AI evaluation** — the hourly/weekly Claude pipeline, extended across all five
  hosts rather than just the UDM Pro. This is the differentiated capability.
- **Config management** — one UI to read/write each appliance's `/api/config`.
- **Version + rollout** — what commit each Pi runs, and whether it is stale
  against GHCR `:latest`.
- **Log ingest** — rsyslog from every host that has a shell.

---

## The appliance contract (v1)

Lives in `homelab-standards`. Every appliance conforms regardless of language.

### HTTP

| Endpoint | Returns |
|---|---|
| `GET /api/health` | `200` + `{status, version, uptime_seconds, …}` / `503` on failure |
| `GET /api/version` | `{commit, built_at}` — baked in by CI |
| `GET /api/state` | app-specific live state |
| `GET/PUT /api/config` | app-specific config; **secrets redacted on GET** |

Standardize on `/api/health` (split-flap and syslog already use it; ac-monitor's
`/healthz` moves).

### Deployment

- Multi-arch image at `ghcr.io/jeffstrout/<name>` — `:latest` plus `sha-<short>`.
- The Pi **pulls, never builds**.
- Watchtower with `WATCHTOWER_LABEL_ENABLE`, scoped by
  `com.centurylinklabs.watchtower.enable=true`.
- `restart: unless-stopped`; `/data` volume for config + state; `TZ` set.
- `APP_COMMIT` / `APP_BUILD_TIME` baked at build time.

### MQTT

- Discovery under `homeassistant/…` (ac-monitor's payload shape is the reference).
- State: `homelab/<appliance>/state`
- Availability: `homelab/<appliance>/availability`, **LWT `offline`** — mandatory,
  this is the fleet's dead-device detection.

### Config

`/data/config.yaml`, editable from the appliance's own web panel. Syslog Monitor
migrates off `.env` (which cannot survive an image swap or be edited from a UI).

---

## Shared library: `homelab_appliance`

Ships **inside** `homelab-standards` so the contract and its reference
implementation cannot drift:

```
homelab-standards/
├── README.md                      # the contract above
├── docs/
│   ├── deployment.md              # GHCR + Watchtower pattern
│   ├── mqtt.md                    # topic tree + discovery shapes
│   └── inventory.md               # the fleet table
└── packages/homelab-appliance/    # pip-installable
    └── homelab_appliance/
        ├── health.py              # /api/health + /api/version routers
        ├── version.py             # APP_COMMIT / APP_BUILD_TIME
        ├── config.py              # /data/config.yaml load/save/redact
        ├── mqtt.py                # discovery + LWT publisher
        └── vitals.py              # push client (tier 2)
```

Installed by pinned tag so a standards change never silently alters a running
appliance:

```dockerfile
RUN pip install "git+https://github.com/jeffstrout/homelab-standards@v1.0.0#subdirectory=packages/homelab-appliance"
```

**Flip Board stays Node** and implements the contract natively — it already has
`/api/health`, `/api/version`, GHCR, and Watchtower. React + WebSocket is the
right tool for a wall display; porting it would be a large rewrite of the most
polished project for no gain. The contract is the shared thing, not the language.

---

## Sequencing

### Phase 0 — Recover and de-risk the manager node ⚠️ blocking

The Pi at `.44` is currently wedged: ICMP replies and TCP handshakes complete,
but no userspace process responds (sshd never sends a banner). Kernel alive,
userspace dead — consistent with a read-only rootfs after SD errors, OOM, or an
I/O stall.

1. Console recovery via HDMI + keyboard. **Do not pull power** — this fleet's
   history is undervoltage and power-pulls killing a Pi 4B. Check `dmesg` for
   mmc/ext4 errors and whether `/` went read-only.
2. If the SD card is implicated, **rebuild on USB SSD**. Everything else will
   depend on this node.
3. Re-pin the DHCP reservation and fix the UDM Pro SIEM target.
4. A UPS for the manager node.

### Phase 1 — Ratify the standard

Create `homelab-standards`; write the contract, deployment, and MQTT docs; publish
`homelab_appliance` v1.0.0. Link it from each project's `CLAUDE.md`.

### Phase 2 — Bring Syslog Monitor up to standard

It is the furthest behind and about to become the most important:

- `docker-publish.yml` → GHCR multi-arch + Watchtower on the Pi (stop building on
  device).
- `/api/version`.
- MQTT publisher with LWT.
- Migrate `.env` → `/data/config.yaml`.
- A pytest suite — `ac-monitor/tests/` is the model. This establishes the
  convention `CLAUDE.md` currently records as absent.

### Phase 3 — Fold in the other two Python-adjacent appliances

- `ac-monitor`: adopt `homelab_appliance`, move `/healthz` → `/api/health`, fix
  the stale README ("design phase" with 1,678 lines of code; Pi 3B+ vs Pi 5).
- `split-flap`: add MQTT availability; confirm contract conformance.

### Phase 4 — Build the manager

1. **Vitals ingest** — tier 2 of `fleet-health-design.md`: `POST /api/vitals`,
   own table, own retention, bypassing `templatize()` and the raw-log purge.
2. **Host registry** — an explicit table, not auto-registration on first POST
   (an auto-registered decommissioned host alerts forever). Resolves open
   question 1 of the fleet-health doc.
3. **Fleet dashboard** — per-host vitals, running commit vs GHCR `:latest`.
4. **Config proxy** — read/write each appliance's `/api/config`. **This is the
   first genuine write path across hosts**; the "no authentication anywhere"
   posture documented in `docs/API.md` must change before it ships.
5. **Extend the AI evaluation** to all hosts, with the per-host digest sectioning
   the fleet-health doc recommends so one chatty host cannot crowd out the others.
6. **Back up each appliance's `/data`.** Every appliance keeps state that exists
   in exactly one place — an SD card. Nothing in git covers it, because it is
   deliberately gitignored:

   | Appliance | Unique state at risk |
   |---|---|
   | syslog-ai-monitor | 495 findings + 22 weekly reviews (30 days of history) |
   | ac-monitor | `config.yaml` — **thermistor calibration**, earned with an ice bath and a kettle |
   | split-flap | persisted mode/theme/settings |

   GitHub is replication of *committed* history, not backup: it holds nothing
   that is uncommitted, gitignored, or outside a repo — which is precisely what
   `/data` and every `.env` are. A scheduled pull of each appliance's volume to
   the manager node (and from there into whatever backs up the manager) closes
   the gap. Secrets stay in a password manager, not in a repo.

### Phase 5 — Onboard the appliance-OS hosts

Read-only first.

- **Home Assistant (HAOS)** — no shell or cron, so no agent. Integrate via its
  REST/WebSocket API with a long-lived token. HA is a **peer**, not a target.
- **FlightAware (PiAware)** — Debian-based with systemd and SSH, so rsyslog
  forwarding and a vitals cron should both work; closer to a normal Pi than HAOS.

Both need confirming on-device rather than assuming.

---

## Standing risk: the manager cannot monitor itself

Today's outage is the proof — and it is worse than the fleet-health doc's
Blocker 3. A **ping-based check would have reported this Pi healthy**, because
ICMP is answered by the kernel, which is the part that still works. Only a
userspace push (vitals, or MQTT LWT) detects it.

Two mitigations, both required:

1. **Mutual watch** — HA and Syslog Monitor each publish availability and each
   alert on the other's absence. Both are always-on; neither can report its own
   death.
2. **Off-box dead-man's switch** — something outside the house confirms the pair
   is alive, in case both go (power cut, WAN loss).
