#!/usr/bin/env python3
"""Incrementally enable wxpush in the LIVE Dockge compose.yaml.

Only 4 line-level changes, everything else is preserved byte-for-byte:
  1. app image      : jqknono/weread-challenge:latest -> weread-challenge:local
  2. app pull_policy: always -> never
  3. add WXPUSH_URL=http://192.168.91.130:5566 to app environment
  4. add WEREAD_API_PORT=0 to app environment

WXPUSH_BASE_URL is intentionally NOT set: go-wxpush was started with its own
-base_url (http://192.168.91.130:5566), and a request-level base_url would
override it.

Does NOT run 'docker compose up', does NOT touch Script C/D or plan.json.
The new config takes effect on the next Script C launch (tomorrow's boot).

Usage:
    python deploy-wxpush-compose.py --dry-run        # local preview only
    python deploy-wxpush-compose.py --verify-only    # read-only remote checks
    python deploy-wxpush-compose.py                  # backup + patch + validate
"""

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime

import paramiko

HOST = "192.168.91.130"
USER = "root"
PWD = os.environ.get("CT107_PWD", "2900880")
STACK = "/opt/stacks/weread-challenge"
COMPOSE = STACK + "/compose.yaml"

OLD_IMAGE = "    image: jqknono/weread-challenge:latest\n"
NEW_IMAGE = "    image: weread-challenge:local\n"
OLD_PULL = "    pull_policy: always\n"
NEW_PULL = "    pull_policy: never\n"

ADD_LINES = [
    "      - WXPUSH_URL=http://192.168.91.130:5566\n",
    "      - WEREAD_API_PORT=0\n",
]


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=22, username=USER, password=PWD, timeout=15,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, timeout=120):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    rc = out.channel.recv_exit_status()
    return rc, out.read().decode("utf-8", "replace").strip(), \
        err.read().decode("utf-8", "replace").strip()


def patch(text):
    """Return (new_text, change_log). Abort on any unexpected shape."""
    log = []
    for label, needle in [
        ("official app image", OLD_IMAGE),
        ("app pull_policy always", OLD_PULL),
        ("WEREAD_DURATION", "      - WEREAD_DURATION="),
        ("WEREAD_AGREE_TERMS=false", "      - WEREAD_AGREE_TERMS=false\n"),
        ("EMAIL_TO", "      - EMAIL_TO="),
    ]:
        n = text.count(needle)
        if n != 1:
            raise RuntimeError("%s: expected exactly 1 occurrence, found %d" % (label, n))
    if "WXPUSH_URL" in text or "WEREAD_API_PORT" in text:
        raise RuntimeError("compose already contains WXPUSH_URL / WEREAD_API_PORT - refusing to double-patch")

    text = text.replace(OLD_IMAGE, NEW_IMAGE, 1)
    log.append("image -> weread-challenge:local")
    text = text.replace(OLD_PULL, NEW_PULL, 1)
    log.append("pull_policy -> never")

    # Insert at the end of the app's environment list (before '    volumes:').
    lines = text.splitlines(keepends=True)
    try:
        app_i = next(i for i, l in enumerate(lines) if l.rstrip("\r\n") == "  app:")
    except StopIteration:
        raise RuntimeError("cannot find '  app:' service block")
    try:
        env_i = next(i for i in range(app_i + 1, len(lines))
                     if lines[i].rstrip("\r\n") == "    environment:")
    except StopIteration:
        raise RuntimeError("cannot find app 'environment:' block")
    end_i = env_i + 1
    while end_i < len(lines) and lines[end_i].startswith("      - "):
        end_i += 1
    if end_i == env_i + 1:
        raise RuntimeError("app environment block is empty")
    lines[end_i:end_i] = ADD_LINES
    log.append("inserted %s at end of app environment" %
               ", ".join(x.strip().lstrip("- ").split("=")[0] for x in ADD_LINES))
    return "".join(lines), log


def validate(new_text):
    checks = {
        "WEREAD_AGREE_TERMS=false kept": "      - WEREAD_AGREE_TERMS=false" in new_text,
        "selenium image untouched": "selenium/standalone-chromium:latest" in new_text,
        "selenium pull_policy untouched": "    pull_policy: if_not_present" in new_text,
        "email config kept": all(k in new_text for k in
                                 ["EMAIL_SMTP", "EMAIL_PORT", "EMAIL_USER",
                                  "EMAIL_PASS", "EMAIL_FROM", "EMAIL_TO",
                                  "ENABLE_EMAIL=true"]),
        "dns kept": new_text.count("      - 223.5.5.5") == 2,
        "WEREAD_DURATION exactly once (Script D)": len(re.findall(r"^\s*- WEREAD_DURATION=", new_text, re.M)) == 1,
        "WXPUSH_URL exactly once": new_text.count("WXPUSH_URL") == 1,
        "WEREAD_API_PORT exactly once": new_text.count("WEREAD_API_PORT") == 1,
        "no WXPUSH_TOKEN / BARK leftovers": "WXPUSH_TOKEN" not in new_text and "BARK_KEY" not in new_text,
    }
    failed = [k for k, v in checks.items() if not v]
    for k, v in checks.items():
        print("  %s %s" % ("OK  " if v else "FAIL", k))
    if failed:
        raise RuntimeError("compose validation failed: %s" % ", ".join(failed))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    c = connect()
    sftp = c.open_sftp()
    try:
        with sftp.open(COMPOSE, "r") as f:
            original = f.read().decode("utf-8")
        print("CURRENT compose: %d bytes  sha256=%s" %
              (len(original.encode()), hashlib.sha256(original.encode()).hexdigest()))

        if args.verify_only:
            print("\n--- current compose (EMAIL_PASS redacted) ---")
            print(re.sub(r"(EMAIL_PASS=).*", r"\1<REDACTED>", original))
            return 0

        new_text, log = patch(original)
        print("\n=== PLANNED CHANGES ===")
        for x in log:
            print("  * " + x)
        print("\n=== STRUCTURAL VALIDATION ===")
        validate(new_text)

        print("\n=== DIFF (EMAIL_PASS redacted) ===")
        import difflib
        for line in difflib.unified_diff(
                re.sub(r"(EMAIL_PASS=).*", r"\1<REDACTED>", original).splitlines(),
                re.sub(r"(EMAIL_PASS=).*", r"\1<REDACTED>", new_text).splitlines(),
                fromfile="before", tofile="after", lineterm=""):
            print(line)

        if args.dry_run:
            print("\nDRY RUN - nothing written")
            return 0

        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = "/root/weread-wxpush-backup-%s" % ts
        rc, out, err = run(c, "mkdir -p %s && cp -a %s %s/compose.yaml && sha256sum %s/compose.yaml"
                           % (backup_dir, COMPOSE, backup_dir, backup_dir), timeout=60)
        if rc != 0:
            raise RuntimeError("backup failed: %s %s" % (out, err))
        print("\nBACKUP: %s/compose.yaml" % backup_dir)

        # Stage to /tmp, validate BEFORE replacing the live file.
        tmp = "/tmp/compose.yaml.wxpush-new"
        with sftp.open(tmp, "w") as f:
            f.write(new_text.encode("utf-8"))
        rc, out, err = run(c, "cd %s && docker compose -f %s config -q && echo CONFIG_OK" % (STACK, tmp), timeout=120)
        if rc != 0 or "CONFIG_OK" not in out:
            raise RuntimeError("staged compose failed 'docker compose config -q': %s %s" % (out, err))
        print("STAGED compose config -q: OK")

        rc, out, err = run(c, "mv %s %s && chmod 644 %s" % (tmp, COMPOSE, COMPOSE), timeout=60)
        if rc != 0:
            raise RuntimeError("cannot install compose: %s" % err)

        rc, out, err = run(c, "cd %s && docker compose -f compose.yaml config -q && echo CONFIG_OK" % STACK,
                           timeout=120)
        if rc != 0 or "CONFIG_OK" not in out:
            raise RuntimeError("live compose failed validation: %s %s" % (out, err))
        print("LIVE compose config -q: OK")

        print("\n=== FINAL compose (EMAIL_PASS redacted) ===")
        with sftp.open(COMPOSE, "r") as f:
            final = f.read().decode("utf-8")
        print(re.sub(r"(EMAIL_PASS=).*", r"\1<REDACTED>", final))
        print("\nFINAL sha256=%s" % hashlib.sha256(final.encode()).hexdigest())
        print("ROLLBACK: cp -a %s/compose.yaml %s" % (backup_dir, COMPOSE))
        return 0
    finally:
        sftp.close()
        c.close()


if __name__ == "__main__":
    sys.exit(main())
