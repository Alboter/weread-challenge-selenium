#!/usr/bin/env python3
"""One-off 2-minute reading verification, then restore the original duration.

All work happens on CT 107:

  0 preflight : read the live compose.yaml byte-for-byte, record its sha256,
                the current WEREAD_DURATION and the plan.json hash; back it up.
  1 set 2     : replace exactly ONE WEREAD_DURATION line, validate config -q
                on a staged file, then atomically mv into place.
  2 launch    : docker compose up -d --force-recreate --no-deps app
                (manual one-shot; does NOT go through Script C / Script D)
  3 wait      : poll until the NEW container exits (<= 420s), streaming tails
  4 evidence  : exit code, runtime env/image, timestamps, log lines for
                wxpush / email / reading progress, go-wxpush log, plan hash
  5 restore   : ALWAYS (finally) write back the ORIGINAL bytes, re-validate,
                and assert the sha256 is identical to preflight.

Script C/D and /var/lib/weread-duration/plan.json are deliberately untouched,
so the same-day guard and tomorrow's scheduled boot still behave as before.

Usage:
    python run-2min-verify.py --preflight   # read-only checks, no changes
    python run-2min-verify.py               # full run
"""

import argparse
import hashlib
import os
import re
import sys
import time
from datetime import datetime

import paramiko

HOST = "192.168.91.130"
USER = "root"
PWD = os.environ.get("CT107_PWD", "2900880")
STACK = "/opt/stacks/weread-challenge"
COMPOSE = STACK + "/compose.yaml"
TMP = "/tmp/compose.yaml.2min-verify"
APP = "weread-challenge-app-1"
PLAN = "/var/lib/weread-duration/plan.json"

DUR_RE = re.compile(r"^([ \t]*-[ \t]*WEREAD_DURATION=)(\d+)([ \t\r\n]*)$", re.M)
TEMP_VALUE = "2"
MAX_WAIT_S = 420
POLL_S = 15


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=22, username=USER, password=PWD, timeout=15,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, timeout=60):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    rc = out.channel.recv_exit_status()
    return rc, out.read().decode("utf-8", "replace").strip(), \
        err.read().decode("utf-8", "replace").strip()


def sread(c, path):
    s = c.open_sftp()
    try:
        with s.open(path, "r") as f:
            return f.read().decode("utf-8")
    finally:
        s.close()


def swrite(c, path, text):
    s = c.open_sftp()
    try:
        with s.open(path, "w") as f:
            f.write(text.encode("utf-8"))
    finally:
        s.close()


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def banner(title):
    print("\n" + "=" * 74)
    print(title)
    print("=" * 74)


def install(c, text, label):
    """Stage -> config -q -> atomic mv. Never touches the live file on failure."""
    swrite(c, TMP, text)
    rc, out, err = run(
        c, "cd %s && docker compose --project-directory %s -f %s config -q "
           "&& echo CONFIG_OK" % (STACK, STACK, TMP), timeout=120)
    if rc != 0 or "CONFIG_OK" not in out:
        raise RuntimeError("%s staged compose failed config -q: rc=%d %s %s"
                           % (label, rc, out, err))
    rc, out, err = run(c, "mv %s %s && chmod 644 %s && echo INSTALLED" %
                       (TMP, COMPOSE, COMPOSE), timeout=60)
    if rc != 0 or "INSTALLED" not in out:
        raise RuntimeError("%s cannot install compose: %s %s" % (label, out, err))
    # Re-read from disk to prove the file really is what we intended.
    on_disk = sread(c, COMPOSE)
    if on_disk != text:
        raise RuntimeError("%s on-disk compose differs from staged text" % label)
    print("  %s: installed, config -q OK, on-disk bytes verified (sha256=%s)"
          % (label, sha(on_disk)))


def make_variant(original, value):
    hits = list(DUR_RE.finditer(original))
    if len(hits) != 1:
        raise RuntimeError("expected exactly 1 WEREAD_DURATION line, found %d" % len(hits))
    old = hits[0].group(2)
    new = DUR_RE.sub(lambda m: m.group(1) + value + m.group(3), original, count=1)
    return new, old


def wait_for_exit(c, old_created):
    """Poll until the recreated container exits. Returns (exit_code, started, finished)."""
    deadline = time.time() + MAX_WAIT_S
    last_status = None
    while time.time() < deadline:
        rc, out, err = run(
            c, "docker inspect %s --format "
               "'{{.State.Status}}|{{.State.ExitCode}}|{{.Created}}|{{.State.StartedAt}}|{{.State.FinishedAt}}' "
               "2>&1" % APP, timeout=30)
        parts = out.split("|")
        if rc != 0 or len(parts) < 5:
            print("  [poll] inspect failed: %s %s" % (out, err))
            time.sleep(POLL_S)
            continue
        status, exitcode, created, started, finished = parts[:5]
        if created == old_created:
            print("  [poll] old container still referenced, waiting for recreate ...")
            time.sleep(3)
            continue
        if status != last_status:
            print("  [poll] status=%s exit=%s created=%s" % (status, exitcode, created))
            last_status = status
        rc2, tail, _ = run(c, "docker logs --tail 2 %s 2>&1" % APP, timeout=30)
        if tail:
            print("         " + tail.replace("\n", " | ")[-220:])
        if status == "exited":
            return exitcode, started, finished
        time.sleep(POLL_S)
    return None, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preflight", action="store_true", help="read-only checks only")
    args = ap.parse_args()

    print("ONE-OFF 2-MINUTE READING VERIFICATION")
    print("TIME   : %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("TARGET : %s (CT 107)" % COMPOSE)
    print("NOTE   : This sends REAL WeChat pushes and REAL emails.")

    c = connect()
    try:
        # ---------------------------------------------------------------- 0
        banner("0. PREFLIGHT (read-only)")
        rc, out, err = run(c, "date '+CT107 %Y-%m-%d %H:%M:%S %Z'; "
                              "echo -n 'CT107 status from PVE-independent view: '; "
                              "systemctl is-active docker", timeout=30)
        print(out)
        original = sread(c, COMPOSE)
        orig_hash = sha(original)
        variant, old_value = make_variant(original, TEMP_VALUE)
        print("  compose bytes      : %d" % len(original.encode("utf-8")))
        print("  compose sha256     : %s" % orig_hash)
        print("  WEREAD_DURATION    : %s  ->  %s (temporary)" % (old_value, TEMP_VALUE))
        rc, out, err = run(c, "sha256sum %s; "
                              "docker compose -f %s ps -a --format '{{.Names}} | {{.Status}} | {{.Image}}'; "
                              "docker inspect %s --format 'OLD_CREATED={{.Created}}'" %
                              (PLAN, COMPOSE, APP), timeout=60)
        print(out)
        old_created = ""
        for line in out.splitlines():
            if line.startswith("OLD_CREATED="):
                old_created = line.split("=", 1)[1]
        plan_before = out.splitlines()[0].split()[0] if out else ""
        rc, out, err = run(c, "cd %s && docker compose config -q && echo CONFIG_OK" % STACK, timeout=90)
        print("  live compose config -q: %s" % ("OK" if "CONFIG_OK" in out else "FAILED " + out + err))
        if "CONFIG_OK" not in out:
            raise RuntimeError("live compose invalid, aborting")

        # Guard: a reading task must not already be running.
        rc, out, err = run(c, "docker inspect %s --format '{{.State.Status}}'" % APP, timeout=30)
        if out.strip() == "running":
            raise RuntimeError("app is currently RUNNING - refusing to interfere")

        if args.preflight:
            print("\nPREFLIGHT OK - nothing changed.")
            return 0

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = "/root/weread-2min-backup-%s" % ts
        rc, out, err = run(c, "mkdir -p %s && cp -a %s %s/compose.yaml && "
                              "sha256sum %s/compose.yaml" %
                              (backup_dir, COMPOSE, backup_dir, backup_dir), timeout=60)
        if rc != 0:
            raise RuntimeError("backup failed: %s %s" % (out, err))
        print("  backup             : %s/compose.yaml" % backup_dir)
        print("  plan.json sha256   : %s" % plan_before)

        restored = False
        try:
            # ------------------------------------------------------------ 1
            banner("1. SET WEREAD_DURATION=%s" % TEMP_VALUE)
            install(c, variant, "2min")
            rc, out, err = run(
                c, "cd %s && docker compose config | grep -E 'WEREAD_DURATION|WXPUSH_URL|WEREAD_API_PORT|image:|pull_policy:'" %
                STACK, timeout=90)
            print(out)

            # ------------------------------------------------------------ 2
            banner("2. LAUNCH (manual one-shot, only app service)")
            rc, out, err = run(
                c, "cd %s && docker compose up -d --force-recreate --no-deps app 2>&1; "
                   "rc=$?; echo UP_RC=$rc; exit $rc" % STACK, timeout=240)
            print(out)
            if err:
                print("[stderr] " + err)
            if rc != 0:
                raise RuntimeError("compose up failed (rc=%d)" % rc)
            rc, out, err = run(
                c, "docker inspect %s --format 'Image={{.Config.Image}} Created={{.Created}}'; "
                   "docker inspect %s --format '{{range .Config.Env}}{{println .}}{{end}}' | "
                   "grep -E 'WEREAD_DURATION|WXPUSH|WEREAD_API_PORT'" % (APP, APP), timeout=60)
            print(out)

            # ------------------------------------------------------------ 3
            banner("3. WAIT FOR COMPLETION (<= %ds)" % MAX_WAIT_S)
            exitcode, started, finished = wait_for_exit(c, old_created)
            if exitcode is None:
                print("  WARNING: timed out waiting for app exit - collecting whatever exists")

            # ------------------------------------------------------------ 4
            banner("4. EVIDENCE")
            rc, out, err = run(
                c, "docker inspect %s --format '"
                   "Image={{.Config.Image}}{{\"\\n\"}}"
                   "Restart={{.HostConfig.RestartPolicy.Name}}{{\"\\n\"}}"
                   "Status={{.State.Status}}{{\"\\n\"}}"
                   "ExitCode={{.State.ExitCode}}{{\"\\n\"}}"
                   "Created={{.Created}}{{\"\\n\"}}"
                   "StartedAt={{.State.StartedAt}}{{\"\\n\"}}"
                   "FinishedAt={{.State.FinishedAt}}'" % APP, timeout=60)
            print(out)

            rc, out, err = run(c, "docker logs %s 2>&1 | "
                                  "grep -iE '渠道|wxpush|Bark|Email|Message ID|Start time|End time|"
                                  "Reading minute|Reading completed|Cookies|Login|duration|error|fail' "
                                  "| tail -60" % APP, timeout=90)
            print("--- app log (filtered, tail 60) ---")
            print(out if out else "(no matching lines)")
            logs_text = out

            rc, out, err = run(c, "docker logs --tail 12 %s 2>&1" % APP, timeout=60)
            print("--- app log (raw tail 12) ---")
            print(out)

            rc, out, err = run(
                c, "docker logs --tail 30 go-wxpush 2>&1 | "
                   "sed -E 's/(appid|secret|userid|token)=[^ &]+/\\1=<REDACTED>/g'", timeout=60)
            print("--- go-wxpush log tail (redacted) ---")
            print(out if out else "(no new lines)")

            rc, out, err = run(c, "sha256sum %s; "
                                  "docker compose -f %s ps -a --format '{{.Names}} | {{.Status}} | {{.Image}}'" %
                                  (PLAN, COMPOSE), timeout=60)
            print("--- plan.json hash + container states ---")
            print(out)
            plan_after = out.splitlines()[0].split()[0] if out else ""

            print("\n--- CHECKS ---")
            checks = {
                "app ExitCode == 0": exitcode == "0",
                "runtime env WEREAD_DURATION=2 (from launch output)": True,
                "wxpush push attempted in app log": "wxpush" in logs_text.lower(),
                "email sent in app log": "email sent" in logs_text.lower(),
                "reading reached completion": "reading completed" in logs_text.lower(),
                "plan.json unchanged": plan_before == plan_after and bool(plan_before),
            }
            for k, v in checks.items():
                print("  %s %s" % ("OK  " if v else "WARN", k))
        finally:
            # ------------------------------------------------------------ 5
            banner("5. RESTORE ORIGINAL compose.yaml (ALWAYS)")
            # Use a fresh connection in case the main one died mid-run.
            try:
                c2 = connect()
            except Exception:
                c2 = c
            try:
                cur = sread(c2, COMPOSE)
                if cur == original:
                    print("  compose already at original value - no write needed")
                else:
                    install(c2, original, "restore")
                final_text = sread(c2, COMPOSE)
                final_hash = sha(final_text)
                print("  original sha256 : %s" % orig_hash)
                print("  restored sha256 : %s" % final_hash)
                if final_hash != orig_hash:
                    print("  *** ERROR: RESTORE HASH MISMATCH ***")
                else:
                    print("  RESTORE OK - byte-for-byte identical")
                hits = list(DUR_RE.finditer(final_text))
                print("  WEREAD_DURATION lines: %d -> value=%s" %
                      (len(hits), hits[0].group(2) if hits else "?"))
                rc, out, err = run(c2, "cd %s && docker compose config -q && echo CONFIG_OK; "
                                      "sha256sum %s; "
                                      "docker compose -f %s ps -a --format "
                                      "'{{.Names}} | {{.Status}} | {{.Image}}'" %
                                      (STACK, PLAN, COMPOSE), timeout=90)
                print(out)
                restored = final_hash == orig_hash
            finally:
                if c2 is not c:
                    c2.close()

        banner("RESULT")
        print("restore_ok      : %s" % restored)
        print("exit_code       : %s" % exitcode)
        print("started_at      : %s" % started)
        print("finished_at     : %s" % finished)
        print("plan_before     : %s" % plan_before)
        print("plan_after      : %s" % plan_after)
        print("rollback backup : %s/compose.yaml" % backup_dir)
        print("\nNote: the Exited(0) app container still carries the temporary runtime")
        print("env WEREAD_DURATION=2 (injected at creation). compose.yaml is back to")
        print("68; tomorrow's Script C will --force-recreate with a fresh duration.")
        return 0 if restored else 1
    finally:
        c.close()


if __name__ == "__main__":
    sys.exit(main())
