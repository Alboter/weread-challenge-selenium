#!/usr/bin/env python3
"""One real WeChat push to verify the whole chain.

Runs a one-shot container via `docker compose run`, so the image, environment
and network ALL come from the live compose.yaml -- exactly what Script C will
use tomorrow. It overrides the entrypoint to execute a small verifier that
extracts sendWxpush() from the IMAGE's own /app/app.js and sends ONE message:

    patched image -> compose env -> weread-challenge_default network
      -> go-wxpush :5566/wxsend -> api.weixin.qq.com -> your WeChat

Does NOT run the reading flow, does NOT create/recreate the app service,
does NOT touch plan.json or Script C/D. The verifier container is --rm.

Usage:
    python deploy-wxpush-verify.py --verify-only   # show plan, read-only
    python deploy-wxpush-verify.py                 # send one real message
"""

import argparse
import os
import sys
from datetime import datetime

import paramiko

HOST = "192.168.91.130"
USER = "root"
PWD = os.environ.get("CT107_PWD", "2900880")
STACK = "/opt/stacks/weread-challenge"
REMOTE_JS = "/tmp/weread-verify-push.js"

VERIFIER = r'''
const fs = require("fs");
const vm = require("vm");
const http = require("http");
const https = require("https");

const src = fs.readFileSync("/app/app.js", "utf8");
const a = src.indexOf("function isWxpushEnabled()");
const b = src.indexOf("function quoteShellArg", a);
if (a < 0 || b < 0) {
  console.log(JSON.stringify({ ok: false, error: "cannot locate wxpush block in image app.js" }));
  process.exit(2);
}
const snippet = src.slice(a, b);

const logs = [];
const sandbox = {
  WXPUSH_URL: process.env.WXPUSH_URL || "",
  WXPUSH_BASE_URL: process.env.WXPUSH_BASE_URL || "",
  BARK_KEY: "",
  ENABLE_EMAIL: false,
  http, https, Buffer, URL,
  console: {
    info: (...x) => logs.push("INFO " + x.join(" ")),
    error: (...x) => logs.push("ERROR " + x.join(" ")),
  },
};
vm.createContext(sandbox);
vm.runInContext(snippet, sandbox);

function probe(url) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const mod = u.protocol === "https:" ? https : http;
    const req = mod.request(
      { hostname: u.hostname, port: u.port || (u.protocol === "https:" ? 443 : 80),
        path: "/", method: "GET", timeout: 6000 },
      (res) => { res.resume(); res.on("end", () => resolve(res.statusCode)); }
    );
    req.on("timeout", () => { req.destroy(); resolve("TIMEOUT"); });
    req.on("error", (e) => resolve("ERROR " + e.message));
    req.end();
  });
}

(async () => {
  const endpoint = process.env.WXPUSH_URL || "";
  const probeStatus = await probe(endpoint);
  const title = "微信读书挑战 - 部署验证";
  const content = "全链路推送验证成功：自建镜像 → compose 网络 → go-wxpush → 微信。这是一条测试消息，无需处理。";
  const ok = await sandbox.sendWxpush(title, content);
  console.log(JSON.stringify({
    ok,
    endpoint,
    api_port: process.env.WEREAD_API_PORT || null,
    probe_root_status: probeStatus,
    enabled: sandbox.isWxpushEnabled(),
    logs,
    at: new Date().toISOString(),
  }, null, 2));
  process.exit(ok ? 0 : 1);
})();
'''


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, port=22, username=USER, password=PWD, timeout=15,
              look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, timeout=180):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    print("TARGET : one real WeChat message via go-wxpush")
    print("CHAIN  : compose image -> compose env -> weread-challenge_default")
    print("         -> http://192.168.91.130:5566/wxsend -> api.weixin.qq.com")
    print("TIME   : %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if args.verify_only:
        return 0

    c = connect()
    sftp = c.open_sftp()
    try:
        print("\n=== 0. pre-flight (read-only) ===")
        rc, out, err = run(c,
            "cd %s && docker compose config | grep -E 'image:|pull_policy:|WXPUSH_URL|WEREAD_API_PORT|WEREAD_DURATION|WEREAD_AGREE_TERMS'"
            % STACK, timeout=60)
        show("effective compose config", rc, out, err)

        rc, out, err = run(c, "sha256sum /var/lib/weread-duration/plan.json; "
                              "docker compose -f %s/compose.yaml ps -a --format '{{.Names}} | {{.Status}}'"
                              % STACK, timeout=60)
        show("plan.json + service state BEFORE", rc, out, err)
        before_plan = out.splitlines()[0] if out else ""

        print("\n=== 1. upload verifier to %s ===" % REMOTE_JS)
        with sftp.open(REMOTE_JS, "w") as f:
            f.write(VERIFIER.encode("utf-8"))
        print("  %d bytes written" % len(VERIFIER.encode("utf-8")))

        print("\n=== 2. run ONE push through the live compose stack ===")
        cmd = (
            "cd %s && docker compose run --rm --no-deps --entrypoint node "
            "-v %s:/verify.js:ro app /verify.js 2>&1; rc=$?; echo VERIFY_RC=$rc; exit $rc"
            % (STACK, REMOTE_JS)
        )
        rc, out, err = run(c, cmd, timeout=180)
        show("real push", rc, out, err)

        print("\n=== 3. post-flight (must be unchanged) ===")
        rc2, out2, err2 = run(c, "sha256sum /var/lib/weread-duration/plan.json; "
                               "docker compose -f %s/compose.yaml ps -a --format '{{.Names}} | {{.Status}}'"
                               % STACK, timeout=60)
        show("plan.json + service state AFTER", rc2, out2, err2)
        after_plan = out2.splitlines()[0] if out2 else ""
        if before_plan and before_plan != after_plan:
            print("  WARNING: plan.json changed during verification!")
        else:
            print("  plan.json UNCHANGED")

        rc3, out3, err3 = run(c, "rm -f %s && echo CLEANED" % REMOTE_JS, timeout=30)
        print("  verifier file: %s" % (out3 or "cleanup issue"))

        if rc != 0:
            print("\nVERIFY FAILED (rc=%d). See logs above; no reading task was started." % rc)
            return 1
        print("\nVERIFY OK - one real WeChat message was delivered.")
        return 0
    finally:
        sftp.close()
        c.close()


if __name__ == "__main__":
    sys.exit(main())
