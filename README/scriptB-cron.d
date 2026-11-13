# /etc/cron.d/pve-ct107-random-start  (scheduler for Script B)
# Script B starts CT 107 once per day at a Gaussian-random moment between
# 08:00 and 08:30 CST. 08:10 is the MODE (most frequent moment), NOT the
# mean: mu=10min, sigma=6min jitter, truncated to the [0,30] window.
# PVE host TZ is Asia/Shanghai -> cron times are LOCAL, no UTC conversion.
# 07:59 plan generates the daily plan; 08:00-08:30 launch polls each minute.
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
59 7 * * * root /usr/local/sbin/pve-ct107-random-start.py plan   >> /var/log/ct107-random-start.log 2>&1
0-30 8 * * * root /usr/local/sbin/pve-ct107-random-start.py launch >> /var/log/ct107-random-start.log 2>&1
