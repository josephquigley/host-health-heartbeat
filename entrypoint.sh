#!/bin/sh
# Container entrypoint: render the busybox crontab from env, then hand off to crond.
#
# The SCHEDULE (the 5 cron fields) lives in the environment so it can be retuned
# from .env with `docker compose up -d` — no image rebuild/retag. The job COMMANDS
# stay fixed in this file; only cadence is configurable. Defaults below are the
# source of truth when the vars are unset, and equal the shipped schedule.
#
# Two codepaths on different cadences (see DESIGN.md):
#   CRON_KERNEL      — KERNEL check; fast heartbeat for OOM/panic. Default 30 min.
#   CRON_SECREBOOT   — SECURITY + REBOOT; move on apt timescales. Default daily 06:00 UTC.
#
# WARNING — keep each schedule in lockstep with its heartbeat monitor's period+grace
# (a mismatch produces false alarms), and NEVER use a day-of-month step like
# `*/2`: busybox crond reads `*/2` as EVEN days (2,4,6,...) while some heartbeat
# services' cron parsers read it as ODD days (1,3,5,...). They disagree
# and the monitor false-alarms DOWN on the off-parity days. Use plain daily/hourly.
set -e

: "${CRON_KERNEL:=*/30 * * * *}"
: "${CRON_SECREBOOT:=0 6 * * *}"

# Install the crontab for crond to read. Schedule from env, commands fixed.
{
	printf '%s cd /app && python os_health.py KERNEL\n'          "$CRON_KERNEL"
	printf '%s cd /app && python os_health.py SECURITY REBOOT\n' "$CRON_SECREBOOT"
} | crontab -

echo "os-health: KERNEL='$CRON_KERNEL'  SECURITY+REBOOT='$CRON_SECREBOOT'"

# Run once now (all categories) so health/pings report immediately instead of
# waiting for the first tick. A failing start run must NOT stop crond, so `|| true`.
(cd /app && python os_health.py) || true

# crond becomes PID 1 (exec) so signals/logs go straight to Docker.
exec crond -f -l 8
