#!/bin/bash

# Configuration
SESSION_NAME="mbii_duel"
PYTHON_SCRIPT="duel.py"
PYTHON_BIN="python3"

start() {
    screen -ls | grep -q "$SESSION_NAME"
    if [ $? -eq 0 ]; then
        echo "[!] Duel is already running in screen session: $SESSION_NAME"
    else
        echo "[*] Starting Duel in detached mode..."
        screen -dmS "$SESSION_NAME" $PYTHON_BIN $PYTHON_SCRIPT
        echo "[+] Duel started. Use './manage_duel.sh attach' to view logs."
    fi
}

stop() {
    screen -ls | grep -q "$SESSION_NAME"
    if [ $? -eq 0 ]; then
        echo "[*] Stopping Duel session..."
        screen -S "$SESSION_NAME" -X quit
        echo "[+] Duel stopped."
    else
        echo "[!] Duel is not running."
    fi
}

restart() {
    echo "[*] Restarting Duel..."
    stop
    sleep 1
    start
}

attach() {
    echo "[*] Attaching to Duel console... (Press Ctrl+A then D to detach)"
    screen -r "$SESSION_NAME"
}

status() {
    screen -ls | grep -q "$SESSION_NAME"
    if [ $? -eq 0 ]; then
        echo "[+] Duel Status: RUNNING"
    else
        echo "[-] Duel Status: STOPPED"
    fi
}

case "$1" in
    start) start ;;
    stop) stop ;;
    restart) restart ;;
    attach) attach ;;
    status) status ;;
    *) echo "Usage: $0 {start|stop|restart|attach|status}" ;;
esac