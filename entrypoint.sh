#!/bin/sh
# If IG_CDP_URL points to a remote host, start a local socat forwarder
# so Chrome's Host header check passes. Chrome's DevTools HTTP endpoints
# reject Host headers that don't match 127.0.0.1/localhost/[::1].
set -e

if [ -n "$IG_CDP_URL" ]; then
    case "$IG_CDP_URL" in
        http://127.0.0.1:*|http://localhost:*|http://[::1]:*)
            ;;
        *)
            REMOTE="${IG_CDP_URL#http://}"
            REMOTE="${REMOTE#https://}"
            REMOTE="${REMOTE%/}"

            echo "entrypoint: forwarding 127.0.0.1:9222 -> $REMOTE"
            socat TCP-LISTEN:9222,fork,reuseaddr,bind=127.0.0.1 "TCP:$REMOTE" &
            SOCAT_PID=$!

            for i in $(seq 1 40); do
                if curl -fsS http://127.0.0.1:9222/json/version >/dev/null 2>&1; then
                    echo "entrypoint: forwarder ready"
                    break
                fi
                sleep 0.25
            done

            export IG_CDP_URL="http://127.0.0.1:9222"

            trap 'kill $SOCAT_PID 2>/dev/null || true' TERM INT EXIT
            ;;
    esac
fi

exec "$@"
