# /etc/cron.d/pve-power-schedule  (scheduler for Script A)
# Daily power schedule for PVE guests on node "Router" (192.168.91.254).
#
# System timezone is Asia/Shanghai (CST, +0800) with active NTP sync,
# so cron interprets the times below as LOCAL time. No UTC conversion needed.
#
# Never shut down        : VM 100 (HA router), CT 106 (EasyTier)
# 00:00 shutdown targets : all guests except the EXCLUDE_*_STOP lists
# 09:00 startup  targets : all guests except the EXCLUDE_*_START lists
#                          (VM 103/104/109 and CT 107 are shutdown-only)
# Guests are discovered dynamically, so a NEW VM/CT is auto-managed.
# CT 108 is gone. CT 105/110 fully script-managed; CT 107 shutdown-only.
#
# The script writes its own log to /var/log/pve-power-schedule.log. stdout is
# discarded; stderr is appended to /var/log/pve-power-schedule.err so failures
# outside the script's own logger (syntax error, killed process, skipped pass)
# remain visible. Both files are rotated weekly by logrotate.
# Debian cron picks up this file automatically within one minute
# (no service restart required).
#
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
#
# .---------------- minute (0-59)
# |  .------------- hour   (0-23)
# |  |  .---------- day of month (1-31)
# |  |  |  .------- month (1-12)
# |  |  |  |  .---- day of week (0-7, 0 and 7 = Sunday)
# |  |  |  |  |
# m h dom mon dow user  command
0 0 * * * root /usr/local/sbin/pve-power-schedule.sh stop  >/dev/null 2>>/var/log/pve-power-schedule.err
0 9 * * * root /usr/local/sbin/pve-power-schedule.sh start >/dev/null 2>>/var/log/pve-power-schedule.err
