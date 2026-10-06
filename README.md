# ig-reel

Instagram reel metrics scraper. Two Docker containers: a Chrome container
exposing a CDP endpoint, and a scraper container that connects to it.

## Architecture

```
┌─────────────────────┐        ┌─────────────────────────┐
│  ig-reel-chrome     │◄──CDP──│  ig-reel-scraper        │
│  headless Chrome    │        │  Playwright async       │
│  :9222 (internal)   │        │  injects ig_state.json  │
└─────────────────────┘        └─────────────────────────┘
```

- Chrome runs persistent, on the internal Docker network only.
- Scraper runs one-shot per fetch, injects the session, exits.
- Login lives in `secrets/ig_state.json`, never baked into an image.

## Setup

```bash
git clone git@github.com:YOUR_USER/ig-reel.git ~/ig-reel
cd ~/ig-reel
mkdir -p secrets out
chmod 700 secrets
# scp ig_state.json from your laptop into secrets/
chmod 600 secrets/ig_state.json
chmod +x run.sh
```

## Build and run

```bash
docker compose build
docker compose up -d chrome
sleep 15
docker compose ps

docker compose run --rm scraper python health_check.py
docker compose run --rm scraper fetch reels.txt --headless
```

## Output

Each run writes `fetch_session_<timestamp>.csv` and `.json` into `out/`.

## Session renewal

`ig_state.json` lasts about a year. When it expires:

```bash
# On a desktop with a real display:
python ig_scrap.py login
scp ig_state.json user@server:~/ig-reel/secrets/ig_state.json
```

## Scheduled runs

See `run.sh` and install via cron:

```
0 6,18 * * * /home/user/ig-reel/run.sh >> /home/user/ig-reel/out/cron.log 2>&1
```

## Housekeeping

```bash
docker compose logs chrome
docker compose restart chrome
docker compose down
```

Do NOT run `docker system prune` on this host — other projects share
the Docker daemon.
