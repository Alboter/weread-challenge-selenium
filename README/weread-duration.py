#!/usr/bin/env python3
# Script D -- daily random WEREAD_DURATION generator for weread-challenge.
#   Name   : /usr/local/bin/weread-duration.py
#   Caller : Script C /etc/systemd/system/weread-boot-start.service
#            ExecStartPre: roll, wait   /   ExecStart: launch
#   Compose: /opt/stacks/weread-challenge/compose.yaml
#   State  : /var/lib/weread-duration/plan.json
#   Backup : /var/lib/weread-duration/compose.before.yaml
#   Log    : /var/log/weread-duration.log
#
#   Rule   : once per calendar day (Asia/Shanghai), draw a truncated-normal
#            INTEGER in [62,120] minutes whose MODE (most frequent value) is
#            exactly 73, then atomically rewrite the WEREAD_DURATION line.
#            73 is NOT the mean: mu is set directly to 73 and sigma=12min is
#            the jitter. Since mu lies inside [62,120], truncation cannot move
#            the mode; it only pushes the mean a few minutes higher (~76.8).
#   Why    : CT 107 is stopped at 00:00 by Script A and started somewhere in
#            08:00-08:30 by Script B. A 06:00 cron INSIDE CT 107 (the old
#            weread-daily.py approach) can never fire. Boot time is the only
#            reliable once-per-day point, and running as Script C's
#            ExecStartPre guarantees the YAML is written BEFORE compose up.
#   Note   : changing an environment variable does NOT require a docker build.
#            The container only needs to be RECREATED, which Script C does
#            with `docker compose up -d --force-recreate app`.
#   Added  : 2026-11-12
#   Change : 2026-11-12 23:20 -- mean-calibration removed per user's
#            clarification that "73" means the mode, not the average.
#   Change : 2026-11-13 -- millisecond startup offset (plan 1).
#            Each day also draws a random start_offset_ms and the new `wait`
#            command sleeps exactly that many milliseconds before Script C
#            runs compose up. Purpose: the app login timestamp (the ONLY
#            online/offline moment the remote side can see) must not land on
#            a whole minute, and the boot->app gap must vary day to day.
#            WEREAD_DURATION is STILL an integer number of minutes: the
#            upstream image parses it with parseInt(), so a decimal would be
#            silently truncated. No image patch, no build, no bind mount.
#            Because endTime = startTime + N*60000 inside the app, a
#            millisecond-bearing startTime automatically yields a
#            millisecond-bearing endTime as well.
#   Change : 2026-11-13 -- same-day relaunch guard.
#            `roll` is idempotent per calendar day, but the old ExecStart
#            `compose up -d --force-recreate app` only skipped when the app
#            container was RUNNING. Once it had Exited(0), a second CT 107
#            boot on the same day would run the whole reading again and send
#            two more emails. Script D now records launched_date /
#            launched_at_bj via `mark`, answers `guard` with RUN/SKIP, and
#            owns the whole decision in `launch` (Script C's ExecStart).
#            A FAILED compose up leaves NO mark, so the next boot retries.
#   Change : 2026-11-13 14:00 -- `wait` short-circuits when today's reading
#            was already launched. Previously a 2nd same-day boot still slept
#            the full random ms offset (up to 60s) and rewrote `waited` before
#            `launch` skipped. The early return is read-only: no sleep, no
#            plan.json write. `--force` retains the old behaviour for tests.
#   Why a RELATIVE offset, not an absolute timestamp: Script D is invoked by
#   Script C only AFTER CT 107 has booted, i.e. at an unpredictable moment.
#   An absolute target inside 08:00-08:30 could already be in the past. The
#   offset is therefore measured from the moment `wait` is called, with
#   millisecond resolution and perf_counter calibration.
#

import argparse
import contextlib
from collections import Counter
try:
    import fcntl
except ImportError:      # offline validation on a non-POSIX host (Windows)
    fcntl = None
import io
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BJ = timezone(timedelta(hours=8))

MODE_MIN = 73.0              # the MOST FREQUENT duration, in minutes
SIGMA = 12.0                 # jitter / spread; raise for wider scatter
LO, HI = 62.0, 120.0        # hard inclusive bounds

# Uniform [0, 60000) ms: no centre is wanted here, the goal is to break the
# whole-minute boundary and make the boot->app gap unpredictable. Uniform is
# the least predictable choice; a bell-shaped offset would re-introduce a
# characteristic "typical" gap. 1 ms resolution.
OFFSET_MIN_MS = 0
OFFSET_MAX_MS = 60000
_SRNG = random.SystemRandom()   # OS entropy, not the reproducible Mersenne state

DEFAULT_COMPOSE = Path("/opt/stacks/weread-challenge/compose.yaml")
DEFAULT_STATE = Path("/var/lib/weread-duration/plan.json")
DEFAULT_BACKUP = Path("/var/lib/weread-duration/compose.before.yaml")
# Overridable only for offline validation; production (systemd) leaves it unset.
LOG_FILE = Path(os.environ.get("WEREAD_DURATION_LOG",
                               "/var/log/weread-duration.log"))
LOCK_FILE = Path("/run/weread-duration.lock")

COMPOSE_UP_CMD = ["/usr/bin/docker", "compose", "up", "-d",
                  "--force-recreate", "app"]
COMPOSE_PS_CMD = ["/usr/bin/docker", "compose", "ps",
                  "--status", "running", "--services"]

# [ \t] rather than \s: never let the pattern swallow a newline and match
# across lines. Exactly one such line must exist in compose.yaml.
DURATION_RE = re.compile(
    r"^([ \t]*-[ \t]*WEREAD_DURATION=)(\d+)([ \t]*(?:#[^\n]*)?)$", re.M
)


def bj_now():
    return datetime.now(BJ)


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def truncated_expectation(mu, sigma, lo, hi):
    """Informational only: the mean is explicitly NOT the centre we target."""
    a = (lo - mu) / sigma
    b = (hi - mu) / sigma
    z = norm_cdf(b) - norm_cdf(a)
    if z <= 1e-12:
        return hi if mu > hi else lo
    return mu + sigma * (norm_pdf(a) - norm_pdf(b)) / z


# For a truncated normal with lo <= mu <= hi, the density keeps its peak at
# mu, so the MODE is exactly mu. No mean-calibration is wanted here.
MU = MODE_MIN


def truncated_normal(mu, sigma, lo, hi):
    for _ in range(2000):
        x = random.gauss(mu, sigma)
        if lo <= x <= hi:
            return x
    return min(max(mu, lo), hi)


def draw_duration():
    x = truncated_normal(MU, SIGMA, LO, HI)
    n = max(int(LO), min(int(HI), int(round(x))))
    return n, x


def draw_offset_ms():
    """One random millisecond offset, uniform in [OFFSET_MIN_MS, OFFSET_MAX_MS)."""
    return _SRNG.randint(OFFSET_MIN_MS, OFFSET_MAX_MS - 1)


def precise_sleep_ms(ms):
    """Sleep exactly `ms` milliseconds using a monotonic-clock calibration.

    time.sleep() can return early (signal) or late (scheduler granularity), so
    recompute the remainder on every iteration and busy-wait the last 20 ms.
    Production runs on Linux CT 107, where this reaches sub-millisecond drift.
    """
    deadline = time.perf_counter() + ms / 1000.0
    while True:
        rem = deadline - time.perf_counter()
        if rem <= 0.0:
            return
        if rem > 0.020:
            time.sleep(min(rem - 0.010, 0.5))
        # else: tight loop for the final 20 ms


def log(msg, level="INFO"):
    line = "%s [%s] %s" % (bj_now().strftime("%Y-%m-%d %H:%M:%S %z"), level, msg)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line)


def load_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def current_duration(compose):
    try:
        text = compose.read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError("cannot read %s: %s" % (compose, e))
    hits = list(DURATION_RE.finditer(text))
    if len(hits) != 1:
        raise RuntimeError("expected exactly 1 WEREAD_DURATION line in %s, found %d"
                           % (compose, len(hits)))
    return int(hits[0].group(2)), text


def apply_duration(compose, duration, backup, dry_run=False):
    """Atomically set WEREAD_DURATION. Returns (old_value, changed)."""
    cur, text = current_duration(compose)
    if cur == duration:
        return cur, False
    new_text = DURATION_RE.sub(
        lambda m: m.group(1) + str(duration) + m.group(3), text, count=1
    )
    if dry_run:
        return cur, True
    if backup:
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(compose, backup)
    mode = compose.stat().st_mode & 0o7777
    fd, tmpname = tempfile.mkstemp(prefix=".compose.", dir=str(compose.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmpname, mode)
        os.replace(tmpname, compose)
    except Exception:
        try:
            os.unlink(tmpname)
        except OSError:
            pass
        raise
    return cur, True


def new_plan(n, sample):
    return {
        "date": bj_now().date().isoformat(),
        "duration": n,
        "sample": round(sample, 4),
        "applied": False,
        "applied_at_bj": None,
        "mode": MODE_MIN,
        "nominal_mu": round(MU, 4),
        "sigma": SIGMA,
        "bounds": [int(LO), int(HI)],
        # --- millisecond startup offset (Script D / plan 1) ---
        "start_offset_ms": draw_offset_ms(),
        "offset_bounds_ms": [OFFSET_MIN_MS, OFFSET_MAX_MS],
        "waited": False,
        "waited_at_bj": None,
        "waited_elapsed_ms": None,
        # --- same-day relaunch guard ---
        "launched_date": None,
        "launched_at_bj": None,
        "note": "generated by Script D (mode=73, sigma=12min, uniform ms offset)",
    }


def already_launched_today(state):
    if not state:
        return False
    return state.get("launched_date") == bj_now().date().isoformat()


def mark_launched(state, path, dry_run=False):
    """Persist the fact that today's reading task HAS been launched."""
    today = bj_now().date().isoformat()
    if not state:
        state = {"date": today, "note": "reconstructed by launch mark"}
    if state.get("launched_date") == today:
        return 0
    state["launched_date"] = today
    state["launched_at_bj"] = bj_now().isoformat()
    if dry_run:
        log("DRY-RUN: would mark launched_date=%s" % today)
        return 0
    save_state(path, state)
    log("marked launched_date=%s at %s" % (today, state["launched_at_bj"]))
    return 0


def cmd_plan(args):
    n, sample = draw_duration()
    state = new_plan(n, sample)
    if args.dry_run:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0
    save_state(args.state, state)
    log("plan: WEREAD_DURATION=%d (sample=%.4f) start_offset_ms=%d"
        % (n, sample, state["start_offset_ms"]))
    return 0


def cmd_apply(args):
    state = load_state(args.state)
    if not state:
        log("no plan found, run 'plan' first", "ERROR")
        return 1
    n = int(state["duration"])
    old, changed = apply_duration(args.compose, n, args.backup, args.dry_run)
    if args.dry_run:
        log("DRY-RUN: would set WEREAD_DURATION %s -> %d" % (old, n))
        return 0
    state["applied"] = changed or bool(state.get("applied"))
    state["applied_at_bj"] = bj_now().isoformat()
    save_state(args.state, state)
    log("apply: WEREAD_DURATION %s -> %d (changed=%s)" % (old, n, changed))
    return 0


def cmd_roll(args):
    """plan + apply in one step; idempotent per calendar day."""
    now = bj_now()
    state = load_state(args.state)
    if not args.force and state and state.get("date") == now.date().isoformat() \
            and state.get("applied"):
        n = int(state["duration"])
        cur, _ = current_duration(args.compose)
        if cur == n:
            log("already applied today: WEREAD_DURATION=%d, start_offset_ms=%s (skip)"
                % (n, state.get("start_offset_ms")))
            return 0
        log("state says applied but YAML has %s; re-applying %d" % (cur, n))
        old, _ = apply_duration(args.compose, n, args.backup, args.dry_run)
        log("re-applied WEREAD_DURATION %s -> %d" % (old, n))
        return 0
    n, sample = draw_duration()
    state = new_plan(n, sample)
    old, changed = apply_duration(args.compose, n, args.backup, args.dry_run)
    if args.dry_run:
        log("DRY-RUN: would set WEREAD_DURATION %s -> %d (sample=%.4f)"
            % (old, n, sample))
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return 0
    state["applied"] = True
    state["applied_at_bj"] = now.isoformat()
    save_state(args.state, state)
    log("roll: WEREAD_DURATION %s -> %d (sample=%.4f, mode=%.1f, sigma=%.1f, "
        "start_offset_ms=%d)"
        % (old, n, sample, MODE_MIN, SIGMA, state["start_offset_ms"]))
    return 0


def cmd_wait(args):
    """Sleep today's random millisecond offset. Idempotent per calendar day."""
    state = load_state(args.state)
    if not state:
        log("no plan found; Script C must call 'roll' before 'wait'", "ERROR")
        return 1
    # Same-day relaunch guard, deliberately placed BEFORE the legacy
    # self-healing below: an already-launched day must be STRICTLY read-only
    # (no sleep, no plan.json write at all). `launch` will SKIP anyway, so
    # sleeping the startup offset here would only delay the unit by up to
    # 60s for no benefit. A FAILED compose up leaves NO launched_date, so the
    # retry path still reaches the normal wait below. `--force` bypasses this
    # for offline validation.
    if already_launched_today(state) and not args.force:
        log("already launched today (%s); skip startup offset (launch will SKIP)"
            % state.get("launched_at_bj"))
        return 0
    if "start_offset_ms" not in state:
        # BACKWARD COMPATIBILITY: a plan.json written by pre-2026-11-13
        # Script D has no offset fields. `roll` legitimately SKIPS when such a
        # state says "already applied today", so the offset MUST be drawn
        # HERE -- otherwise wait would fail and abort the whole unit before
        # compose up ever runs. Persist it so a unit restart does not redraw.
        state["start_offset_ms"] = draw_offset_ms()
        state["offset_bounds_ms"] = [OFFSET_MIN_MS, OFFSET_MAX_MS]
        state.setdefault("waited", False)
        state.setdefault("waited_at_bj", None)
        state.setdefault("waited_elapsed_ms", None)
        if args.dry_run:
            log("DRY-RUN: legacy plan.json; would draw start_offset_ms=%d"
                % state["start_offset_ms"], "WARN")
        else:
            save_state(args.state, state)
            log("migrated legacy plan.json: drew start_offset_ms=%d"
                % state["start_offset_ms"], "WARN")
    if state.get("date") != bj_now().date().isoformat():
        # Do NOT fail here: roll and wait are adjacent ExecStartPre lines, so a
        # midnight rollover between them would otherwise abort the whole unit
        # BEFORE compose up. Script C has just run roll, so the state is fresh.
        log("plan date %s differs from today (rollover or manual restart); "
            "using its offset anyway" % state.get("date"), "WARN")
    if state.get("waited") and not args.force:
        log("already waited today (offset=%s ms), skip"
            % state.get("start_offset_ms"))
        return 0
    try:
        ms = int(state["start_offset_ms"])
    except (KeyError, TypeError, ValueError):
        log("state has no valid start_offset_ms; run 'roll' first", "ERROR")
        return 1
    if not (OFFSET_MIN_MS <= ms < OFFSET_MAX_MS):
        log("start_offset_ms=%d out of [%d,%d)" % (ms, OFFSET_MIN_MS, OFFSET_MAX_MS),
            "ERROR")
        return 1
    if args.dry_run:
        log("DRY-RUN: would sleep %d ms (%.3f s) before compose up" % (ms, ms / 1000.0))
        return 0
    t0 = time.perf_counter()
    precise_sleep_ms(ms)
    elapsed = (time.perf_counter() - t0) * 1000.0
    state["waited"] = True
    state["waited_at_bj"] = bj_now().isoformat()
    state["waited_elapsed_ms"] = round(elapsed, 3)
    save_state(args.state, state)
    log("wait: slept %d ms (actual %.3f ms, drift %+.3f ms)"
        % (ms, elapsed, elapsed - ms))
    return 0


def cmd_guard(args):
    """Read-only verdict for Script C. Prints exactly RUN or SKIP on stdout.

    Diagnostics go to stderr so a shell can capture a single clean token.
    Always exits 0 -- a crash is a crash, not a 'skip'.
    """
    state = load_state(args.state)
    today = bj_now().date().isoformat()
    if not state:
        sys.stderr.write("guard: no plan.json (roll should have run first); allowing RUN\n")
    if already_launched_today(state):
        sys.stderr.write("guard: already launched today (%s), SKIP\n"
                         % state.get("launched_at_bj"))
        print("SKIP")
    else:
        sys.stderr.write("guard: no launch recorded for %s, RUN\n" % today)
        print("RUN")
    return 0


def cmd_mark(args):
    """Manually mark today's reading task as launched (used by launch/tests)."""
    return mark_launched(load_state(args.state), args.state, args.dry_run)


def cmd_launch(args):
    """Script C's ExecStart: single decision point.

    guard -> (docker compose ps) -> compose up -> mark. Only a SUCCESSFUL
    compose up writes the mark, so a failed start can be retried next boot.
    --dry-run never spawns docker and never writes state.
    """
    state = load_state(args.state)
    today = bj_now().date().isoformat()
    if already_launched_today(state):
        log("launch: already launched today (%s), skip"
            % state.get("launched_at_bj"))
        return 0
    if args.dry_run:
        log("DRY-RUN: launch would check running services, then run: %s"
            % " ".join(COMPOSE_UP_CMD))
        return 0
    cwd = str(args.compose.parent)
    running = False
    try:
        ps = subprocess.run(COMPOSE_PS_CMD, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, timeout=30)
        running = "app" in (ps.stdout or "").splitlines()
    except (OSError, subprocess.SubprocessError) as e:
        log("launch: cannot query running services (%s); proceeding" % e, "WARN")
    if running:
        log("launch: app already running, skip compose up")
        return mark_launched(state, args.state)
    log("launch: %s (cwd=%s)" % (" ".join(COMPOSE_UP_CMD), cwd))
    try:
        rc = subprocess.call(COMPOSE_UP_CMD, cwd=cwd, timeout=300)
    except (OSError, subprocess.SubprocessError) as e:
        log("launch: compose up failed (%s); NOT marking (retry next boot)" % e,
            "ERROR")
        return 1
    if rc != 0:
        log("launch: compose up rc=%d; NOT marking (retry next boot)" % rc, "ERROR")
        return 1
    mark_launched(state, args.state)
    log("launch: compose up succeeded and launch marked for %s" % today)
    return 0


def cmd_status(args):
    cur, _ = current_duration(args.compose)
    print(json.dumps({
        "distribution": {
            "bounds": [int(LO), int(HI)],
            "mode": MODE_MIN,
            "sigma": SIGMA,
            "nominal_mu": round(MU, 4),
            "mean_after_truncation_informational": round(
                truncated_expectation(MU, SIGMA, LO, HI), 4),
        },
        "start_offset": {
            "bounds_ms": [OFFSET_MIN_MS, OFFSET_MAX_MS],
            "distribution": "uniform, 1 ms resolution",
            "caller": "Script C ExecStartPre: weread-duration.py wait",
            "skip_if_already_launched_today": True,
        },
        "launch_guard": {
            "state_fields": ["launched_date", "launched_at_bj"],
            "caller": "Script C ExecStart: weread-duration.py launch",
            "retry_on_compose_failure": True,
        },
        "compose": str(args.compose),
        "WEREAD_DURATION_in_yaml": cur,
        "today_plan": load_state(args.state),
        "now_bj": bj_now().isoformat(),
    }, indent=2, ensure_ascii=False))
    return 0


def cmd_selftest(args):
    n = 5000
    vals = sorted(draw_duration()[0] for _ in range(n))
    p5, p50, p95 = vals[int(0.05 * n)], vals[int(0.50 * n)], vals[int(0.95 * n)]
    counts = Counter(vals)
    modal_int, modal_count = counts.most_common(1)[0]
    # The densest 5-minute sliding window should be centred on the mode.
    masses = {c: sum(counts.get(c + d, 0) for d in (-2, -1, 0, 1, 2))
              for c in range(int(LO) + 2, int(HI) - 1)}
    peak5 = max(masses, key=masses.get)
    mean = sum(vals) / float(n)
    in_window = all(int(LO) <= v <= int(HI) for v in vals)
    print("samples=%d mode=%dmin sigma=%.1fmin bounds=[%d,%d]"
          % (n, int(MODE_MIN), SIGMA, int(LO), int(HI)))
    print("nominal_mu=%.4f  mean_after_truncation=%.3f (informational, NOT the centre)"
          % (MU, truncated_expectation(MU, SIGMA, LO, HI)))
    print("min=%d P5=%d P50=%d P95=%d max=%d sampled_mean=%.3f"
          % (vals[0], p5, p50, p95, vals[-1], mean))
    print("modal_integer=%d (count=%d)  densest_5min_window_centre=%d  all_in_window=%s"
          % (modal_int, modal_count, peak5, in_window))

    offs = [draw_offset_ms() for _ in range(n)]
    off_mean = sum(offs) / float(n)
    off_ok = (min(offs) >= OFFSET_MIN_MS
              and max(offs) < OFFSET_MAX_MS
              and len(set(offs)) > int(0.8 * n)
              and abs(off_mean - (OFFSET_MIN_MS + OFFSET_MAX_MS) / 2.0) < 600)
    print("offset: bounds=[%d,%d) samples=%d unique=%d min=%d max=%d mean=%.1fms  %s"
          % (OFFSET_MIN_MS, OFFSET_MAX_MS, n, len(set(offs)), min(offs), max(offs),
             off_mean, "OK" if off_ok else "FAILED"))

    drift = []
    for probe in (5, 13, 37):
        t0 = time.perf_counter()
        precise_sleep_ms(probe)
        drift.append((time.perf_counter() - t0) * 1000.0 - probe)
    sleep_ok = max(abs(d) for d in drift) < 3.0
    print("precision: probes=5/13/37ms drift_ms=%s  %s"
          % ([round(d, 3) for d in drift], "OK" if sleep_ok else "FAILED"))

    # guard/mark smoke test on a throwaway state: never touches production.
    guard_ok = False
    try:
        with tempfile.TemporaryDirectory(prefix="weread-guard-") as td:
            s = Path(td) / "plan.json"
            ns = argparse.Namespace(state=s, dry_run=False, force=False)
            save_state(s, {"date": bj_now().date().isoformat(),
                           "launched_date": None, "launched_at_bj": None})
            b1 = io.StringIO()
            with contextlib.redirect_stdout(b1):
                cmd_guard(ns)
            first = b1.getvalue().strip()
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_mark(ns)
            b2 = io.StringIO()
            with contextlib.redirect_stdout(b2):
                cmd_guard(ns)
            second = b2.getvalue().strip()
            guard_ok = first == "RUN" and second == "SKIP"
            print("guard/mark: fresh=%s after_mark=%s  %s"
                  % (first, second, "OK" if guard_ok else "FAILED"))
    except Exception as e:
        print("guard/mark: FAILED (%s: %s)" % (type(e).__name__, e))

    # `wait` must short-circuit when today's reading was already launched:
    # zero sleep, zero state write. Monkeypatch the sleeper so this test is
    # instant and can assert whether it was called at all (no real 57s wait).
    wait_guard_ok = False
    orig_sleep = precise_sleep_ms
    sleeps = []

    def fake_sleep(ms):
        sleeps.append(ms)

    try:
        globals()["precise_sleep_ms"] = fake_sleep
        with tempfile.TemporaryDirectory(prefix="weread-waitguard-") as td:
            s = Path(td) / "plan.json"
            today = bj_now().date().isoformat()
            launched = {
                "date": today, "duration": 68,
                "start_offset_ms": 57455, "offset_bounds_ms": [0, 60000],
                "waited": False, "waited_at_bj": None,
                "waited_elapsed_ms": None,
                "launched_date": today,
                "launched_at_bj": "2026-11-13T08:08:07+08:00",
            }
            ns = argparse.Namespace(state=s, dry_run=False, force=False)

            # A) already launched today -> no sleep, plan.json byte-identical.
            save_state(s, dict(launched))
            sleeps.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                rc_skip = cmd_wait(ns)
            skip_sleeps = list(sleeps)
            skip_ok = (rc_skip == 0 and not skip_sleeps
                       and load_state(s) == launched)

            # B) NOT launched (e.g. a failed compose up left no mark): the
            #    offset MUST still be applied and `waited` recorded.
            save_state(s, dict(launched, launched_date=None,
                               launched_at_bj=None))
            sleeps.clear()
            with contextlib.redirect_stdout(io.StringIO()):
                rc_run = cmd_wait(ns)
            run_sleeps = list(sleeps)
            run_ok = (rc_run == 0 and run_sleeps == [57455]
                      and load_state(s).get("waited") is True)

            # C) --force must still bypass the early return (test semantics).
            save_state(s, dict(launched))
            sleeps.clear()
            ns_force = argparse.Namespace(state=s, dry_run=False, force=True)
            with contextlib.redirect_stdout(io.StringIO()):
                rc_force = cmd_wait(ns_force)
            force_ok = (rc_force == 0 and list(sleeps) == [57455])

            wait_guard_ok = skip_ok and run_ok and force_ok
            print("wait guard: launched-> rc=%d sleeps=%s state_unchanged=%s | "
                  "not_launched-> rc=%d sleeps=%s | force-> rc=%d sleeps=%d  %s"
                  % (rc_skip, skip_sleeps, skip_ok, rc_run, run_sleeps,
                     rc_force, len(sleeps), "OK" if wait_guard_ok else "FAILED"))
    except Exception as e:
        print("wait guard: FAILED (%s: %s)" % (type(e).__name__, e))
    finally:
        globals()["precise_sleep_ms"] = orig_sleep

    # Deterministic assertions ONLY. `modal_integer` / `peak5` are printed as
    # diagnostics, NOT asserted: sigma=12min is far wider than a 1-minute bin,
    # so the single-integer mode legitimately wanders (73, 75, 76 ...) across
    # 5000-sample runs. Asserting `abs(peak5 - mode) <= 2` made this selftest
    # flaky and could make a healthy deploy report FAILED. The mode is instead
    # guaranteed structurally: for a truncated normal with lo <= mu <= hi the
    # density peak stays at mu, so MU == MODE_MIN is the real invariant.
    mode_structural = (MU == MODE_MIN and LO <= MU <= HI)
    mean_higher = mean > MODE_MIN + 1.0   # truncation pushes the mean up
    ok = (in_window and mode_structural and mean_higher and off_ok
          and sleep_ok and guard_ok and wait_guard_ok)
    print("SELFTEST %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def main():
    p = argparse.ArgumentParser(description="Script D: daily random WEREAD_DURATION")
    p.add_argument("command",
                   choices=["plan", "apply", "roll", "wait", "guard", "mark",
                            "launch", "status", "selftest"])
    p.add_argument("--compose", type=Path, default=DEFAULT_COMPOSE)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true",
                   help="roll: re-draw today; wait: sleep again even if waited")
    args = p.parse_args()

    # wait/guard/mark/launch take NO lock: Script C runs all of these
    # serially, and a lock miss would silently skip a step (for `wait` that
    # would start the app early -- exactly what the offset is meant to avoid).
    lock = None
    need_lock = args.command in ("plan", "apply", "roll")
    if need_lock and fcntl is None:
        # Only reachable on a non-POSIX host; production CT 107 always has fcntl.
        log("fcntl unavailable on this host; running WITHOUT the lock "
            "(offline validation only)", "WARN")
    elif need_lock:
        lock = LOCK_FILE.open("a+")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another instance holds the lock, skip", "WARN")
            return 0

    handlers = {
        "plan": cmd_plan,
        "apply": cmd_apply,
        "roll": cmd_roll,
        "wait": cmd_wait,
        "guard": cmd_guard,
        "mark": cmd_mark,
        "launch": cmd_launch,
        "status": cmd_status,
        "selftest": cmd_selftest,
    }
    try:
        return handlers[args.command](args)
    except Exception as e:
        log("%s failed: %s: %s" % (args.command, type(e).__name__, e), "ERROR")
        return 1


if __name__ == "__main__":
    sys.exit(main())
