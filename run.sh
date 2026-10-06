#!/usr/bin/env bash
set -euo pipefail
cd /home/user/ig-reel

# Bail if Chrome isn't up.
if ! docker compose run --rm scraper python health_check.py; then
    echo "$(date -Iseconds) chrome unreachable, skipping" >&2
    exit 1
fi

# Fetch. Exit code from ig_scrap.py propagates up.
docker compose run --rm scraper fetch reels.txt --headless
