#!/bin/zsh

set -u

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON_EXECUTABLE="$PROJECT_ROOT/.venv/bin/python"
CHROME_PROFILE="${AUTOAPPLY_CHROME_PROFILE:-$HOME/Library/Application Support/AutoApply/ChromeProfile}"
CHROME_EXECUTABLE="${AUTOAPPLY_CHROME_EXECUTABLE:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"
LOG_DIRECTORY="$PROJECT_ROOT/logs"
REDIS_DATA_DIRECTORY="$PROJECT_ROOT/data/redis"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

typeset -a CHILD_PIDS
CHILD_PIDS=()
CLEANED_UP=0

if [[ -f "$PROJECT_ROOT/.env" ]]; then
  set -a
  source "$PROJECT_ROOT/.env"
  set +a
fi

log() {
  print -r -- "[AutoApply] $1"
}

fail() {
  print -ru2 -- "[AutoApply] ERROR: $1"
  exit 1
}

port_is_open() {
  nc -z 127.0.0.1 "$1" >/dev/null 2>&1
}

wait_for_port() {
  local port="$1"
  local attempts="$2"
  local count=0
  while (( count < attempts )); do
    port_is_open "$port" && return 0
    sleep 0.25
    (( count += 1 ))
  done
  return 1
}

cleanup() {
  (( CLEANED_UP == 1 )) && return
  CLEANED_UP=1
  log "Stopping services started by this launcher..."
  local pid
  for pid in "${CHILD_PIDS[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  wait >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

[[ -x "$PYTHON_EXECUTABLE" ]] || fail "Virtual environment not found. Follow the installation steps in README.md."
[[ -x "$CHROME_EXECUTABLE" ]] || fail "Google Chrome not found. Set AUTOAPPLY_CHROME_EXECUTABLE in your shell."

mkdir -p "$LOG_DIRECTORY" "$REDIS_DATA_DIRECTORY" "$CHROME_PROFILE"

if ! port_is_open 11434; then
  command -v ollama >/dev/null 2>&1 || fail "Ollama is not installed or is not available on PATH."
  log "Starting Ollama..."
  ollama serve >>"$LOG_DIRECTORY/ollama.log" 2>&1 &
  CHILD_PIDS+=("$!")
  wait_for_port 11434 120 || fail "Ollama did not start on port 11434."
else
  log "Using the Ollama service that is already running."
fi

if ! port_is_open 6379; then
  command -v redis-server >/dev/null 2>&1 || fail "Redis is not installed. Run: brew install redis"
  log "Starting Redis..."
  redis-server --bind 127.0.0.1 --port 6379 --dir "$REDIS_DATA_DIRECTORY" --appendonly yes >>"$LOG_DIRECTORY/redis.log" 2>&1 &
  CHILD_PIDS+=("$!")
  wait_for_port 6379 60 || fail "Redis did not start on port 6379."
else
  log "Using the Redis service that is already running."
fi

if port_is_open 8000; then
  fail "Port 8000 is already in use. Stop the other service and try again."
fi

log "Starting the AutoApply API..."
cd "$PROJECT_ROOT"
PYTHONUTF8=1 "$PYTHON_EXECUTABLE" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 >>"$LOG_DIRECTORY/backend.log" 2>&1 &
CHILD_PIDS+=("$!")
wait_for_port 8000 720 || fail "The API did not start. Check logs/backend.log."

log "Opening the dedicated Chrome profile..."
"$CHROME_EXECUTABLE" \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --remote-allow-origins=http://127.0.0.1:9222 \
  --user-data-dir="$CHROME_PROFILE" \
  --no-first-run \
  --disable-background-mode \
  --new-window http://127.0.0.1:8000/ &
CHROME_PID="$!"
CHILD_PIDS+=("$CHROME_PID")

log "AutoApply is running. Close this Chrome window or press Control-C to stop."
wait "$CHROME_PID" || true
