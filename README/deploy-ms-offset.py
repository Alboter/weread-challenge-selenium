#!/usr/bin/env python3
"""Deploy Script C + D (millisecond offset + same-day relaunch guard) to CT 107.

Revision 2026-11-13 14:00: Script D's `wait` now short-circuits when today's
reading was already launched, so a 2nd same-day CT 107 boot no longer sleeps
the full random ms offset before `launch` skips. Script C is unchanged but is
re-uploaded with identical bytes to keep the deploy atomic and consistent.

Scope: ONLY /usr/local/bin/weread-duration.py (Script D) and
       /etc/systemd/system/weread-boot-start.service (Script C).
Script A/B on the PVE host are NOT touched. No image patch, no build,
no bind mount, no `systemctl start`, no `docker compose up`.

Usage:
    python deploy-ms-offset.py --dry-run        # print the plan, no connection
    python deploy-ms-offset.py --verify-only    # connect, read-only checks
    python deploy-ms-offset.py                 # backup + upload + verify

The password can be overridden with the CT107_PWD environment variable.
"""

import argparse
import hashlib
import json
import os
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paramiko

HOST = "192.168.91.130"          # CT 107 (LXC), NOT the PVE host
USER = "root"
PWD = os.environ.get("CT107_PWD", "2900880")

ROOT = Path(__file__).resolve().parent
STATE = "/var/lib/weread-duration/plan.json"
COMPOSE = "/opt/stacks/weread-challenge/compose.yaml"
UNIT = "/etc/systemd/system/weread-boot-start.service"

FILES = [
    ("weread-duration.py", "/usr/local/bin/weread-duration.py", 0o755),
    ("scriptC-weread-boot-start.service", UNIT, 0o644),
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=22, username=USER, password=PWD, timeout=15,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, timeout=90):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    rc = out.channel.recv_exit_status()
    return rc, out.read().decode("utf-8", "replace").strip(), \
        err.read().decode("utf-8", "replace").strip()


def show(title, rc, out, err):
    print("\n--- %s (rc=%d) ---" % (title, rc))
    if out:
        print(out)
    if err:
        print("[stderr] " + err)


def upload(c, sftp, local, remote, mode):
    data = local.read_bytes()
    if not data:
        raise RuntimeError("local file %s is empty - refusing to deploy" % local)
    tmp = "/tmp/" + local.name + ".msnew"
    # Plain bytes over SFTP. NO gzip here: a zlib/gzip mismatch once wrote a
    # zero-byte Script B to the host. Always stage in /tmp, verify, then mv.
    with sftp.open(tmp, "w") as f:
        f.write(data)
    rc, out, err = run(c, "test -s %s && sha256sum %s | cut -d' ' -f1" % (tmp, tmp))
    if rc != 0 or out != digest(data):
        raise RuntimeError("SHA256 mismatch for %s: remote=%r local=%s"
                           % (remote, out, digest(data)))
    rc, out, err = run(c, "mv %s %s && chmod %o %s" % (tmp, remote, mode, remote))
    if rc != 0:
        raise RuntimeError("cannot install %s: %s" % (remote, err))
    print("INSTALLED %s (%d bytes, sha256=%s, mode=%o)"
          % (remote, len(data), digest(data)[:16], mode))


def migrate_state(c, sftp, backup_dir):
    """Bring an existing plan.json up to the current Script D schema.

    Two independent migrations, both idempotent:
      * start_offset_ms / waited       -- millisecond offset (plan 1)
      * launched_date / launched_at_bj -- same-day relaunch guard
    If today's reading already ran (applied=true and applied_at_bj is today),
    the launch is BACKFILLED so the guard takes effect immediately, today.
    """
    try:
        with sftp.open(STATE, "r") as f:
            state = json.loads(f.read().decode("utf-8"))
    except (IOError, OSError, ValueError) as e:
        print("STATE: no readable plan.json (%s); Script D will create one" % e)
        return

    changed = []
    if "start_offset_ms" not in state:
        state["start_offset_ms"] = random.SystemRandom().randint(0, 59999)
        state["offset_bounds_ms"] = [0, 60000]
        state.setdefault("waited", False)
        state.setdefault("waited_at_bj", None)
        state.setdefault("waited_elapsed_ms", None)
        changed.append("start_offset_ms=%d" % state["start_offset_ms"])

    if "launched_date" not in state:
        bj_today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        applied = str(state.get("applied_at_bj") or "")
        if state.get("applied") and applied.startswith(bj_today):
            # Confirmed by the journal/app container: today's reading really
            # did run. Without this backfill a second boot today would repeat it.
            state["launched_date"] = bj_today
            state["launched_at_bj"] = applied
            changed.append("launched_date=%s (backfilled: today already ran)"
                           % bj_today)
        else:
            state["launched_date"] = None
            state["launched_at_bj"] = None
            changed.append("launched_date=None")

    if not changed:
        print("STATE: already current (offset=%s, launched_date=%s)"
              % (state.get("start_offset_ms"), state.get("launched_date")))
        return

    run(c, "cp -a %s %s/ 2>/dev/null || true" % (STATE, backup_dir))
    tmp = "/tmp/weread-plan.json.msnew"
    with sftp.open(tmp, "w") as f:
        f.write((json.dumps(state, indent=2, ensure_ascii=False) + "\n")
                .encode("utf-8"))
    rc, out, err = run(c, "test -s %s && mv %s %s && chmod 644 %s"
                       % (tmp, tmp, STATE, STATE))
    if rc != 0:
        raise RuntimeError("cannot migrate %s: %s" % (STATE, err))
    print("STATE: migrated -> " + ", ".join(changed))


VERIFY_CMDS = [
    ("python syntax", "python3 -m py_compile /usr/local/bin/weread-duration.py && echo PY_OK"),
    ("systemd unit", "systemd-analyze verify %s 2>&1; echo UNIT_VERIFY_RC=$?" % UNIT),
    ("unit enabled", "systemctl is-enabled weread-boot-start.service"),
    ("unit active (RemainAfterExit=yes: 'active' is normal after a boot run)",
     "systemctl is-active weread-boot-start.service || true"),
    ("Script D selftest", "python3 /usr/local/bin/weread-duration.py selftest"),
    ("roll dry-run (NO write)", "python3 /usr/local/bin/weread-duration.py roll --dry-run"),
    ("wait dry-run (NO sleep)", "python3 /usr/local/bin/weread-duration.py wait --dry-run"),
    ("REAL wait timing (instant when today already launched)",
     "t0=$(date +%s%3N); python3 /usr/local/bin/weread-duration.py wait >/dev/null 2>&1; "
     "rc=$?; echo WAIT_RC=$rc ELAPSED_MS=$(( $(date +%s%3N) - t0 ))"),
    ("guard verdict (read-only)", "python3 /usr/local/bin/weread-duration.py guard"),
    ("launch dry-run (NO docker, NO state write)",
     "python3 /usr/local/bin/weread-duration.py launch --dry-run"),
    ("Script D status", "python3 /usr/local/bin/weread-duration.py status"),
    ("compose valid", "cd /opt/stacks/weread-challenge && docker compose config -q && echo COMPOSE_OK"),
    ("compose env", "grep -n 'WEREAD_DURATION\\|WEREAD_AGREE_TERMS' %s" % COMPOSE),
    ("unit exec lines", "grep -nE 'ExecStartPre|ExecStart=|TimeoutStartSec' %s" % UNIT),
    ("no stale weread units",
     "systemctl list-unit-files | grep -i weread || true"),
]


def verify(c, check_hashes=False):
    if check_hashes:
        print("\n=== FILE HASH MATCH (local vs remote) ===")
        for name, remote, _ in FILES:
            local = (ROOT / name).read_bytes()
            rc, out, err = run(c, "sha256sum %s | cut -d' ' -f1" % remote)
            print("%-6s %s  local=%s remote=%s"
                  % ("MATCH" if out == digest(local) else "DIFFER",
                     remote, digest(local)[:16], out[:16]))
    print("\n=== READ-ONLY VERIFICATION ===")
    for title, cmd in VERIFY_CMDS:
        rc, out, err = run(c, cmd)
        show(title, rc, out, err)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = "/root/weread-ms-backup-" + stamp

    print("TARGET      : %s (CT 107) as %s" % (HOST, USER))
    print("BACKUP DIR  : %s" % backup_dir)
    print("NOT TOUCHED : PVE host scripts A/B, compose.yaml, Docker images")
    print("NEVER RUNS  : systemctl start/restart, docker compose up, real reading\n")
    for name, remote, mode in FILES:
        local = ROOT / name
        d = local.read_bytes()
        print("  %-42s -> %-55s %6d B sha256=%s mode=%o"
              % (name, remote, len(d), digest(d)[:16], mode))

    if args.dry_run:
        print("\nDRY-RUN: no connection made. Plan:")
        print("  1. mkdir %s and cp -a both current remote files into it" % backup_dir)
        print("  2. cp -a %s and %s into it" % (STATE, COMPOSE))
        print("  3. stage each new file in /tmp, verify size + SHA256, mv, chmod")
        print("  4. migrate plan.json: start_offset_ms/waited + launched_date")
        print("     (backfill today's launch if applied_at_bj is today)")
        print("  5. py_compile, systemd-analyze verify, daemon-reload")
        print("  6. read-only verification (selftest / guard / dry-runs / status)")
        return 0

    print("\nConnecting ...")
    c = connect()
    sftp = c.open_sftp()
    try:
        if args.verify_only:
            verify(c, check_hashes=True)
            return 0

        rc, out, err = run(c, "mkdir -p %s" % backup_dir)
        if rc != 0:
            raise RuntimeError("cannot create backup dir: %s" % err)
        for _, remote, _ in FILES:
            run(c, "cp -a %s %s/ 2>/dev/null || true" % (remote, backup_dir))
        run(c, "cp -a %s %s/ 2>/dev/null || true" % (COMPOSE, backup_dir))
        print("\nBACKUP: %s" % backup_dir)
        show("backup contents", *run(c, "ls -l %s" % backup_dir))

        for name, remote, mode in FILES:
            upload(c, sftp, ROOT / name, remote, mode)
        migrate_state(c, sftp, backup_dir)

        show("py_compile", *run(c, "python3 -m py_compile /usr/local/bin/weread-duration.py && echo PY_OK"))
        show("systemd-analyze verify", *run(c, "systemd-analyze verify %s 2>&1; echo RC=$?" % UNIT))
        show("daemon-reload", *run(c, "systemctl daemon-reload && echo RELOADED"))

        verify(c)
        print("\n=== DONE ===")
        print("Deployed Script C + D. NO service started, NO container recreated,")
        print("NO reading task triggered. The change takes effect at the next")
        print("CT 107 boot (Script B, 08:00-08:30 CST).")
        print("\nRollback if needed:")
        print("  cp -a %s/weread-duration.py /usr/local/bin/weread-duration.py" % backup_dir)
        print("  cp -a %s/weread-boot-start.service %s" % (backup_dir, UNIT))
        print("  systemctl daemon-reload")
        return 0
    finally:
        sftp.close()
        c.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print("DEPLOY FAILED: %s: %s" % (type(e).__name__, e), file=sys.stderr)
        sys.exit(1)
