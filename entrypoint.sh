#!/bin/bash
envsubst < /app/msmtp.conf.template > /etc/msmtprc
chmod 600 /etc/msmtprc
chmod +x /app/daily_mail.sh

# Configuration de la tâche cron quotidienne à 23h59
echo "59 23 * * * /app/daily_mail.sh" > /etc/cron.d/imapsync-cron
chmod 0644 /etc/cron.d/imapsync-cron
crontab /etc/cron.d/imapsync-cron
cron

exec uvicorn main:app --host 0.0.0.0 --port 8080
