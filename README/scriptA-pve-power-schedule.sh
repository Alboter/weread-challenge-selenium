#!/usr/bin/env bash
#
# pve-power-schedule.sh -- Daily guest power schedule for Proxmox VE
#
# Host : 192.168.91.254 (node "Router"), PVE 9.2.25 / Debian 13 (trixie)
# TZ   : Asia/Shanghai (CST, +0800) -- cron interprets schedule as LOCAL time
#
#   /etc/cron.d/pve-power-schedule:
#     00:00  ./pve-power-schedule.sh stop
#     09:00  ./pve-power-schedule.sh start
#
# Script A -- daily guest power schedule (00:00 stop / 09:00 start, CST).
#   Name : /usr/local/sbin/pve-power-schedule.sh
#   Cron : /etc/cron.d/pve-power-schedule
#   Log  : /var/log/pve-power-schedule.log
#   Note : CT 105/110 full script control; CT 107 shutdown-only. (2026-11-12)
#
# Exclusion rules (updated 2026-11-12):
#   Never shut down      : VM 100 (Openwrt, HA router), CT 106 (EasyTier)
#   Shutdown at 00:00    : all guests except EXCLUDE_QM_STOP / EXCLUDE_PCT_STOP
#   Start    at 09:00    : all guests except EXCLUDE_QM_START / EXCLUDE_PCT_START
#                          (VM 103 / 104 / 109 and CT 107 are "shutdown only")
#   CT 105 / CT 110 are fully script-managed: stop 00:00, start 09:00.
#
# NOTE: guests are discovered dynamically with "qm list" / "pct list", so ANY
#       newly created VM/CT is automatically managed. CT 108 has been removed.
#       Add an ID to the EXCLUDE_* lists below if it must stay up 24x7.
#
# NOTE: VM 103 is currently stopped with onboot=0. If you start it by hand,
#       it will still be shut down at 00:00, but never started at 09:00.
#
# Usage:
#   pve-power-schedule.sh status   # print target list + planned actions, NO changes
#   pve-power-schedule.sh stop     # shutdown pass (cron 00:00)
#   pve-power-schedule.sh start    # startup  pass (cron 09:00)
#
# WARNING: FORCE_STOP=1 powers off an unresponsive guest with "qm stop" /
#          "pct stop" after the graceful timeout. This is equivalent to
#          pulling the power plug: unsaved guest data may be lost.
#          Set FORCE_STOP=0 to only log a WARN instead.
#
set -uo pipefail

export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

readonly LOG_FILE=/var/log/pve-power-schedule.log
readonly LOCK_FILE=/run/pve-power-schedule.lock
readonly SHUTDOWN_TIMEOUT=90    # seconds allowed for a graceful ACPI shutdown
readonly FORCE_STOP=1           # 1 = hard stop after timeout (user approved)
readonly START_INTERVAL=5       # seconds between two guest starts

readonly EXCLUDE_QM_STOP="100"
readonly EXCLUDE_PCT_STOP="106"
readonly EXCLUDE_QM_START="100 103 104 109"
readonly EXCLUDE_PCT_START="106 107"

ACTION="${1:-status}"

log() {
    local level="$1" msg="$2" line
    line="$(date '+%Y-%m-%d %H:%M:%S %z') [${level}] [${ACTION}] ${msg}"
    printf '%s\n' "$line" >>"$LOG_FILE" 2>/dev/null || true
    printf '%s\n' "$line"
}

is_excluded() {
    local id="$1" list="$2" item
    for item in $list; do
        [ "$item" = "$id" ] && return 0
    done
    return 1
}

# Print "type vmid" lines sorted by vmid, e.g. "qm 101", "pct 105".
all_guests() {
    {
        qm list 2>/dev/null | awk 'NR>1 && $1 ~ /^[0-9]+$/ {print "qm", $1}'
        pct list 2>/dev/null | awk 'NR>1 && $1 ~ /^[0-9]+$/ {print "pct", $1}'
    } | sort -k2,2n
}

guest_status() {
    "$1" status "$2" 2>/dev/null \
        | awk '/^status:/ {sub(/^status:[[:space:]]*/, ""); print; exit}'
}

guest_name() {
    if [ "$1" = qm ]; then
        qm config "$2" 2>/dev/null | awk '/^name:/ {print $2; exit}'
    else
        pct config "$2" 2>/dev/null | awk '/^hostname:/ {print $2; exit}'
    fi
}

exclusions_for() {
    if [ "$ACTION" = start ]; then
        [ "$1" = qm ] && echo "$EXCLUDE_QM_START" || echo "$EXCLUDE_PCT_START"
    else
        [ "$1" = qm ] && echo "$EXCLUDE_QM_STOP" || echo "$EXCLUDE_PCT_STOP"
    fi
}

show_status() {
    local type id name st s_ex b_ex a_stop a_start
    printf '%-4s %-6s %-18s %-9s %-12s %s\n' \
        TYPE VMID NAME STATUS AT-00:00 AT-09:00
    printf '%-4s %-6s %-18s %-9s %-12s %s\n' \
        ---- ------ ------------------ -------- ------------ --------
    while read -r type id; do
        [ -n "$id" ] || continue
        name="$(guest_name "$type" "$id")"
        st="$(guest_status "$type" "$id")"
        if [ "$type" = qm ]; then
            s_ex="$EXCLUDE_QM_STOP"
            b_ex="$EXCLUDE_QM_START"
        else
            s_ex="$EXCLUDE_PCT_STOP"
            b_ex="$EXCLUDE_PCT_START"
        fi
        a_stop=SHUTDOWN
        a_start=START
        is_excluded "$id" "$s_ex" && a_stop=KEEP-ON
        is_excluded "$id" "$b_ex" && a_start=SKIP
        printf '%-4s %-6s %-18s %-9s %-12s %s\n' \
            "$type" "$id" "${name:--}" "${st:-unknown}" "$a_stop" "$a_start"
    done < <(all_guests)
}

run_pass() {
    local type id excl st rc out
    while read -r type id; do
        [ -n "$id" ] || continue
        excl="$(exclusions_for "$type")"
        if is_excluded "$id" "$excl"; then
            log INFO "$type:$id EXCLUDED from ${ACTION} (left untouched)"
            continue
        fi

        st="$(guest_status "$type" "$id")"

        if [ "$ACTION" = stop ]; then
            if [ "$st" != "running" ]; then
                log INFO "$type:$id already ${st:-unknown}, skip shutdown"
                continue
            fi
            log INFO "$type:$id graceful shutdown (timeout ${SHUTDOWN_TIMEOUT}s) ..."
            "$type" shutdown "$id" --timeout "$SHUTDOWN_TIMEOUT" --forceStop 0 \
                >/dev/null 2>&1
            rc=$?
            sleep 2
            st="$(guest_status "$type" "$id")"
            if [ "$st" != "running" ]; then
                log INFO "$type:$id stopped gracefully (rc=$rc)"
                continue
            fi
            if [ "$FORCE_STOP" -eq 1 ]; then
                log WARN "$type:$id still running after ${SHUTDOWN_TIMEOUT}s -> FORCE STOP (unsaved data may be lost)"
                if [ "$type" = qm ]; then
                    out="$(qm stop "$id" --overrule-shutdown 1 2>&1)"
                    rc=$?
                else
                    out="$(pct stop "$id" 2>&1)"
                    rc=$?
                fi
                sleep 2
                st="$(guest_status "$type" "$id")"
                if [ "$st" != "running" ]; then
                    log INFO "$type:$id forced stop OK (rc=$rc)"
                else
                    log ERROR "$type:$id STILL RUNNING after forced stop (rc=$rc): $(printf '%s' "$out" | tr '\n' ' ')"
                fi
            else
                log WARN "$type:$id still running, left as-is (FORCE_STOP=0)"
            fi
        else
            if [ "$st" = "running" ]; then
                log INFO "$type:$id already running, skip start"
                continue
            fi
            log INFO "$type:$id starting ..."
            out="$("$type" start "$id" 2>&1)"
            rc=$?
            if [ "$rc" -eq 0 ]; then
                log INFO "$type:$id start OK"
            else
                log ERROR "$type:$id start FAILED (rc=$rc): $(printf '%s' "$out" | tr '\n' ' ')"
            fi
            sleep "$START_INTERVAL"
        fi
    done < <(all_guests)
}

case "$ACTION" in
    status|dry-run)
        show_status
        exit 0
        ;;
    stop|start)
        ;;
    *)
        printf 'Usage: %s {status|stop|start}\n' "$0" >&2
        exit 2
        ;;
esac

# Serialize: never let two passes run at the same time.
exec 9>"$LOCK_FILE" 2>/dev/null || {
    printf 'ERROR: cannot open lock file %s\n' "$LOCK_FILE" >&2
    exit 1
}
flock -n 9 || {
    log WARN "another instance is running, skip this pass"
    printf '%s [WARN] another instance is running, skip this pass\n' \
        "$(date '+%Y-%m-%d %H:%M:%S %z')" >&2
    exit 0
}

# FIX 2026-11-10 -- never leak the lock fd to guest processes.
# fd 9 is inherited by EVERY child, including the long-lived qemu / lxc-start
# processes spawned by "qm start" / "pct start". Such a child outlives this
# script and keeps the flock held, so the next cron pass (e.g. 00:00 stop)
# gets "another instance is running" and shuts nothing down.
# The subshell drops fd 9 before doing any work, while the parent keeps
# holding the lock for the whole pass: concurrency protection is unchanged.
log INFO "=== ${ACTION} begin: node=$(hostname) tz=$(date +%Z) force_stop=${FORCE_STOP} timeout=${SHUTDOWN_TIMEOUT}s ==="
(
    exec 9>&-
    run_pass
)
log INFO "=== ${ACTION} done ==="
exit 0
