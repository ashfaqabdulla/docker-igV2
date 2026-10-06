"""
api.py - HTTP API wrapper around ig_scrap.run_fetch().

Endpoints
---------
  GET  /health          Liveness probe. Also checks the Chrome CDP endpoint.
  GET  /session         Reports the session file status.
  POST /fetch           Fetch metrics for a list of reel URLs.
                        Body: {"urls": [...], "batch_size": 10, "pool_size": 10}
                        Returns the same rows the CLI writes to CSV.

Runs in the same image as the scraper container, using the same session
file and Chrome container. Different entrypoint (uvicorn vs. python).
"""
import asyncio
import csv
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

import ig_scrap as core

app = FastAPI(title="ig-reel API", version="1.0")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class FetchRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1,
                            description="One or more Instagram reel URLs.")
    batch_size: int = Field(10, ge=1, le=50)
    pool_size: int = Field(10, ge=1, le=20)
    session: bool = Field(True, description="Use the saved session.")


class FetchResponse(BaseModel):
    count: int
    expired: bool
    elapsed_seconds: float
    rows: list[dict]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _make_config(req: FetchRequest | None = None,
                  session_on: bool = True) -> core.Config:
    """Build a core.Config from env vars and (optionally) request fields."""
    return core.Config(
        state=os.environ.get("IG_STATE", "ig_state.json"),
        headless=True,
        max_scrolls=core.DEFAULT_MAX_SCROLLS,
        account_timeout=int(os.environ.get("IG_TIMEOUT", "150")),
        concurrency=int(os.environ.get("IG_CONCURRENCY", "5")),
        session_concurrency=int(os.environ.get("IG_SESSION_CONCURRENCY", "5")),
        batch_size=req.batch_size if req else core.DEFAULT_BATCH_SIZE,
        pool_size=req.pool_size if req else core.DEFAULT_POOL_SIZE,
        session_on=session_on,
        cdp_url=os.environ.get("IG_CDP_URL"),
        out_dir=os.environ.get("IG_OUT_DIR", "out"),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    """
    Liveness + readiness in one. Reports whether Chrome is reachable
    and whether the session file is present.
    """
    cdp = os.environ.get("IG_CDP_URL")
    chrome_ok = False
    chrome_info = None
    if cdp:
        import urllib.request
        try:
            with urllib.request.urlopen(cdp + "/json/version", timeout=3) as r:
                info = json.load(r)
            chrome_ok = "Browser" in info
            chrome_info = info.get("Browser")
        except Exception as e:
            chrome_info = f"unreachable: {e}"

    state = os.environ.get("IG_STATE", "ig_state.json")
    sid, err = core.state_file_ok(state)

    return {
        "ok": chrome_ok and err is None,
        "chrome": {"reachable": chrome_ok, "browser": chrome_info},
        "session": {"valid": err is None, "error": err,
                    "expires": sid.get("expires") if sid else None},
    }


@app.get("/session")
async def session_status():
    """Just the session file status, without probing Chrome."""
    state = os.environ.get("IG_STATE", "ig_state.json")
    sid, err = core.state_file_ok(state)
    if err:
        return {"valid": False, "error": err}
    exp = sid.get("expires", -1)
    return {
        "valid": True,
        "expires": exp,
        "days_left": max(0, int((exp - time.time()) / 86400)) if exp > 0 else None,
    }


@app.post("/fetch", response_model=FetchResponse)
async def fetch(req: FetchRequest):
    """
    Fetch metrics for the provided URLs. Runs the full three-phase
    pipeline. Blocks until done and returns the rows.

    For a 68-reel file with defaults, expect ~10s. For much larger
    inputs, use the async job pattern (see README) or cap `urls` at a
    reasonable size.
    """
    cfg = _make_config(req, session_on=req.session)

    if cfg.session_on and not cfg.cdp_url:
        # In API mode we always expect CDP.
        raise HTTPException(500, "IG_CDP_URL is not set; API requires CDP mode.")

    t0 = time.time()
    try:
        rows, expired = await core.run_fetch(
            req.urls, cfg.concurrency, cfg.session_on, cfg
        )
    except core.SessionExpired as e:
        raise HTTPException(401, f"Session expired: {e}")

    # Persist artifacts just like the CLI does.
    if rows:
        out_dir = Path(cfg.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        tag = "session" if cfg.session_on else "loggedout"
        with open(out_dir / f"fetch_{tag}_{stamp}.csv", "w",
                  newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        with open(out_dir / f"fetch_{tag}_{stamp}.json", "w",
                  encoding="utf-8") as f:
            json.dump(rows, f, indent=2)

    return FetchResponse(
        count=len(rows),
        expired=expired,
        elapsed_seconds=round(time.time() - t0, 2),
        rows=[asdict(r) if hasattr(r, "__dataclass_fields__") else r for r in rows],
    )


@app.get("/")
async def root():
    return {"service": "ig-reel API", "version": "1.0",
            "endpoints": ["/health", "/session", "/fetch"]}
