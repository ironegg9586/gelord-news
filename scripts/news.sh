#!/data/data/com.termux/files/usr/bin/bash
# Starts MyNews at http://localhost:8888 (runs in the background).
DIR="$HOME/MyNews"
cd "$DIR" || { echo "MyNews folder not found: $DIR"; exit 1; }

if curl -s -o /dev/null http://127.0.0.1:8888/ 2>/dev/null; then
  echo "MyNews is already running."
else
  nohup python3 "$DIR/server.py" > "$DIR/server.log" 2>&1 &
  echo $! > "$DIR/server.pid"
  sleep 1
  echo "MyNews started."
fi

echo "Open: http://localhost:8888"
command -v termux-open-url >/dev/null 2>&1 && termux-open-url "http://localhost:8888"
