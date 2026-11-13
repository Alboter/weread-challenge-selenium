#!/usr/bin/env python3
"""Read-only probe: fetch the WeChat test-account template definition.

Uploads a small shell/python script to CT 107, runs it there (so appid/secret
never leave the server and never appear in our output), then removes it.
Nothing is modified on the server.
"""

import paramiko

HOST = "192.168.91.130"
USER = "root"
PWD = "2900880"
REMOTE = "/tmp/probe-wx-template.sh"

SCRIPT = r'''#!/bin/bash
set -u
ARGS=$(docker inspect go-wxpush --format '{{join .Config.Cmd " "}}' 2>/dev/null)
APPID=$(echo "$ARGS"  | sed -nE 's/.*-appid ([^ ]+).*/\1/p')
SECRET=$(echo "$ARGS" | sed -nE 's/.*-secret ([^ ]+).*/\1/p')
TEMPLATE=$(echo "$ARGS" | sed -nE 's/.*-template_id ([^ ]+).*/\1/p')
python3 - "$APPID" "$SECRET" "$TEMPLATE" <<'PY'
import json, ssl, sys, urllib.request, urllib.parse

appid, secret, template_id = sys.argv[1], sys.argv[2], sys.argv[3]
print("appid_present   :", bool(appid))
print("secret_present  :", bool(secret))
print("template_id     :", (template_id[:6] + "..." if template_id else "<none>"))

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE   # upstream go-wxpush also uses InsecureSkipVerify

def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20, context=ctx) as r:
        return json.loads(r.read().decode())

def get(url):
    with urllib.request.urlopen(url, timeout=20, context=ctx) as r:
        return json.loads(r.read().decode())

try:
    tok = post("https://api.weixin.qq.com/cgi-bin/stable_token",
               {"grant_type": "client_credential", "appid": appid,
                "secret": secret, "force_refresh": False})
    print("token_response  :", "errcode=%s errmsg=%s" % (tok.get("errcode"), tok.get("errmsg"))
          if tok.get("errcode") else "OK token_len=%d" % len(tok.get("access_token", "")))
    token = tok.get("access_token", "")
except Exception as e:
    print("token_error     :", type(e).__name__, str(e)[:200])
    token = ""

if token:
    for api in ["get_all_private_template", "get_all_template"]:
        url = ("https://api.weixin.qq.com/cgi-bin/template/%s?access_token=%s"
               % (api, urllib.parse.quote(token)))
        try:
            d = get(url)
            print("\n--- %s ---" % api)
            print(json.dumps(d, ensure_ascii=False, indent=2)[:4000])
        except Exception as e:
            print("\n--- %s ERROR --- %s %s" % (api, type(e).__name__, str(e)[:200]))
PY
'''


def main():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, 22, USER, PWD, timeout=15, look_for_keys=False, allow_agent=False)
    try:
        s = c.open_sftp()
        with s.open(REMOTE, "w") as f:
            f.write(SCRIPT.encode("utf-8"))
        s.close()
        _, out, err = c.exec_command("bash %s 2>&1; rm -f %s" % (REMOTE, REMOTE), timeout=90)
        print(out.read().decode("utf-8", "replace"))
        e = err.read().decode("utf-8", "replace").strip()
        if e:
            print("STDERR:", e[:500])
    finally:
        c.close()


if __name__ == "__main__":
    main()
