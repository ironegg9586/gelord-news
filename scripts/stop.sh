#!/data/data/com.termux/files/usr/bin/bash
# Stops MyNews (the server started by news.sh).
DIR="$HOME/MyNews"
PIDFILE="$DIR/server.pid"
PID=""
if [ -f "$PIDFILE" ]; then
  PID=$(cat "$PIDFILE" 2>/dev/null)
fi
# Fallback: exact match on the server command line (no partial matches).
if [ -z "$PID" ] || ! kill -0 "$PID" 2>/dev/null; then
  PID=$(pgrep -f "^python3 $DIR/server\.py$" 2>/dev/null | head -n 1)
fi
if [ -z "$PID" ]; then
  echo "MyNews is not running."
  rm -f "$PIDFILE"
  exit 0
fi
kill "$PID" && echo "MyNews stopped (PID $PID)."
rm -f "$PIDFILE"
