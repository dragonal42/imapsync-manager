#!/bin/bash
CONFIG_FILE="${CONFIG_FILE:-/app/data/config.json}"
REPORT_FILE="${REPORT_FILE:-/app/data/daily_errors.log}"

if [ -f "$CONFIG_FILE" ] && [ -f "$REPORT_FILE" ] && [ -s "$REPORT_FILE" ]; then
    DEST_EMAIL=$(python3 - "$CONFIG_FILE" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as source:
    cfg = json.load(source)
print(cfg.get("report_email") or next((u["email"] for u in cfg.get("users", []) if u.get("role") == "admin"), ""))
PY
    )
    
    if [ -n "$DEST_EMAIL" ] && [ "$DEST_EMAIL" != "" ]; then
        if { printf "Subject: IMAPSync - Rapport quotidien : erreurs et avertissements IA\nContent-Type: text/plain; charset=UTF-8\n\n"; cat "$REPORT_FILE"; } | msmtp --account=default "$DEST_EMAIL"; then
            > "$REPORT_FILE"
        fi
    fi
fi
