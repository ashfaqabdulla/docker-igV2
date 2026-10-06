#!/bin/sh
# Start Chrome, clean stale profile locks, expose CDP via socat.
# Chrome is the foreground process; if it dies, the container exits.
set -e

PROFILE=/home/chrome/profile

rm -f "$PROFILE/SingletonLock" \
      "$PROFILE/SingletonCookie" \
      "$PROFILE/SingletonSocket"

google-chrome-stable \
    --remote-debugging-port=9222 \
    --user-data-dir="$PROFILE" \
    --headless=new \
    --no-sandbox \
    --disable-gpu \
    --disable-dev-shm-usage \
    --no-first-run \
    --no-default-browser-check \
    --remote-allow-origins=* \
    --disable-background-networking \
    --disable-default-apps \
    --disable-sync \
    &
CHROME_PID=$!

for i in $(seq 1 60); do
    if curl -fsS http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done

socat TCP-LISTEN:9223,fork,reuseaddr,bind=0.0.0.0 TCP:127.0.0.1:9222 &
SOCAT_PID=$!

trap 'kill $SOCAT_PID 2>/dev/null || true; exit 0' TERM INT
wait $CHROME_PID
kill $SOCAT_PID 2>/dev/null || true
