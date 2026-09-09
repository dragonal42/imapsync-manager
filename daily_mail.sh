#!/bin/bash
CONFIG_FILE="/app/data/config.json"
REPORT_FILE="/app/data/daily_errors.log"

if [ -f "$CONFIG_FILE" ] && [ -f "$REPORT_FILE" ] && [ -s "$REPORT_FILE" ]; then
    DEST_EMAIL=$(python3 -c "import json; cfg = json.load(open('$CONFIG_FILE')); print(cfg.get('report_email', ''))")
    
    if [ -n "$DEST_EMAIL" ] && [ "$DEST_EMAIL" != "" ]; then
        echo -e "Subject: IMAPSync - Rapport d'erreurs quotidien\n\nVoici les erreurs enregistrees :\n\n$(cat $REPORT_FILE)" | msmtp --account=default "$DEST_EMAIL"
    fi
    > "$REPORT_FILE"
fi
