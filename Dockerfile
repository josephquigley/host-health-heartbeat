# host-health-heartbeat monitor image.
#
# Bakes the check script + entrypoint into python:alpine so a deploy can build
# straight from git (Docker build context = this repo at a tag) instead of
# bind-mounting code from the host. Stdlib only, so no pip step.
#
# Runtime: entrypoint.sh renders the crontab from env (cadence is configurable via
# .env — CRON_KERNEL / CRON_SECREBOOT), runs os_health.py once now (all categories,
# so health/pings report immediately on (re)start), then execs busybox crond. See
# DESIGN.md.
FROM python:3.12-alpine

WORKDIR /app
COPY os_health.py entrypoint.sh ./
RUN chmod +x entrypoint.sh

CMD ["sh", "/app/entrypoint.sh"]
