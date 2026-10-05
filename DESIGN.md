# host-health-heartbeat — design

**Target host:** Ubuntu LTS (uses `update-notifier`, `unattended-upgrades` and
`/run/reboot-required`), Docker with Compose v2+.
**Container:** `os-health`

## Purpose

A single, self-contained container that reports the **health of the host OS** to two
places at once:

- **A Docker-health-aware dashboard** (for example Beszel, Portainer or `docker ps`) — via the
  container's Docker `HEALTHCHECK` status. Fails if **any** category fails.
- **A heartbeat service** (Healthchecks.io, or any self-hosted service with the same
  `/ping/<token>` + `/fail` convention) — via **per-category dead-man's-switch pings**.
  Each category has its own heartbeat monitor, so an alert names the exact failing
  condition (and a dead container/host trips all three on missed heartbeats).

It monitors three host-level conditions:

1. **Pending standard security updates**
2. **Reboot required** (kernel/libc updated)
3. **Linux OOM kills and other critical kernel errors**

> **Disk is intentionally NOT checked here.** Host-metrics tools already monitor disk usage
> and alert on it, so a disk check would be redundant.

Failures are **signals only**. The container is read-only against the host, never
restarts anything, and an unhealthy status does not take down the container, Docker, or
the host.

## Architecture

- Base image `python:3.12-alpine`.
- busybox `crond` is PID 1. `entrypoint.sh` renders the crontab from the
  environment at start (schedule from `.env`, job commands fixed), runs the script
  once with no args (all categories, so health/pings report immediately instead of
  waiting for the first tick), then `exec`s `crond`. Two codepaths on different
  cadences: `os_health.py KERNEL` **every 30 min** (`CRON_KERNEL`, default
  `*/30 * * * *`) and `os_health.py SECURITY REBOOT` **daily** (`CRON_SECREBOOT`,
  default `0 6 * * *`, 06:00 UTC). Kernel needs a fast heartbeat (OOM/panic);
  security/reboot only move on apt timescales, so daily is plenty. Cadence lives in
  `.env` so it can be retuned with `compose up -d`, no rebuild. (Do NOT use a
  day-of-month step like `*/2`: busybox crond reads `*/2` as EVEN days while some
  heartbeat services' cron parsers read it as ODD days, so they disagree and the
  monitor false-alarms on the off-parity days.)
- One script, `os_health.py`, standard library only (no pip deps). `main()` runs
  the categories named on argv (or all when none are given).
- Default bridge network — the only outbound need is HTTPS to the heartbeat service.
- `mem_limit: 128m` and `oom_score_adj: -500` so the OOM monitor can never itself become
  the OOM culprit.

### Files

| File | Purpose |
|---|---|
| `compose.yml` | service definition, mounts, healthcheck |
| `Dockerfile` | bakes `os_health.py` + `entrypoint.sh` into the image |
| `os_health.py` | the check + report script (stdlib only) |
| `entrypoint.sh` | renders the crontab from `CRON_*` env, runs once, execs crond |
| `.env` | the three per-category ping URLs + optional tunables (secret, never committed) |
| `DESIGN.md` | this document |

A named volume holds the kernel-log scan state (byte offsets), so it survives restarts.

### Read-only host mounts

All mounts are `:ro`. The container only reads files.

| Mount (host → container) | Used by |
|---|---|
| `/var/lib/update-notifier → /host/update-notifier:ro` | security check (parses `updates-available`) |
| `/var/lib/apt/periodic → /host/apt-periodic:ro` | security check (auto-update run-stamp mtime = liveness) |
| `/run → /host/run:ro` | reboot check (`reboot-required` presence + `.pkgs`) |
| `/var/log → /host/log:ro` | kernel-errors check (`kern.log`, `syslog`, and their rotated `.1` tail) |

## The three checks

Each check is isolated in its own `try/except`. A check that throws (e.g. a source file
it can't read) is treated as **fail** with a message — it never aborts the other checks.

### 1. Security updates

Parses `/host/update-notifier/updates-available` for `N of these updates are standard
security updates`. Pending updates are **not** a failure by themselves — the host is
expected to run `unattended-upgrades`, so newly-published (or Ubuntu-phased) updates
normally clear on the next daily run. **FAIL** only when `N > 0` **and** auto-update has
stalled: the `unattended-upgrades` run-stamp (`/host/apt-periodic/unattended-upgrades-stamp`,
whose mtime advances on every daily run whether or not anything is installed) is older than
`AUTOUPDATE_MAX_AGE` (default 2 days), or the stamp is missing (not installed / never ran).
This decouples the alert from mere availability: it fires when auto-update can't keep up, not
when an update is simply new. Not detected: auto-update running daily but a *specific* package
install repeatedly failing (stamp stays fresh) — out of scope. ESM updates and the general
(non-security) count are not checked.

### 2. Reboot required

**FAIL** when `/host/run/reboot-required` exists (kernel/libc updated) **and** the flag is
older than `REBOOT_MAX_AGE_DAYS`. With the default `0` there is no grace: any flag fails at
once, which is right on a host rebooted by hand as soon as one is due. A host that reboots on
a *scheduled window* (e.g. a weekly `reboot-if-required` timer) carries the flag for days by
design; set `REBOOT_MAX_AGE_DAYS` past that window (weekly window -> `9`) so the alert means
the scheduled reboot did not happen, rather than firing every cycle. Inside the grace the
check reports ok with the flag's age. If the companion `reboot-required.pkgs` file is present,
the triggering package names are included in the failure message and ping body.

### 3. Kernel errors (since last run)

Scans newly-appended lines of `/host/log/kern.log` and `/host/log/syslog` for:

```
out of memory | oom-killer | killed process | kernel panic | BUG: | EXT4-fs error | I/O error
```

(case-insensitive). Any match → **FAIL**, with the matching lines included in the report.

**"Since last run"** uses a state file (in the named volume) recording each log file's
`(inode, byte-offset)`. Each run scans only bytes appended since last time. **First run
after deploy** sets offsets to current EOF and reports OK, so historical events don't
flood on day one.

**Rotation handling.** Ubuntu's default logrotate config rotates `kern.log`/`syslog` by
*rename* + rsyslog HUP (the live file is renamed to `<name>.1`; a fresh file takes its
place — `delaycompress` keeps `.1` uncompressed and readable). On a run, if the live
file's inode no longer matches the recorded one, the bytes we hadn't yet scanned are now
in `<name>.1`: we scan that file's tail **from the old offset** (guarded by an inode match
so we never rescan an unrelated file or re-report already-scanned bytes), then scan the
new live file from 0. This closes the window where an OOM/panic lands in the tail just
before a rotation. A same-inode shrink (copytruncate) rescans from 0.

## Reporting flow (per run)

For each category in turn:

1. Run the check → `(ok, fails, info)`.
2. Print its `info`/`FAIL` lines to stdout/stderr (→ `docker logs`).
3. Ping **that category's own URL**: bare on success, `/fail` on failure, with the
   category's own detail lines as the request body (capped ~100 KB).

After the run: write the health-file `/tmp/os-health` = `"<ok|fail> <epoch>"` where
`fail` means *any* category's **last** status is fail (this is what the Docker
healthcheck reads). Because the two codepaths run on different cadences, each run merges
the categories it just checked with the others' persisted status
(`/state/category-status.json`), so a kernel-only run never masks a stale security/reboot
failure. Exit non-zero if any category *this run* checked failed; `crond` keeps running
regardless.

Each per-category URL is **optional**: if its env var is empty, that ping is skipped
(best-effort, never raises) while the others and the Docker healthcheck still work.

## Docker healthcheck

```
test: pgrep crond present
      AND /tmp/os-health modified within last 60m (find -mmin -60)
      AND /tmp/os-health starts with "ok "
interval: 1h   timeout: 10s   retries: 3   start_period: 2m
```

The 60-minute window tracks the **30-min** KERNEL codepath (the fast one): a live
scheduler rewrites the file every 30 min, so staleness past 60 min means crond
died or the script hung. If you slow `CRON_KERNEL` down, widen the window to match.

Healthy = scheduler alive **AND** the last run was recent **AND** all categories passed.
Stale (cron died / script hung) or any `fail` result turns the container unhealthy.

## Why failures are safe ("fail switch that doesn't take anything down")

- All host mounts are **read-only**; the container performs no host mutations.
- A tripped check sets the health-file to `fail` and sends a `/fail` ping — a *status*.
- Docker does **not** restart a container for being unhealthy; `restart: unless-stopped`
  only acts on an actual process exit. An unhealthy monitor simply sits red until the
  condition clears.
- Even an unhandled exception is caught, and a failed start run still leaves `crond`
  running (`entrypoint.sh` uses `|| true`).
- `mem_limit: 128m` + `oom_score_adj: -500` bound and protect the monitor itself.

## Heartbeat monitor setup (per category)

Three heartbeat monitors, one per category. **Each monitor's period + grace MUST match the
codepath that pings it** — a mismatch (e.g. a 40-min tolerance against a daily emitter)
is a false-alarm generator:

| Category | env var | expected schedule |
|---|---|---|
| kernel | `HC_PING_URL_KERNEL` | period **30 min + grace 10 min** |
| security | `HC_PING_URL_SECURITY` | Cron `0 6 * * *` (UTC) / grace ~3h (period 24h) |
| reboot | `HC_PING_URL_REBOOT` | Cron `0 6 * * *` (UTC) / grace ~3h (period 24h) |

Each monitor receives a ping every time its codepath runs (bare = ok, `/fail` = problem),
so it goes down on a real failure *and* on a missed heartbeat (monitor/host dead).

## Tunables (`.env`)

| Var | Default | Meaning |
|---|---|---|
| `HC_PING_URL_SECURITY` | (empty) | ping URL for the security-updates check |
| `HC_PING_URL_REBOOT` | (empty) | ping URL for the reboot-required check |
| `HC_PING_URL_KERNEL` | (empty) | ping URL for the kernel-errors check |
| `CRON_KERNEL` | `*/30 * * * *` | schedule for the KERNEL codepath (rendered into the crontab at start) |
| `CRON_SECREBOOT` | `0 6 * * *` | schedule for the SECURITY+REBOOT codepath; keep it in sync with those monitors' period/grace, and never use a day-of-month `*/2` step (busybox parity bug) |
| `REBOOT_MAX_AGE_DAYS` | `0` | days a pending reboot may stay unactioned before REBOOT fails; `0` = fail immediately. Set past the host's scheduled reboot window (weekly -> `9`) |
| `AUTOUPDATE_MAX_AGE` | `172800` | seconds the unattended-upgrades run-stamp may age before pending security updates count as a failure |
| `HEALTH_FILE` | `/tmp/os-health` | health-file path (matches healthcheck) |

## Out of scope (YAGNI)

- No HTTP server (dashboards read the Docker healthcheck instead).
- No disk check (host-metrics tools cover disk natively).
- No general/ESM update or apt-staleness checks (the OS handles apt refresh + security auto-install).
- No host-side cron or helper (everything runs in the container off read-only mounts).
