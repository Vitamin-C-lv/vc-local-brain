#!/usr/bin/env bash
# adaptive-compaction — one-shot safe restart helper for the DSH Web Host.
# Runs detached from the host it stops (launched via nohup+setsid).
# Kills ONLY the exact old PID (verified by cmdline), then starts the new host
# with the same real launcher/args. Never pkill/killall.
set -u

DSH_BIN=/home/vitamin_c/.local/bin/dsh
RUN_DIR=/home/vitamin_c/.dsh/luna-team
STATUS="$RUN_DIR/restart-adaptive.status"
LOG="$RUN_DIR/restart-adaptive.log"
OLD_PID="${1:-}"
PORT=3080
TARGET=127.0.0.1

mkdir -p "$RUN_DIR"
echo "STARTING old_pid=$OLD_PID at $(date -Is)" > "$STATUS"

fail() {
  echo "FAIL:$1 at $(date -Is)" > "$STATUS"
  exit 1
}

[ -n "$OLD_PID" ] || fail "no-old-pid"

# 1) give the current agent turn time to finish and flush to disk
sleep 45

# 2) re-verify the PID is exactly the DSH web host before touching it
if [ -r "/proc/$OLD_PID/cmdline" ]; then
  CMDLINE="$(tr '\0' ' ' < "/proc/$OLD_PID/cmdline" 2>/dev/null || true)"
  case "$CMDLINE" in
    *"bin.js"*" web "*) : ;;
    *) fail "pid-changed($OLD_PID)" ;;
  esac
else
  fail "pid-gone($OLD_PID)"
fi

# 3) stop ONLY this exact PID
kill -TERM "$OLD_PID" 2>/dev/null || fail "term-send"
for _ in $(seq 1 30); do
  kill -0 "$OLD_PID" 2>/dev/null || break
  sleep 1
done
if kill -0 "$OLD_PID" 2>/dev/null; then
  kill -KILL "$OLD_PID" 2>/dev/null
  sleep 1
fi
echo "OLD_HOST_STOPPED at $(date -Is)" > "$STATUS"

# 4) wait for the port to be released
for _ in $(seq 1 30); do
  ss -ltn 2>/dev/null | grep -q ":$PORT " || break
  sleep 1
done

# 5) start the new host, fully detached from this helper's session
setsid nohup "$DSH_BIN" web --host "$TARGET" --port "$PORT" --no-open \
  > "$LOG" 2>&1 < /dev/null &
NEW_PID=$!
echo "NEW_HOST_STARTED new_pid=$NEW_PID at $(date -Is)" > "$STATUS"

# 6) wait for LISTEN, then verify the listener is a NEW pid
LISTEN_PID=""
for _ in $(seq 1 30); do
  if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    LISTEN_PID="$(ss -ltnp 2>/dev/null | grep ":$PORT " | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2)"
    [ -n "$LISTEN_PID" ] && break
  fi
  sleep 1
done

HEALTH=""
for _ in $(seq 1 12); do
  HEALTH="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://$TARGET:$PORT/" 2>/dev/null || true)"
  case "$HEALTH" in
    200|301|302|303|307|308) break ;;
    *) sleep 1 ;;
  esac
done

if [ -n "$LISTEN_PID" ] && [ "$LISTEN_PID" != "$OLD_PID" ] && [ -n "$HEALTH" ]; then
  echo "PORT_READY listen_pid=$LISTEN_PID health=$HEALTH at $(date -Is)" > "$STATUS"
  exit 0
fi
fail "port-not-ready(listen=$LISTEN_PID health=$HEALTH)"
