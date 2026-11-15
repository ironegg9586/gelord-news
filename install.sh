#!/usr/bin/env bash
# gelord-news installer (v1.0.0)
# Copies the app into ~/MyNews and can start it at http://localhost:8888.
# No AI, no paid services. The only network use is an optional "pkg install"
# that runs ONLY if you say yes.
set -u

SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="${MYNEWS_DIR:-$HOME/MyNews}"

ask() {  # ask "question" -> returns 0 for yes
  local reply
  read -r -p "$1 [y/N] " reply || reply=""
  case "$reply" in [yY]|[yY][eE][sS]) return 0 ;; *) return 1 ;; esac
}

echo "== gelord-news installer =="
echo "Install folder: $DEST"
echo

# 1. Check what the app needs
missing=()
for cmd in python3 curl; do
  if command -v "$cmd" >/dev/null 2>&1; then
    echo "OK      $cmd"
  else
    echo "MISSING $cmd"
    missing+=("$cmd")
  fi
done

if [ "${#missing[@]}" -gt 0 ]; then
  pkgs=()
  for m in "${missing[@]}"; do
    case "$m" in python3) pkgs+=("python") ;; *) pkgs+=("$m") ;; esac
  done
  echo
  echo "These are needed: ${missing[*]}"
  echo "Install command:  pkg install ${pkgs[*]}"
  if command -v pkg >/dev/null 2>&1 && ask "Run that pkg install now?"; then
    pkg install "${pkgs[@]}" || { echo "pkg install failed. Please run it yourself, then run this installer again."; exit 1; }
  else
    echo "Nothing was installed. Run the command above, then run this installer again."
    exit 1
  fi
fi

# 2. Check the repo files are here
for f in app/server.py app/index.html config/feeds.json config/topics.example.json \
         assets/icon.svg scripts/news.sh scripts/stop.sh; do
  if [ ! -f "$SRC/$f" ]; then
    echo "Missing file in the download: $f"
    exit 1
  fi
done

# 3. Copy the app (your own data is never overwritten)
mkdir -p "$DEST" || { echo "Could not create $DEST"; exit 1; }
cp "$SRC/app/server.py" "$SRC/app/index.html" "$DEST/" &&
cp "$SRC/config/feeds.json" "$DEST/" &&
cp "$SRC/assets/icon.svg" "$DEST/" &&
cp "$SRC/scripts/news.sh" "$SRC/scripts/stop.sh" "$DEST/" || { echo "Copy failed."; exit 1; }

if [ -f "$DEST/topics.json" ]; then
  echo "Kept your topics.json (not changed)."
else
  cp "$SRC/config/topics.example.json" "$DEST/topics.json" && echo "Added starter topics.json."
fi
# settings.json and seen.json are made by the server itself on first start.

chmod +x "$DEST/news.sh" "$DEST/stop.sh"
echo "Copied the app to $DEST"
echo

# 4. Start it?
if ask "Start the news server now?"; then
  bash "$DEST/news.sh"
  echo "Open: http://localhost:8888"
else
  echo "Start it later with:  bash $DEST/news.sh"
  echo "Then open:            http://localhost:8888"
fi
echo "Stop it with:         bash $DEST/stop.sh"
