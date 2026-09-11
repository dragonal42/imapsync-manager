#!/bin/bash
export SMTP_STARTTLS=on
if [ "${SMTP_SECURITY:-starttls}" = "ssl" ]; then
    export SMTP_STARTTLS=off
fi
envsubst < /app/msmtp.conf.template > /etc/msmtprc
chmod 600 /etc/msmtprc
chmod +x /app/daily_mail.sh

# Configuration de la tâche cron quotidienne à 23h59
echo "59 23 * * * /app/daily_mail.sh" > /etc/cron.d/imapsync-cron
chmod 0644 /etc/cron.d/imapsync-cron
crontab /etc/cron.d/imapsync-cron
cron

# One worker owns the JSON transactions and scheduler. Query strings contain
# one-time credentials; do not include them in HTTP access logs.
exec uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1 --no-access-log --proxy-headers
