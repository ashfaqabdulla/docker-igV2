# ig-reel

Instagram reel metrics scraper. Runs as a two-container Docker stack:
Chrome exposes a CDP endpoint, the scraper connects to it and fetches
metrics for a list of reel URLs. Also exposes a FastAPI HTTP interface.

## Architecture

```
┌─────────────────────┐        ┌─────────────────────────┐
│  ig-reel-chrome     │◄──CDP──│  ig-reel-api            │
│  headless Chrome    │        │  FastAPI on :8000       │
│  socat 9223→9222    │        │  entrypoint socat       │
└─────────────────────┘        │  127.0.0.1:9222→remote  │
                                └──────────┬──────────────┘
                                           │ host port 8099
                                           ▼
                                    http://server:8099
```

- Chrome runs persistent on the internal network only.
- API runs persistent, injects `ig_state.json` into a fresh browser
  context per request.
- Scraper is one-shot, used by cron or manual `docker compose run`.

## Setup

```bash
git clone <your-repo> ig-reel
cd ig-reel
mkdir -p secrets out
chmod 700 secrets
# Copy ig_state.json into secrets/
chmod 600 secrets/ig_state.json
cp reels.txt.example reels.txt
```

## Build and run

```bash
docker compose build
docker compose up -d
docker compose ps
```

## API

- `GET  /health`   — Chrome + session status
- `GET  /session`  — session file status
- `POST /fetch`    — fetch metrics
  ```json
  {"urls":["https://www.instagram.com/reel/XXXX/"],"batch_size":10,"pool_size":10}
  ```

## CLI

```bash
docker compose run --rm scraper fetch /app/reels.txt --headless
```

## Session renewal

`ig_state.json` lasts ~1 year. When it expires:

```bash
# On a machine with a real display:
python ig_scrap.py login
scp ig_state.json user@server:~/ig-reel/secrets/ig_state.json
```

## Scheduled runs

```bash
crontab -e
```

Add:

```
PATH=/usr/local/bin:/usr/bin:/bin
0 6,18 * * * /home/user/ig-reel/run.sh >> /home/user/ig-reel/out/cron.log 2>&1
```

## Housekeeping

```bash
docker compose logs chrome
docker compose restart chrome
docker compose down
```

Do NOT run `docker system prune` on a shared host — other projects share
the Docker daemon.
