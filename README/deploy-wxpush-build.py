#!/usr/bin/env python3
"""Build a local weread-challenge image from the user's patched source.

Scope (CT 107 only):
  - upload 4 build-context files to /opt/weread-build (SHA256-verified)
  - docker build -t weread-challenge:local
  - verify the image contains the patched code and passes node --check

Does NOT touch: compose.yaml, Script C/D, plan.json, running containers,
PVE host, or the user's git repository.

Usage:
    python deploy-wxpush-build.py --dry-run       # show the plan, no connection
    python deploy-wxpush-build.py --verify-only   # read-only checks on CT 107
    python deploy-wxpush-build.py                 # upload + build + verify
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

import paramiko

HOST = "192.168.91.130"
USER = "root"
PWD = os.environ.get("CT107_PWD", "2900880")

ROOT = Path(__file__).resolve().parent
# All four files live in one stable workspace build context. The Downloads
# attachment and the user's git repo are NOT read at deploy time.
BUILD = ROOT / "weread-build"
PATCHED_JS = BUILD / "src" / "weread-challenge.js"

REMOTE_ROOT = "/opt/weread-build"
IMAGE = "weread-challenge:local"

FILES = [
    ("Dockerfile", BUILD / "Dockerfile", "/opt/weread-build/Dockerfile", 0o644),
    ("package.json", BUILD / "package.json", "/opt/weread-build/package.json", 0o644),
    ("package-lock.json", BUILD / "package-lock.json", "/opt/weread-build/package-lock.json", 0o644),
    ("src/weread-challenge.js", PATCHED_JS, "/opt/weread-build/src/weread-challenge.js", 0o644),
]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=22, username=USER, password=PWD, timeout=15,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, timeout=600):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    rc = out.channel.recv_exit_status()
    return rc, out.read().decode("utf-8", "replace").strip(), \
        err.read().decode("utf-8", "replace").strip()


def show(title, rc, out, err, tail=30):
    print("\n--- %s (rc=%d) ---" % (title, rc))
    lines = [x for x in (out + ("\n[stderr] " + err if err else "")).splitlines() if x.strip()]
    for line in lines[-tail:]:
        print(line)
    return rc


def upload(c, sftp, local, remote, mode, idx):
    data = local.read_bytes()
    if not data:
        raise RuntimeError("refusing to upload empty file: %s" % local)
    tmp = "/tmp/wxpush-build-%d-%s" % (idx, local.name)
    with sftp.open(tmp, "w") as f:
        f.write(data)
    rc, out, err = run(c, "test -s %s && sha256sum %s | cut -d' ' -f1" % (tmp, tmp), timeout=60)
    if rc != 0 or out != digest(data):
        raise RuntimeError("SHA256 mismatch for %s: remote=%r local=%s"
                           % (remote, out, digest(data)))
    rc, out, err = run(c, "mv %s %s && chmod %o %s" % (tmp, remote, mode, remote), timeout=60)
    if rc != 0:
        raise RuntimeError("cannot install %s: %s" % (remote, err))
    print("  %-28s %s  (%d bytes)" % (remote.split("/opt/weread-build/")[-1], digest(data)[:16], len(data)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    if args.dry_run:
        print("DRY RUN - no connection")
        for label, local, remote, mode in FILES:
            print("  %s -> %s  sha256=%s" % (local, remote, digest(local.read_bytes())))
        print("  docker build -t %s %s" % (IMAGE, REMOTE_ROOT))
        return 0

    c = connect()
    sftp = c.open_sftp()
    try:
        if args.verify_only:
            show("existing build context", *run(c, "ls -la %s %s/src" % (REMOTE_ROOT, REMOTE_ROOT)))
            show("image", *run(c, "docker image inspect %s --format '{{.Id}} {{.Created}} {{.Size}}' 2>&1" % IMAGE))
            return 0

        print("=== 1. prepare build context ===")
        rc, out, err = run(c, "mkdir -p %s/src && echo OK" % REMOTE_ROOT, timeout=60)
        if rc != 0 or "OK" not in out:
            raise RuntimeError("cannot create build dir: %s %s" % (out, err))
        print("  %s/src ready" % REMOTE_ROOT)

        print("\n=== 2. upload + SHA256 verify ===")
        for idx, (label, local, remote, mode) in enumerate(FILES):
            upload(c, sftp, local, remote, mode, idx)

        print("\n=== 3. docker build (this can take a few minutes) ===")
        build_cmd = (
            "cd %s && docker build -t %s . > /tmp/wxpush-build.log 2>&1; "
            "rc=$?; tail -25 /tmp/wxpush-build.log; echo BUILD_RC=$rc; exit $rc"
            % (REMOTE_ROOT, IMAGE)
        )
        rc, out, err = run(c, build_cmd, timeout=600)
        show("docker build", rc, out, err, tail=30)
        if rc != 0:
            raise RuntimeError("docker build failed (rc=%d)" % rc)

        print("\n=== 4. verify image contents ===")
        # Use --entrypoint to avoid nested shell quoting entirely.
        checks = [
            ("image id / size",
             "docker image inspect %s --format '{{.Id}} | {{.Created}} | {{.Size}} bytes'" % IMAGE,
             None),
            ("node --check app.js",
             "docker run --rm --entrypoint node %s --check /app/app.js && echo SYNTAX_OK" % IMAGE,
             "SYNTAX_OK"),
            ("url patch present",
             "docker run --rm --entrypoint grep %s -c 'wxpush地址无效' /app/app.js" % IMAGE,
             "1"),
            ("try patch present",
             "docker run --rm --entrypoint grep %s -c '放在 try 内' /app/app.js" % IMAGE,
             "1"),
            ("old endpoint bug gone",
             "docker run --rm --entrypoint grep %s -c 'endpoint += \"/wxsend\"' /app/app.js || true" % IMAGE,
             "0"),
            ("sendWxpush occurrences",
             "docker run --rm --entrypoint grep %s -c 'sendWxpush' /app/app.js" % IMAGE,
             None),
            ("app.js sha256 == local patched js",
             "docker run --rm --entrypoint sha256sum %s /app/app.js" % IMAGE,
             None),
        ]
        local_hash = digest(PATCHED_JS.read_bytes())
        for title, cmd, expect in checks:
            rc, out, err = run(c, cmd, timeout=180)
            show(title, rc, out, err, tail=8)
            if title.startswith("app.js sha256"):
                if local_hash not in out:
                    raise RuntimeError("image app.js does not match local patched source")
                print("  MATCH local=%s" % local_hash[:16])
            elif expect is not None and expect not in out:
                raise RuntimeError("verification failed: %s (expected %r, got %r)" % (title, expect, out))
            elif rc != 0:
                raise RuntimeError("verification command failed: %s (rc=%d)" % (title, rc))

        print("\nBUILD DEPLOY OK - image=%s  context=%s" % (IMAGE, REMOTE_ROOT))
        return 0
    finally:
        sftp.close()
        c.close()


if __name__ == "__main__":
    sys.exit(main())
