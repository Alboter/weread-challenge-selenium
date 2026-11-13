#!/usr/bin/env python3
"""Read-only final review of the wxpush / go-wxpush deployment chain.

Checks CT 107 (compose, image, containers, Script C/D, state, backup) and
PVE (Script A/B schedules, logs, guest state). Nothing is written, started,
stopped, recreated or removed.
"""

import os
import sys
import paramiko

CT = dict(host="192.168.91.130", user="root", pwd=os.environ.get("CT107_PWD", "2900880"))
PVE = dict(host="192.168.91.254", user="root", pwd=os.environ.get("PVE_PWD", "qm2900880"))
STACK = "/opt/stacks/weread-challenge"


def connect(cfg):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["host"], port=22, username=cfg["user"], password=cfg["pwd"],
              timeout=15, look_for_keys=False, allow_agent=False)
    return c


def run(c, cmd, timeout=90):
    _, out, err = c.exec_command(cmd, timeout=timeout)
    rc = out.channel.recv_exit_status()
    return rc, out.read().decode("utf-8", "replace").strip(), \
        err.read().decode("utf-8", "replace").strip()


def section(c, title, cmd, timeout=90):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)
    rc, out, err = run(c, cmd, timeout)
    print(out if out else "(no stdout)")
    if err:
        print("[stderr] " + err)
    if rc != 0:
        print("[rc=%d]" % rc)
    return out


def main():
    print("READ-ONLY FINAL REVIEW - wxpush / go-wxpush deployment")
    print("CT107 : %s   PVE : %s" % (CT["host"], PVE["host"]))

    cc = connect(CT)
    try:
        section(cc, "0. Clocks",
                "date '+CT107 local: %Y-%m-%d %H:%M:%S %Z (%z)'; "
                "date -u '+CT107 UTC  : %Y-%m-%d %H:%M:%S'")

        section(cc, "1. Effective compose config (EMAIL_PASS redacted)",
                "cd %s && docker compose config 2>/dev/null | "
                "grep -E 'image:|pull_policy:|WXPUSH_URL|WXPUSH_BASE_URL|WEREAD_API_PORT|"
                "WEREAD_DURATION|WEREAD_AGREE_TERMS|ENABLE_EMAIL|EMAIL_USER|EMAIL_TO' | "
                "sed -E 's/(EMAIL_PASS=).*/\\1<REDACTED>/'" % STACK)

        section(cc, "2. Key file hashes & permissions",
                "sha256sum %s/compose.yaml /var/lib/weread-duration/plan.json "
                "/usr/local/bin/weread-duration.py /etc/systemd/system/weread-boot-start.service "
                "/opt/weread-build/src/weread-challenge.js /opt/weread-build/Dockerfile; "
                "echo '--- perms ---'; "
                "stat -c '%%a %%U:%%G %%s %%n' /usr/local/bin/weread-duration.py "
                "/etc/systemd/system/weread-boot-start.service %s/compose.yaml" % (STACK, STACK))

        section(cc, "3. Images (local build + pinned base + official)",
                "docker images --format '{{.Repository}}:{{.Tag}} | {{.ID}} | {{.Size}} | {{.CreatedSince}}' | "
                "grep -E 'weread' ; "
                "echo '--- image app.js hash (transient --rm container) ---'; "
                "docker run --rm --network none --entrypoint sha256sum "
                "weread-challenge:local /app/app.js 2>&1 | head -1")

        section(cc, "4. Container states",
                "echo '--- weread stack ---'; "
                "docker compose -f %s/compose.yaml ps -a --format '{{.Names}} | {{.Status}} | {{.Image}}'; "
                "echo '--- go-wxpush ---'; "
                "docker ps -a --filter name=go-wxpush --format '{{.Names}} | {{.Status}} | {{.Image}} | {{.Ports}}'; "
                "echo '--- stray verifier containers ---'; "
                "docker ps -a --filter name=verify --format '{{.Names}} | {{.Status}}' ; "
                "echo '--- stray verifier file ---'; "
                "ls -l /tmp/weread-verify-push.js 2>&1 || echo 'GONE (expected)'" % STACK)

        section(cc, "5. app container runtime facts (image / env / lifecycle)",
                "docker inspect weread-challenge-app-1 --format '"
                "Image={{.Config.Image}}{{\"\\n\"}}"
                "Restart={{.HostConfig.RestartPolicy.Name}}{{\"\\n\"}}"
                "Created={{.Created}}{{\"\\n\"}}"
                "StartedAt={{.State.StartedAt}}{{\"\\n\"}}"
                "FinishedAt={{.State.FinishedAt}}{{\"\\n\"}}"
                "ExitCode={{.State.ExitCode}}' ; "
                "echo '--- runtime env (filtered) ---'; "
                "docker inspect weread-challenge-app-1 --format '{{range .Config.Env}}{{println .}}{{end}}' | "
                "grep -E 'WXPUSH|WEREAD_|ENABLE_EMAIL|EMAIL_USER' | "
                "sed -E 's/(EMAIL_PASS=).*/\\1<REDACTED>/'")

        section(cc, "6. Script C unit & Script D state",
                "systemctl is-enabled weread-boot-start.service 2>&1; "
                "systemctl is-active weread-boot-start.service 2>&1; "
                "echo '--- unit Exec lines ---'; "
                "systemctl cat weread-boot-start.service | grep -E 'Type=|RemainAfterExit=|TimeoutStartSec=|ExecStart'; "
                "echo '--- plan.json ---'; "
                "cat /var/lib/weread-duration/plan.json; "
                "echo '--- duration script status ---'; "
                "/usr/local/bin/weread-duration.py status 2>&1 | head -40")

        section(cc, "7. Backup / build context / residual check",
                "echo '--- compose backups ---'; ls -ld /root/weread-wxpush-backup-* 2>&1; "
                "echo '--- build context ---'; ls -l /opt/weread-build /opt/weread-build/src; "
                "echo '--- wxpush-related systemd units / cron ---'; "
                "systemctl list-unit-files --type=service | grep -iE 'weread|wxpush' || true; "
                "crontab -l 2>/dev/null | grep -iE 'weread|wxpush' || echo '(no wxpush cron in CT107 root crontab)'; "
                "echo '--- BARK leftovers in compose ---'; "
                "grep -c 'BARK_' %s/compose.yaml || true" % STACK)

        section(cc, "8. go-wxpush reachability (host + from weread network)",
                "echo -n 'host LAN  : '; curl -s -o /dev/null -w 'HTTP %{http_code}\\n' "
                "--max-time 8 http://192.168.91.130:5566/ ; "
                "echo -n 'bridge gw : '; curl -s -o /dev/null -w 'HTTP %{http_code}\\n' "
                "--max-time 8 http://172.17.0.1:5566/ ; "
                "echo -n 'from selenium (weread net): '; "
                "docker exec weread-challenge-selenium-1 sh -c "
                "\"curl -s -o /dev/null -w 'HTTP %{http_code}' --max-time 8 "
                "http://192.168.91.130:5566/\" 2>&1 || echo '(exec failed)'")

        section(cc, "9. Recent push / duration evidence",
                "echo '--- last app log lines (today already ran on OLD image) ---'; "
                "docker logs --tail 25 weread-challenge-app-1 2>&1 | tail -25; "
                "echo '--- duration log tail ---'; tail -15 /var/log/weread-duration.log 2>/dev/null || true; "
                "echo '--- go-wxpush log tail (redacted) ---'; "
                "docker logs --tail 20 go-wxpush 2>&1 | sed -E 's/(appid|secret|userid|token)=[^ &]+/\\1=<REDACTED>/g' | tail -20")
    finally:
        cc.close()

    pc = connect(PVE)
    try:
        section(pc, "10. PVE Script A/B files, cron, logs",
                "date '+PVE local: %Y-%m-%d %H:%M:%S %Z (%z)'; "
                "echo '--- A/B hashes ---'; "
                "sha256sum /usr/local/sbin/pve-power-schedule.sh "
                "/usr/local/sbin/pve-ct107-random-start.py; "
                "echo '--- A exclude lists ---'; "
                "grep -E '^readonly EXCLUDE_' /usr/local/sbin/pve-power-schedule.sh; "
                "echo '--- B distribution ---'; "
                "grep -E '^(MODE_MIN|SIGMA|LO, HI|MU|CTID) ?=' /usr/local/sbin/pve-ct107-random-start.py; "
                "echo '--- cron.d ---'; "
                "for f in /etc/cron.d/pve-power-schedule /etc/cron.d/pve-ct107-random-start; do "
                "echo \\\"[$f]\\\"; grep -vE '^\\s*(#|$)' $f; done; "
                "echo '--- err log sizes ---'; "
                "ls -l /var/log/pve-power-schedule.err /var/log/ct107-random-start.log 2>&1; "
                "echo '--- A last 8 log lines ---'; tail -8 /var/log/pve-power-schedule.log; "
                "echo '--- B last 8 log lines ---'; tail -8 /var/log/ct107-random-start.log; "
                "echo '--- B plan.json ---'; cat /var/lib/ct107-random-start/plan.json 2>&1")

        section(pc, "11. PVE guest power state",
                "pct list | sed -n '1,20p'; echo '---'; qm list | sed -n '1,20p'; "
                "echo '--- 107 ---'; pct status 107")
    finally:
        pc.close()

    print("\n" + "=" * 72)
    print("REVIEW COMPLETE - read-only, no production changes made")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
