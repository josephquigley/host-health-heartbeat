#!/usr/bin/env python3
"""os-health: report host OS health via the Docker healthcheck and per-category
dead-man's-switch heartbeat pings. Stdlib only. See DESIGN.md.

Three independent host-level checks — pending security updates / reboot-required /
kernel errors — each reporting to its OWN heartbeat monitor (so an alert names the
failing category). Two crond codepaths on different cadences: KERNEL runs every
30 min (OOM kills / panics must be caught fast), SECURITY + REBOOT run daily
(they only move on apt / unattended-upgrade timescales). `main()` runs the
categories named on argv, or all of them when none are given (the once-on-start
run). Disk is intentionally not checked here: host-metrics tools cover disk.

The Docker health-file merges the *last* status of every category across both
codepaths, so a kernel-only run never masks a stale security/reboot failure.

Failures are signals only: read-only host access, never restarts anything, never
raises out of main()."""
import os, sys, re, json, time, urllib.request

# ---- host mount points (all read-only) ----
UPDATES_FILE = os.environ.get("UPDATES_FILE", "/host/update-notifier/updates-available")
REBOOT_FILE = os.environ.get("REBOOT_FILE", "/host/run/reboot-required")
LOG_DIR = os.environ.get("LOG_DIR", "/host/log")
STATE_FILE = os.environ.get("STATE_FILE", "/state/log-offsets.json")
HEALTH_FILE = os.environ.get("HEALTH_FILE", "/tmp/os-health")

# ---- per-category last status; persisted in the state volume so the health-file
# reflects EVERY category, not just the ones a given codepath's run checked ----
CATEGORY_STATUS_FILE = os.environ.get("CATEGORY_STATUS_FILE", "/state/category-status.json")

# ---- auto-update liveness (SECURITY gates on this, not on mere availability) ----
AUTOUPDATE_STAMP = os.environ.get("AUTOUPDATE_STAMP", "/host/apt-periodic/unattended-upgrades-stamp")
AUTOUPDATE_MAX_AGE = int(os.environ.get("AUTOUPDATE_MAX_AGE", "172800"))  # 2 days

# ---- reboot grace (a host that reboots on a weekly window carries the flag for days) ----
# 0 = no grace: any reboot-required flag fails immediately (default).
REBOOT_MAX_AGE = int(os.environ.get("REBOOT_MAX_AGE_DAYS", "0")) * 86400

# ---- per-category heartbeat ping URLs (env; empty disables that category's ping) ----
PING_URLS = {
    "SECURITY": os.environ.get("HC_PING_URL_SECURITY", "").rstrip("/"),
    "REBOOT":   os.environ.get("HC_PING_URL_REBOOT", "").rstrip("/"),
    "KERNEL":   os.environ.get("HC_PING_URL_KERNEL", "").rstrip("/"),
}

LOG_FILES = ["kern.log", "syslog"]
ERROR_PATTERN = re.compile(
    r"out of memory|oom-killer|killed process|kernel panic|BUG:|EXT4-fs error|I/O error",
    re.IGNORECASE,
)


# ---------- security updates ----------
def parse_security_count(text):
    """Pending standard security updates from update-notifier's
    updates-available text (0 if the line is absent)."""
    m = re.search(r"(\d+)\s+of these updates are standard security updates", text, re.IGNORECASE)
    return int(m.group(1)) if m else 0


# ---------- kernel error log scanning ----------
def _read_matches(path, start, pattern):
    """Read path[start:EOF] in binary (so offsets are byte positions, consistent
    with st_size); return (matched_lines, new_offset)."""
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
        end = f.tell()
    text = data.decode("utf-8", "replace")
    return [ln for ln in text.splitlines() if pattern.search(ln)], end


def scan_new_bytes(path, file_state, pattern):
    """Scan bytes appended to `path` since file_state {inode, offset}.
    Returns (matches, new_state).

    file_state None -> first sight: seed to EOF, no historical scan.

    On a logrotate rename the live file's inode changes and the bytes we had
    not yet scanned moved to `<path>.1`. We scan that file's tail from the old
    offset -- but only when its inode matches the one we recorded, so we never
    rescan an unrelated file or re-report already-scanned bytes -- then scan the
    new file from 0. This closes the rotation window without duplicates.

    A same-inode shrink (copytruncate) rescans the current file from 0."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return [], file_state  # file may appear later; keep prior state
    inode, size = st.st_ino, st.st_size
    if file_state is None:
        return [], {"inode": inode, "offset": size}
    old_inode = file_state.get("inode")
    old_offset = file_state.get("offset", 0)
    matches = []
    if inode != old_inode:
        rpath = path + ".1"
        try:
            rst = os.stat(rpath)
            if rst.st_ino == old_inode and rst.st_size >= old_offset:
                tail, _ = _read_matches(rpath, old_offset, pattern)
                matches += tail
        except FileNotFoundError:
            pass
        start = 0
    elif size < old_offset:
        start = 0
    else:
        start = old_offset
    new_matches, new_offset = _read_matches(path, start, pattern)
    matches += new_matches
    return matches, {"inode": inode, "offset": new_offset}


def scan_logs(log_dir, log_files, state, pattern):
    """Scan each log file for new error lines since last run. Returns
    (matches, new_state). new_state is keyed by file basename."""
    new_state = dict(state)
    all_matches = []
    for name in log_files:
        path = os.path.join(log_dir, name)
        matches, fstate = scan_new_bytes(path, state.get(name), pattern)
        if fstate is not None:
            new_state[name] = fstate
        all_matches += [f"{name}: {m}" for m in matches]
    return all_matches, new_state


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path, state):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


# ---------- reporting ----------
def write_health(path, failed):
    """Rewrite the health-file the Docker HEALTHCHECK reads: '<ok|fail> <epoch>'."""
    with open(path, "w") as f:
        f.write(f"{'fail' if failed else 'ok'} {int(time.time())}\n")


def ping(url, suffix="", body=""):
    """Best-effort heartbeat ping. Never raises: monitoring must not be able to
    break the run. No-op when `url` is empty."""
    if not url:
        return
    full = url + suffix
    try:
        data = body.encode("utf8", "ignore")[:100_000] if body else None
        urllib.request.urlopen(urllib.request.Request(full, data=data, method="POST"), timeout=10).close()
    except Exception as e:
        print(f"  ping {suffix or '/'} failed: {type(e).__name__}: {e}", file=sys.stderr)


# ---------- checks: each returns (ok, fails, info) ----------
def _autoupdate_age(path):
    """Seconds since the daily auto-upgrade last ran (stamp mtime), or None if the
    stamp is absent (auto-update never ran / not installed)."""
    try:
        return time.time() - os.stat(path).st_mtime
    except FileNotFoundError:
        return None


def _fmt_age(age):
    if age is None:
        return "never"
    hours = age / 3600
    return f"{hours:.0f}h ago" if hours < 48 else f"{hours / 24:.0f}d ago"


def check_security(updates_file=None, stamp=None, max_age=None):
    """Pending security updates FAIL only when auto-update has demonstrably stopped
    applying them: count>0 AND the unattended-upgrades run-stamp is older than
    max_age (or missing). A fresh stamp means any pending update is simply new or
    Ubuntu-phased -> OK. Decouples the alert from mere availability."""
    updates_file = updates_file or UPDATES_FILE
    stamp = stamp or AUTOUPDATE_STAMP
    max_age = AUTOUPDATE_MAX_AGE if max_age is None else max_age
    with open(updates_file) as f:
        security = parse_security_count(f.read())
    if security == 0:
        return True, [], ["security: 0 pending"]
    age = _autoupdate_age(stamp)
    if age is not None and age <= max_age:
        return True, [], [f"security: {security} pending, auto-update ran {_fmt_age(age)} (ok)"]
    reason = ("auto-update never ran / not installed" if age is None
              else f"auto-update last ran {_fmt_age(age)} (> {max_age // 86400}d)")
    return (False,
            [f"{security} pending security update(s); {reason}"],
            [f"security: {security} pending, {reason}"])


def check_reboot(reboot_file=None, max_age=None):
    """A pending reboot FAILS once the flag is older than max_age. With max_age 0
    (the default) any flag fails at once; on a host that reboots on a weekly
    window, set REBOOT_MAX_AGE_DAYS past that window so the alert means the
    scheduled reboot did not happen, not merely that one is due."""
    reboot_file = REBOOT_FILE if reboot_file is None else reboot_file
    max_age = REBOOT_MAX_AGE if max_age is None else max_age
    if not os.path.exists(reboot_file):
        return True, [], ["reboot: not required"]
    pkgs = ""
    try:
        with open(reboot_file + ".pkgs") as f:
            names = sorted(set(f.read().split()))
        if names:
            pkgs = " (" + ", ".join(names) + ")"
    except OSError:
        pass
    try:
        age = time.time() - os.stat(reboot_file).st_mtime
    except OSError:
        age = None
    if max_age and age is not None and age <= max_age:
        return (True, [],
                [f"reboot: required {_fmt_age(age)}, within {max_age // 86400}d grace"])
    since = f" since {_fmt_age(age)}" if age is not None else ""
    over = f" (> {max_age // 86400}d)" if max_age else ""
    return (False,
            [f"reboot required{pkgs}{since}{over}"],
            [f"reboot: required{since}{over}"])


def check_kernel():
    state = load_state(STATE_FILE)
    matches, new_state = scan_logs(LOG_DIR, LOG_FILES, state, ERROR_PATTERN)
    save_state(STATE_FILE, new_state)
    if matches:
        return False, list(matches), [f"kernel: {len(matches)} new error line(s)"]
    return True, [], ["kernel: clean"]


CHECKS = [("SECURITY", check_security), ("REBOOT", check_reboot), ("KERNEL", check_kernel)]


def selected(argv):
    """Categories to run: those named on argv (case-insensitive), or all of them
    when argv is empty (the once-on-(re)start run). Unknown names are fatal so a
    typo in the crontab fails loudly instead of silently checking nothing."""
    valid = [n for n, _ in CHECKS]
    if not argv:
        return valid
    want = {a.upper() for a in argv}
    unknown = sorted(want - set(valid))
    if unknown:
        sys.exit(f"unknown categor(ies) {', '.join(unknown)}; valid: {', '.join(valid)}")
    return [n for n in valid if n in want]  # canonical order


def overall_failed(status):
    """True if ANY known category's last recorded status is 'fail'. A category
    absent from `status` (never run yet) is treated as ok."""
    return any(status.get(n) == "fail" for n, _ in CHECKS)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    names = selected(argv)
    check_map = dict(CHECKS)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} os-health starting [{', '.join(names)}]")
    status = load_state(CATEGORY_STATUS_FILE)  # {category: "ok"|"fail"}, persisted
    ran_failed = 0
    for name in names:
        try:
            ok, fails, info = check_map[name]()
        except Exception as e:
            ok, fails, info = False, [f"{name} check errored: {type(e).__name__}: {e}"], []
        # human/log output -> container stdout/stderr (PID 1 -> docker logs)
        for line in info:
            print(f"  {line}")
        for fmsg in fails:
            print(f"  {name} FAIL: {fmsg}", file=sys.stderr)
        # per-category heartbeat ping; body is this category's own detail
        body = "\n".join(list(info) + [f"{name} FAIL: {m}" for m in fails])
        ping(PING_URLS.get(name, ""), "" if ok else "/fail", body)
        status[name] = "ok" if ok else "fail"
        if not ok:
            ran_failed += 1
    save_state(CATEGORY_STATUS_FILE, status)
    # Docker HEALTHCHECK: fail if ANY category's LAST status is fail,
    # merging the categories this run checked with the others' persisted status.
    write_health(HEALTH_FILE, overall_failed(status))
    summary = " | ".join(f"{n} {status.get(n, '?')}" for n, _ in CHECKS)
    print(f"{stamp} os-health done: {summary}")
    sys.exit(1 if ran_failed else 0)


if __name__ == "__main__":
    main()
