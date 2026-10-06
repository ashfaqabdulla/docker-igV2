#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if ! curl -fsS http://127.0.0.1:8099/health >/dev/null; then
    echo "$(date -Iseconds) API unreachable" >&2
    exit 1
fi

URLS_JSON=$(grep -v '^#' reels.txt | grep -v '^$' | \
    python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin]))')

curl -fsS -X POST http://127.0.0.1:8099/fetch \
    -H 'Content-Type: application/json' \
    -d "{\"urls\": $URLS_JSON, \"batch_size\": 10, \"pool_size\": 10}" \
    > /dev/null

echo "$(date -Iseconds) fetch complete"
