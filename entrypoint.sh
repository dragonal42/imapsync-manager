#!/bin/bash
envsubst < /app/msmtp.conf.template > /etc/msmtprc
chmod 600 /etc/msmtprc

cat << 'EOF' > /app/daily_mail.sh
#!/bin/bash
CONFIG_FILE="/app/data/config.json"
REPORT_FILE="/app/data/daily_errors.log"
if [ -f "$CONFIG_FILE" ] && [ -f "$REPORT_FILE" ] && [ -s "$REPORT_FILE" ]; then
    DEST_EMAIL=$(python3 -c "import json; print(json.load(open('$CONFIG_FILE')).get('report_email', ''))")
    if [ -n "$DEST_EMAIL" ]; then
        echo -e "Subject: IMAPSync - Rapport d'erreurs quotidien\n\nVoici les erreurs de la journée :\n\n$(cat $REPORT_FILE)" | msmtp --account=default "$DEST_EMAIL"
    fi
    > "$REPORT_FILE"
fi
EOF

chmod +x /app/daily_mail.sh
echo "59 23 * * * /app/daily_mail.sh" > /etc/cron.d/imapsync-cron
chmod 0644 /etc/cron.d/imapsync-cron
crontab /etc/cron.d/imapsync-cron
cron
exec uvicorn main:app --host 0.0.0.0 --port 8080
