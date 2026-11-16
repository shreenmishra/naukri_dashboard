#!/usr/bin/env bash
# ==============================================================================
# One-Command Linux Server Setup for Naukri Web App (Port 8080) & Scheduler
# ==============================================================================
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== [1/3] Preparing Python Environment & Playwright ==="
if ! command -v python3 &> /dev/null; then
    echo "Python 3 is required. Run: sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi

if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
playwright install --with-deps chromium

echo "=== [2/3] Starting Naukri Web Application on Port 8080 ==="
# Stop any existing instance on port 8080
pkill -f "naukri_dashboard.py" 2>/dev/null || true

nohup "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/naukri_dashboard.py" --port 8080 > "$SCRIPT_DIR/webapp.log" 2>&1 &
sleep 2

echo "=== [3/3] Service Status ==="
if pgrep -f "naukri_dashboard.py" >/dev/null; then
    SERVER_IP=$(hostname -I | awk '{print $1}')
    echo "SUCCESS! Naukri Web Portal & Background Scheduler is running."
    echo "  -> Local URL:  http://$SERVER_IP:8080"
    echo "  -> Logs:       tail -f $SCRIPT_DIR/webapp.log"
else
    echo "ERROR: Web app failed to start. Check $SCRIPT_DIR/webapp.log"
    exit 1
fi
