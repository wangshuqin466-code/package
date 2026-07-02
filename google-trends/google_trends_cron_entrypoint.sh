#!/bin/sh
set -eu

mkdir -p /logs /cache_archive
touch /logs/google_trends_cron.log

cat >/etc/cron.d/google-trends <<EOF
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TZ=Asia/Shanghai
0 3 * * * root cd /app && /bin/sh /app/google_trends_docker_job.sh >> /logs/google_trends_cron.log 2>&1
EOF

chmod 0644 /etc/cron.d/google-trends
crontab /etc/cron.d/google-trends

echo "Google Trends cron container started at $(date -Iseconds)" | tee -a /logs/google_trends_cron.log
echo "Schedule: daily at 03:00 Asia/Shanghai" | tee -a /logs/google_trends_cron.log

cron -f
