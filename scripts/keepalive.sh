#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$LOG_DIR/uvicorn.pid"
SERVER_LOG="$LOG_DIR/server.log"
UV_BIN="${UV_BIN:-$HOME/.local/bin/uv}"
PORT="${PORT:-8010}"

mkdir -p "$LOG_DIR"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        exit 0
    fi
fi

# Check if port 8010 is already bound by our uvicorn process
EXISTING_PID=$(pgrep -f "[u]vicorn app.main:app --host 0.0.0.0 --port $PORT" | head -n 1 || true)
if [ -n "$EXISTING_PID" ]; then
    echo "$EXISTING_PID" > "$PID_FILE"
    exit 0
fi

cd "$PROJECT_DIR"
export PATH="$PROJECT_DIR/bin:$PATH"
nohup setsid "$UV_BIN" run uvicorn app.main:app --host 0.0.0.0 --port "$PORT" >> "$SERVER_LOG" 2>&1 < /dev/null &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] Started TubeShift YouTube Converter server on port $PORT (PID=$NEW_PID)" >> "$SERVER_LOG"
