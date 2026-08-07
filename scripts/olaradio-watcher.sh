#!/bin/bash
# olaradio-watcher.sh — fswatch daemon for Ola Radio messages
#
# Watches DB → transcribes → injects into iMessage session via imsg
# Messages appear in the OpenClaw iMessage session as if spoken via radio.
#
# Routing: state/olaradio-routes.json maps group ID → destination

DB_DIR="/Users/jerclaw/Library/Containers/com.aewt.app.friends/Data/Documents"
MONITOR="/Users/jerclaw/.openclaw/workspace/scripts/olaradio-monitor.py"
DELIVER="/Users/jerclaw/.openclaw/workspace/scripts/olaradio-deliver.py"
LOG="/Users/jerclaw/.openclaw/workspace/logs/olaradio-watcher.log"
TMP_OUT="/tmp/olaradio-monitor-output.json"

touch "$LOG" "$TMP_OUT"
echo "$(date): watcher started" >> "$LOG"

/opt/homebrew/bin/fswatch -0 --latency 1.5 "$DB_DIR" | while IFS= read -r -d '' event; do
    BASENAME=$(basename "$event")
    case "$BASENAME" in
        poc_chat_record.db|poc_chat_record.db-journal|poc_chat_record.db-wal|poc_chat_record.db-shm)
            echo "$(date): change: $event" >> "$LOG"
            sleep 5

            # State is committed only after delivery succeeds. This prevents a
            # transient imsg/gateway failure from permanently losing a message.
            /usr/bin/python3 "$MONITOR" --defer-state > "$TMP_OUT" 2>>"$LOG"
            if [[ $? -ne 0 ]]; then
                echo "$(date): monitor error" >> "$LOG"
                continue
            fi

            if [[ ! -s "$TMP_OUT" ]] || grep -q '^\[\s*\]' "$TMP_OUT" 2>/dev/null; then
                echo "$(date): no new messages" >> "$LOG"
                continue
            fi

            # The monitor calls olaradio-route.py and embeds its decision in
            # TMP_OUT. Delivery consumes that exact route.
            /usr/bin/python3 "$DELIVER" 2>>"$LOG"
            if [[ $? -ne 0 ]]; then
                echo "$(date): delivery error" >> "$LOG"
                continue
            fi
            /usr/bin/python3 "$MONITOR" --commit-output "$TMP_OUT" >/dev/null 2>>"$LOG"
            if [[ $? -ne 0 ]]; then
                echo "$(date): checkpoint error" >> "$LOG"
                continue
            fi
            echo "$(date): delivery complete" >> "$LOG"
            ;;
    esac
done
