# host-health-heartbeat

Host-health monitor for Ubuntu servers, packaged as a cron-only container (busybox
`crond`). It runs `os_health.py` on two cadences — KERNEL every 30 min, SECURITY +
REBOOT daily at 06:00 UTC — and checks three host conditions, each pinging its own
heartbeat URL (a per-category dead-man's switch). Any service that follows the
Healthchecks.io `/ping/<token>` + `/fail` convention works. Disk usage is
deliberately *not* checked; leave that to your host-metrics tool.

| Category | Fails when | `.env` var |
|---|---|---|
| security | pending standard security updates > 0 **and** unattended-upgrades has stalled | `HC_PING_URL_SECURITY` |
| reboot | `/run/reboot-required` exists (older than `REBOOT_MAX_AGE_DAYS`) | `HC_PING_URL_REBOOT` |
| kernel | new OOM / panic / EXT4 / I-O errors in `kern.log`/`syslog` | `HC_PING_URL_KERNEL` |

Each run also rewrites `/tmp/os-health` (`ok|fail <epoch>`), which the Docker
`HEALTHCHECK` reads — so any dashboard that shows container health marks the
container red if any category fails.

## Files

- `os_health.py` — the check script (Python stdlib only).
- `entrypoint.sh` — renders the crontab from `CRON_*` env at start, then execs crond.
- `.env` — the three `HC_PING_URL_*` heartbeat URLs + optional `CRON_KERNEL` /
  `CRON_SECREBOOT` cadence overrides and `REBOOT_MAX_AGE_DAYS` (never committed; see
  `.env.example`). Defaults: KERNEL every 30 min, SECURITY+REBOOT daily 06:00 UTC, no
  reboot grace.
- `compose.yml` — `python:3.12-alpine`; mounts `/run`, `/var/log`,
  `/var/lib/update-notifier`, `/var/lib/apt/periodic` (read-only) + a state volume;
  `oom_score_adj: -500` so the monitor survives host memory pressure.

## Deploy

1. Create three heartbeat monitors (see `DESIGN.md` for the period/grace each needs).
2. On the host, copy `compose.yml` and `.env.example` into a directory and rename
   `.env.example` to `.env`. Fill in the three ping URLs.
3. Either build from a checkout (`build: .`), or build straight from this repo at a
   pinned tag with no local checkout:

   ```yaml
   build:
     context: https://github.com/josephquigley/host-health-heartbeat.git#v0.1.7
   ```

4. `docker compose up -d --build`, then `docker compose logs` to see the first run.

Full design in `DESIGN.md`.

## Development

```sh
PYTHONPATH=. python -m unittest discover -s tests -p 'test_*.py' -v
```

CI runs the tests on pull requests and pushes to `main`. Each push to `main` also
creates the next patch tag (`vX.Y.Z+1`) as a GitHub release. For a minor or major
bump, push the tag by hand once; the next auto-bump follows it.
