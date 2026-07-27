# Raspberry Pi Fleet Health Monitoring — Design Notes

**Status:** design only, nothing implemented.
**Date:** 2026-07-27
**Origin:** a Flip Board session that started as "the display appliance is slow"
and turned into "how do I health-check all my Pis." Conclusions are carried over
here so implementation can start from them rather than rediscovering them.

---

## Goal

Know the health of every Raspberry Pi in the house, and be told when one is in
trouble — ideally *before* it dies. Reuse the syslog-ai-monitor container pattern
rather than building something new.

---

## Fleet inventory

| Host | IP | Model | Role | Network |
|------|-----|-------|------|---------|
| `OfficeDisplay` | 192.168.0.17 | Pi 4B Rev 1.5 | split-flap display (Docker, GHCR + Watchtower) | **WiFi only** — 5 GHz ch149, ~-64 dBm, no Ethernet cable |
| `syslog` | 192.168.0.125 | Pi 4B 4 GB | this project — syslog receiver + hourly Claude eval | DHCP reservation (pinned so the UDM Pro SIEM target keeps working) |
| `HVACMonitor` | 192.168.0.69 | Pi 3B+ | `ac-monitor` — Sequent HAT, 1-Wire temps, MQTT | *unconfirmed* |

`HVACMonitor`'s IP and identity were inferred from open Terminal tabs plus the
`ac-monitor` repo description. **Confirm before relying on it.**

**`syslog` is the monitor node.** It is headless, always on, not latency
sensitive, already holds an Anthropic API key and SMTP config, and already runs
the scheduler/DB/alerting/dashboard this design wants to reuse. `OfficeDisplay`
is a poor choice — it drives an HDMI display and is on WiFi.

---

## What we know about how these Pis actually fail

From the prior Syslog deployment session, and it should drive the whole design:

> **Power is the recurring failure mode, not the SD card.** The original Pi 4B
> died after repeated undervoltage (powered from a USB hub — a Pi 4 needs a real
> 5 V/3 A USB-C supply) plus live power-pulls; it finally over-current-tripped a
> power bank and shorted the board.

Implications:

- Weight **power/undervoltage** far above SD-card wear. General Pi failure
  statistics say SD cards; *this fleet's* history says power.
- Blinking red PWR LED = undervoltage. Solid red = fine.
- Pi 4 has **no PoE without a HAT** — Ethernet does not power it.
- Always `sudo shutdown -h now` before unplugging. A small UPS is the real fix.

Verified on `OfficeDisplay` 2026-07-27: `/sys/class/hwmon/hwmon1/name = rpi_volt`
— the undervoltage sensor driver is present, so the kernel *does* log these
events and they can be forwarded.

---

## Can syslog alone do this? Partly.

### Works today, no code changes

- `app/syslog_listener.py:57` — `parse_line` already handles **RFC 3164** and
  **RFC 5424** in addition to CEF. Pi rsyslog speaks 3164/5424, so it parses
  cleanly with no parser work.
- `app/preprocess.py:73` — `build_digest` already counts by host and emits a
  `Source hosts: …` line. Multi-host ingest is built in.
- Kernel warnings/errors carry syslog severity ≤ 4, so they land in the
  **elevated-severity sample with real values intact** (`app/preprocess.py:87`).

So pointing rsyslog at `192.168.0.125:514` gets undervoltage events, thermal
throttling, mmc/ext4 errors, OOM kills, failed units, and auth events into a
pipeline that already works. **This is real value for very little effort.**

### Blocker 1 — `templatize()` destroys metrics

`app/preprocess.py:23` masks volatile tokens so repetitive firewall lines
collapse. Among the rules: `\b0x[0-9A-Fa-f]+\b → <HEX>` and `\b\d+\b → <N>`.

A vitals line:

```
host=officedisplay throttled=0x0 temp=39.4 load=0.11 disk_pct=14 signal=-64
```

becomes, before Claude ever sees it:

```
host=officedisplay throttled=<HEX> temp=<N>.<N> load=<N>.<N> disk_pct=<N> signal=-<N>
```

**Every measurement is masked.** The templatizer is correct for its purpose —
health metrics are simply the pathological case, because the numbers *are* the
payload. Metrics must not go through the digest path.

### Blocker 2 — raw logs are purged after each evaluation

`app/evaluator.py:57` calls `delete_logs_until(cutoff)` once a finding is stored.
Even unmasked, there would be no retained time series — and trend detection
("undervoltage count on this host has climbed for ten days") is the main reason
to collect metrics at all.

### Blocker 3 — syslog cannot detect a dead host

Syslog is fire-and-forget push. A healthy quiet Pi and a Pi that died at 3 a.m.
both send nothing; the two are indistinguishable.

This is the decisive gap. Given that **power failure is the known failure mode**,
the event that actually kills these Pis is exactly the one a push-only transport
structurally cannot report — the reporter dies with the box. Detecting it
requires a channel where *absence of traffic* is itself the signal.

### Also note

Today's actual Flip Board bug is an instructive partial case. WiFi power save
(`brcmf_cfg80211_set_power_mgmt: power save enabled`) *is* a kernel line and
would have been forwarded. But the -64 dBm signal measurement never would,
because nothing logs it. Syslog would have carried the clue, not the evidence.

---

## Proposed architecture

Three tiers. Tier 1 is nearly free; tier 2 is where the new code goes.

### Tier 1 — syslog forwarding (events)

Per Pi:

```bash
sudo apt install -y rsyslog
echo '*.* @192.168.0.125:514' | sudo tee /etc/rsyslog.d/90-forward.conf
sudo systemctl restart rsyslog
```

Note: current Raspberry Pi OS images are **journald-only** — `rsyslog` was
confirmed *not active* on `OfficeDisplay`, so this install step is required, it
is not already there.

Gets: undervoltage, thermal throttling, mmc/ext4 errors, filesystem
remount-read-only, OOM kills, failed systemd units, sshd/auth, cron.

Suggested tuning on the receiver: add a **per-host section** to the digest so one
chatty host cannot crowd the others out of `digest_max_templates` /
`digest_max_samples`.

### Tier 2 — vitals + heartbeat over HTTP (state)

Deliberately **not** syslog, for the three blockers above. Add an endpoint to the
existing FastAPI app; metrics arrive structured, over TCP, into their own table
with their own retention, untouched by `templatize()` and unaffected by the
raw-log purge.

```
POST /api/vitals        # shared-secret header; body below
```

Each Pi runs a small cron script every 5 minutes. Sketch:

```bash
#!/usr/bin/env bash
# /usr/local/bin/pi-vitals.sh  —  */5 * * * *
T=$(vcgencmd get_throttled | cut -d= -f2)
read -r L1 L5 L15 _ < /proc/loadavg
curl -fsS -m 10 -X POST http://192.168.0.125:8080/api/vitals \
  -H "Content-Type: application/json" \
  -H "X-Vitals-Token: $VITALS_TOKEN" \
  -d "$(cat <<EOF
{
  "host":       "$(hostname)",
  "boot_id":    "$(cat /proc/sys/kernel/random/boot_id)",
  "uptime_s":   $(cut -d. -f1 /proc/uptime),
  "throttled":  "$T",
  "temp_c":     $(vcgencmd measure_temp | tr -dc '0-9.'),
  "load1":      $L1,
  "mem_pct":    $(free | awk '/Mem:/{printf "%.1f", $3/$2*100}'),
  "swap_pct":   $(free | awk '/Swap:/{if($2>0) printf "%.1f", $3/$2*100; else print 0}'),
  "disk_pct":   $(df --output=pcent / | tail -1 | tr -dc '0-9'),
  "failed_units": $(systemctl list-units --state=failed --no-legend | wc -l),
  "model":      "$(tr -d '\0' < /proc/device-tree/model)"
}
EOF
)"
```

Add per-host extras where relevant: WiFi signal/power-save on `OfficeDisplay`,
`docker ps` container states on hosts running containers.

**`throttled` is the highest-value field.** Store the raw hex and decode the bits:

| Bit | Meaning |
|-----|---------|
| `0x1` | under-voltage **now** |
| `0x2` | ARM frequency capped **now** |
| `0x4` | currently throttled |
| `0x8` | soft temperature limit active **now** |
| `0x10000` | under-voltage **has occurred** since boot |
| `0x20000` | frequency capping has occurred |
| `0x40000` | throttling has occurred |
| `0x80000` | soft temperature limit has occurred |

The `0x1____` sticky bits are the ones that catch a marginal PSU or cable — they
accumulate even when the current state looks clean.

`boot_id` gives free **unexpected-reboot detection**: it changes only across a
reboot, so a change you didn't initiate means power loss or a crash.

**Dead-host detection:** a scheduled job flags any known host with no vitals row
in the last N intervals. This is the thing syslog cannot do, and given the fleet's
failure history it is arguably the single most important alert.

**Retention:** own table, own policy — keep 30–90 days (optionally downsampled)
so trends survive. Must *not* be swept by `purge_old_findings` /
`delete_logs_until`.

### Tier 3 — Claude interpretation

Reuse the existing weekly-review shape. `app/evaluator.py:74`'s
`_build_weekly_digest` builds a recurrence table (title → days seen / hours seen
/ occurrences); the same structure transfers directly, aggregating **metric
threshold-crossings per host** instead of finding titles. `run_weekly_review`
(`app/evaluator.py:136`) is the template for the job itself.

Keep the split that already works here: cheap deterministic collection and
threshold alerting, with the model doing interpretation over accumulated state —
"these three hosts all began logging undervoltage Tuesday afternoon; they share a
power strip."

---

## Why push, not SSH polling

An earlier version of this plan had the monitor node SSH into each Pi. Push is
better:

- No key distribution; no host needs **any** inbound access.
- The monitor node holding keys to the whole fleet is a real blast radius. Push
  removes it entirely.
- Firewall-friendly — outbound only.
- Adding a Pi is one cron line, not a key-distribution step.

SSH remains the right tool for *interactive* debugging (as in the session that
produced this doc), just not for the scheduled path.

**Note:** scheduled *cloud* agents cannot do this at all — they run outside the
LAN and cannot reach `192.168.0.x`. The collection loop must live on hardware
inside the network. This is a further argument for the existing always-on
container on `syslog` rather than a scheduled-agent approach.

---

## Existing-code landmarks

Worth reading before changing anything:

- `app/main.py:67` — **trap**: APScheduler runs sync jobs in a worker thread with
  no running event loop. Wrapping a job in a lambda that calls
  `asyncio.get_event_loop()` raises, and the job then *silently never runs*.
  This already caused commit `d8b3a2b` ("hourly evaluation never ran").
- `app/main.py:74` — `_eval_trigger` aligns runs to wall-clock times rather than
  container-start drift.
- `app/evaluator.py:46` — on a Claude failure, raw logs are deliberately **kept**
  so the window isn't lost. Preserve this property for any new job.
- `app/syslog_listener.py:31` — CEF severity is preferred over transport PRI,
  because UniFi's PRI is generic. Non-CEF lines (i.e. the Pis) fall through to
  normal PRI handling, which is what we want.

---

## Open questions

1. **Extend this repo, or fork it?** Extending reuses scheduler, SQLite,
   alerting, retention, dashboard, and Claude client — roughly 80% — at the cost
   of scope creep in a repo named for syslog. Forking keeps concerns clean but
   duplicates the skeleton. Leaning extend + rename to a homelab-monitor name,
   but it's a taste call.
2. **Confirm `HVACMonitor`** — IP and identity are inferred.
3. **Alert channel** — email/SMTP already exists. Telegram is also wired up on
   the Mac and may be a better fit for "tell me only when something's wrong."
4. **Vitals interval** — 5 min proposed. Dead-host threshold should be a small
   multiple (e.g. alert after 3 missed intervals) to tolerate UDP-free but still
   flaky WiFi on `OfficeDisplay`.
5. **Endpoint auth** — shared secret header is proposed. Fine on a trusted LAN;
   decide if more is wanted.
6. **`OfficeDisplay` baseline** — WiFi power save was disabled 2026-07-27 via
   `/etc/NetworkManager/conf.d/wifi-powersave-off.conf`. Its link is healthy but
   signal is only fair (~-64 dBm, 5 GHz penetrates walls poorly). Expect it to be
   the flakiest reporter of the three.

---

## Suggested first steps

1. Run `/init` — this repo has no `CLAUDE.md`, and the landmarks above are
   exactly the implicit knowledge that belongs in one.
2. Confirm the `HVACMonitor` host, and capture a `vcgencmd get_throttled`
   baseline from all three Pis before designing thresholds around them.
3. Tier 1 (rsyslog forwarding) first — it is cheap, independent of the rest, and
   immediately useful.
4. Then Tier 2, starting with the table + endpoint, then the collector script,
   then dead-host detection.
