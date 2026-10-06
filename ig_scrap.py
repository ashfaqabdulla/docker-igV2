"""
ig-scrapV4.py - Instagram reel metrics via login session + API-first fetch.

Commands
--------
  login               Open a Chrome window; you log in by hand; cookies saved.
  check               Verify the saved session is still valid.
  fetch FILE          Fetch metrics for every reel URL in FILE.
  compare FILE        Run logged-out vs session side-by-side.

Common flags
------------
  --state PATH               Session file (default ig_state.json).
  --headless                 Hide the browser window (launch mode only).
  --cdp-url URL              Attach to a running Chrome via CDP; implies
                             session mode. Falls back to IG_CDP_URL env var.
  --timeout SEC              Per-account hard timeout (default 150).
  --max-scrolls N            Max scrolls per grid page (default 60).
  --session-concurrency N    Max parallel jobs in session mode (default 5).
  --batch-size N             Reels per API batch (default 5).
  --pool-size N              Number of warm pages for API batches (default 3).
  --out-dir PATH             Where to write CSV/JSON (default out).

Browser modes
-------------
  Launch mode (default)
      Script launches its own Chrome. Loads ig_state.json if --session
      is set. Headless is supported.

  CDP mode (--cdp-url http://host:9222 or IG_CDP_URL env var)
      Script attaches to a Chrome already running with
      --remote-debugging-port. Creates a FRESH browser context and
      injects ig_state.json as storage_state, so the remote Chrome
      never needs to be logged in itself. The browser is not closed on
      exit -- only the tabs we opened are closed.

Fetch pipeline
--------------
  phase 1  API batch on ALL reels. /api/v1/media/{id}/info/ returns
           owner username AND metrics in one call.
  phase 2  Reel-page JSON for reels the API missed AND whose URL had no
           username. Only to resolve the owner for grid grouping.
  phase 3  Grid visit per remaining account.

Session safety
--------------
ig_state.json is as sensitive as a password. Do not share or commit it.
Use a dedicated Instagram account, not your personal or business one.
"""
import argparse
import asyncio
import csv
import json
import os
import random
import re
import sys
import time
from collections import Counter, defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Metric fields and defaults
# ---------------------------------------------------------------------------

FIELDS = (
    "play_count", "ig_play_count", "fb_play_count", "view_count",
    "video_view_count", "video_play_count",
    "like_count", "comment_count", "taken_at",
)

VIEW_FIELDS = (
    "play_count", "ig_play_count", "fb_play_count",
    "view_count", "video_view_count", "video_play_count",
)

DEFAULT_MAX_SCROLLS = 60
DEFAULT_ACCOUNT_TIMEOUT = 150
DEFAULT_SESSION_CONCURRENCY = 5
DEFAULT_BATCH_SIZE = 5
DEFAULT_POOL_SIZE = 3
SCROLL_WAIT_MS = 2000
STALE_SCROLLS = 6
API_CALL_TIMEOUT_MS = 8000

SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

# Headless Chrome self-identifies as "HeadlessChrome" in its UA string,
# which Instagram's bot detection flags more often than a real Chrome UA.
# Override with this. Bump the version occasionally.
CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class Config:
    state: str = "ig_state.json"
    headless: bool = False
    max_scrolls: int = DEFAULT_MAX_SCROLLS
    account_timeout: int = DEFAULT_ACCOUNT_TIMEOUT
    concurrency: int = 1
    session_concurrency: int = DEFAULT_SESSION_CONCURRENCY
    batch_size: int = DEFAULT_BATCH_SIZE
    pool_size: int = DEFAULT_POOL_SIZE
    session_on: bool = False
    file: str = "reels.txt"
    cdp_url: str | None = None
    out_dir: str = "out"


class SessionExpired(Exception):
    """Instagram redirected a session request to login or /challenge."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def epoch_iso(v):
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(v)))
    except Exception:
        return None


def shortcode_to_media_id(code: str) -> str:
    """Decode an Instagram shortcode (base64-ish) into the numeric media ID."""
    n = 0
    for c in code:
        n = n * 64 + SHORTCODE_ALPHABET.index(c)
    return str(n)


def clean(url):
    return url.split("?")[0].split("#")[0]


def parse(url):
    """
    Extract (shortcode, username_or_None). Username is None for URLs like
    instagram.com/reel/{code}/ -- the API response supplies the owner.
    """
    m = re.search(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None, None
    um = re.search(r"instagram\.com/([^/?#]+)/(?:reel|reels|p|tv)/", url)
    return m.group(1), (um.group(1).lower() if um else None)


def walk(o):
    """Iteratively yield every dict with a 'code' key. No recursion."""
    stack = [o]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            if "code" in item:
                yield item
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)


def read_urls(path):
    p = Path(path)
    if not p.exists():
        sys.exit(f"File not found: {path}")
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def state_file_ok(path):
    """Return (sessionid_cookie, None) if usable, else (None, error)."""
    p = Path(path)
    if not p.exists():
        return None, "no session file"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"unreadable session file: {e}"
    sid = next((c for c in data.get("cookies", []) if c.get("name") == "sessionid"), None)
    if not sid:
        return None, "session file has no sessionid cookie (login was not completed)"
    return sid, None


def _has_views(rec):
    return any(rec.get(k) is not None for k in VIEW_FIELDS)


def _is_top_media(o):
    """
    True for top-level media objects: they have `code` and `user` at the
    same level. Filters out nested objects that carry a `code` (coauthor
    entries, related media) so their user field can't corrupt the owner.
    """
    return "code" in o and isinstance(o.get("user"), dict)


# ---------------------------------------------------------------------------
# Browser lifecycle
# ---------------------------------------------------------------------------

async def launch(p, headless):
    """Prefer real Chrome if installed; fall back to bundled Chromium."""
    try:
        return await p.chromium.launch(channel="chrome", headless=headless)
    except Exception:
        return await p.chromium.launch(headless=headless)


@asynccontextmanager
async def browser_context(cfg, use_session):
    """
    Yield a browser context.

    CDP mode: attach to a running Chrome, create a FRESH context with
    ig_state.json injected via storage_state. The remote Chrome never
    needs to be logged in itself. The browser is not closed on exit.

    Launch mode: start a fresh browser, load ig_state.json if requested,
    and write back rotated cookies on exit (but only if still logged in).
    """
    # ------------------------------------------------------------------
    # CDP mode
    # ------------------------------------------------------------------
    if cfg.cdp_url:
        if use_session:
            sid, err = state_file_ok(cfg.state)
            if err:
                sys.exit(f"{err}: run  python ig-scrapV4.py login  first.")

        async with async_playwright() as p:
            try:
                browser = await p.chromium.connect_over_cdp(cfg.cdp_url)
            except Exception as e:
                print(f"Could not connect to CDP at {cfg.cdp_url}: {e}")
                sys.exit(2)

            # Fresh context, not the browser's default, so we can inject
            # ig_state.json as storage_state. The UA override hides that
            # we're running headless Chrome in a container.
            kwargs = {"locale": "en-US", "user_agent": CHROME_UA}
            if use_session:
                kwargs["storage_state"] = cfg.state

            ctx = await browser.new_context(**kwargs)
            try:
                yield ctx
            finally:
                if use_session:
                    try:
                        cookies = await ctx.cookies("https://www.instagram.com")
                        if any(c["name"] == "sessionid" for c in cookies):
                            await ctx.storage_state(path=cfg.state)
                    except Exception as e:
                        print("could not refresh session file:", e)
                try:
                    await ctx.close()
                except Exception:
                    pass
            # Do NOT close the browser -- it belongs to another container.
        return

    # ------------------------------------------------------------------
    # Launch mode
    # ------------------------------------------------------------------
    if use_session:
        sid, err = state_file_ok(cfg.state)
        if err:
            sys.exit(f"{err}: run  python ig-scrapV4.py login  first.")

    async with async_playwright() as p:
        browser = await launch(p, cfg.headless)
        kwargs = {"locale": "en-US", "user_agent": CHROME_UA}
        if use_session:
            kwargs["storage_state"] = cfg.state
        ctx = await browser.new_context(**kwargs)
        try:
            yield ctx
        finally:
            if use_session:
                try:
                    cookies = await ctx.cookies("https://www.instagram.com")
                    if any(c["name"] == "sessionid" for c in cookies):
                        await ctx.storage_state(path=cfg.state)
                except Exception as e:
                    print("could not refresh session file:", e)
            try:
                await browser.close()
            except Exception:
                pass


def check_session_url(page, session_on):
    """Raise SessionExpired if a session page has been bounced to login/challenge."""
    if session_on:
        u = page.url
        if "/accounts/login" in u or "/challenge" in u:
            raise SessionExpired(f"Instagram redirected the session to {u.split('?')[0]}")


# ---------------------------------------------------------------------------
# Page-level scraping
# ---------------------------------------------------------------------------

async def capture(ctx, url, on_obj, *, max_scrolls=0, done=lambda: False,
                  session_on=False, wait_for_grid=True):
    """
    Open `url`, feed every JSON object with a 'code' to `on_obj`, and
    optionally scroll. Workers drain a queue so we don't accumulate one
    task per response. wait_for_grid=False skips the 25s selector wait
    used by grid pages (reel pages don't need it).
    """
    queue: asyncio.Queue = asyncio.Queue()
    seen = set()

    def merge(data):
        for o in walk(data):
            seen.add(o["code"])
            on_obj(o)

    async def worker():
        while True:
            resp = await queue.get()
            try:
                if "instagram.com" in resp.url and (
                        "/graphql" in resp.url or "/api/v1/" in resp.url):
                    body = (await resp.text()).replace("for (;;);", "", 1).strip()
                    if body and body[0] in "{[":
                        merge(json.loads(body))
            except Exception:
                pass
            finally:
                queue.task_done()

    workers = [asyncio.create_task(worker()) for _ in range(3)]
    page = await ctx.new_page()
    page.on("response", lambda r: queue.put_nowait(r))
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=90000)
        if wait_for_grid:
            try:
                await page.wait_for_selector(
                    'a[href*="/reel/"], a[href*="/p/"]', timeout=25000)
            except Exception:
                pass
        check_session_url(page, session_on)
        await page.wait_for_timeout(1500)
        await page.keyboard.press("Escape")

        for txt in await page.eval_on_selector_all(
                'script[type="application/json"]',
                "els => els.map(e => e.textContent)"):
            try:
                merge(json.loads(txt))
            except Exception:
                pass

        stale, last = -1, -1
        for _ in range(max_scrolls):
            if done():
                break
            await page.mouse.wheel(0, 3000)
            await page.wait_for_timeout(SCROLL_WAIT_MS)
            check_session_url(page, session_on)
            stale = stale + 1 if len(seen) == last else 0
            last = len(seen)
            if stale >= STALE_SCROLLS:
                break
        check_session_url(page, session_on)
    finally:
        try:
            await asyncio.wait_for(queue.join(), timeout=3)
        except asyncio.TimeoutError:
            pass
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await page.close()


def _absorb(rec, o, *, allow_username=False):
    """
    Merge metric fields from `o` into `rec`. Only pass allow_username=True
    when `o` is the top-level media object; nested objects frequently
    carry a `user` for tagged users or co-authors.
    """
    for k in FIELDS:
        if o.get(k) is not None:
            rec[k] = o[k]
    if allow_username:
        u = (o.get("user") or o.get("owner") or {}).get("username")
        if u:
            rec["_username"] = u.lower()


async def _make_warm_page(ctx, session_on):
    """
    Create one page and load the Instagram homepage so it has a live
    origin. Used by warm_pages to build the pool in parallel.
    """
    p = await ctx.new_page()
    try:
        await p.goto("https://www.instagram.com/",
                     wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    check_session_url(p, session_on)
    return p


async def warm_pages(ctx, n, session_on):
    """
    Create n warm pages in parallel. Each does its own Instagram homepage
    load; running them concurrently drops the pool-build cost from
    ~1.5s per page to ~1.5s total.

    In CDP mode these become tabs in your running Chrome.
    """
    return await asyncio.gather(*[_make_warm_page(ctx, session_on)
                                  for _ in range(n)])


async def scrape_reels_api_batch(page, codes, got_map, session_on):
    """
    One page round-trip, N parallel fetches inside the browser. Each fetch
    has its own AbortController so a slow reel can't stall the batch.
    Returns the set of codes that resolved to at least one view field.
    """
    reqs = [
        {
            "code": c,
            "url": f"https://www.instagram.com/api/v1/media/"
                   f"{shortcode_to_media_id(c)}/info/",
        }
        for c in codes
    ]

    results = await page.evaluate(
        """async ({reqs, timeoutMs}) => {
            const headers = {
                'X-IG-App-ID': '936619743392459',
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9',
            };
            const out = [];
            await Promise.all(reqs.map(async (r) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const resp = await fetch(r.url, {
                        headers,
                        credentials: 'include',
                        signal: controller.signal,
                    });
                    out.push({code: r.code, status: resp.status,
                              body: await resp.text()});
                } catch (e) {
                    out.push({code: r.code, status: 0, body: ''});
                } finally {
                    clearTimeout(timer);
                }
            }));
            return out;
        }""",
        {"reqs": reqs, "timeoutMs": API_CALL_TIMEOUT_MS},
    )

    ok = set()
    for r in results:
        code = r["code"]
        body = (r.get("body") or "").replace("for (;;);", "", 1).strip()
        if r["status"] != 200 or not body.startswith("{"):
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        rec = got_map.setdefault(code, {})
        for item in (data.get("items") or []):
            if item.get("code") == code:
                _absorb(rec, item, allow_username=True)
                break
        for o in walk(data):
            if o.get("code") == code:
                _absorb(rec, o, allow_username=False)
        if _has_views(rec):
            ok.add(code)
    return ok


async def scrape_reel(ctx, code, got, session_on):
    """Visit one reel page and absorb every metric + owner username."""
    def on_obj(o):
        if o.get("code") == code:
            _absorb(got, o, allow_username=_is_top_media(o))

    await capture(ctx, f"https://www.instagram.com/reel/{code}/",
                  on_obj, session_on=session_on, wait_for_grid=False)


async def scrape_user(ctx, user, codes, got, cfg, session_on):
    """
    Fill `got` {code: fields} for the wanted codes by scanning the user's
    grids. Reels tab first, then main grid, each up to two attempts.
    """
    want = set(codes)

    def on_obj(o):
        c = o.get("code")
        if c in want:
            _absorb(got.setdefault(c, {}), o, allow_username=_is_top_media(o))

    def done():
        return all(
            any(got.get(c, {}).get(k) is not None for k in VIEW_FIELDS)
            for c in want
        )

    for path in ("reels/", ""):
        for _attempt in (1, 2):
            if done():
                break
            await capture(ctx, f"https://www.instagram.com/{user}/{path}",
                          on_obj, max_scrolls=cfg.max_scrolls,
                          done=done, session_on=session_on)


# ---------------------------------------------------------------------------
# Fetch driver
# ---------------------------------------------------------------------------

async def run_fetch(urls, concurrency, use_session, cfg):
    """
    Orchestrate the whole fetch. Returns (rows, session_expired_flag).
    The API batch runs first on every reel; the API response supplies the
    owner username in the same call.
    """
    if use_session and concurrency > cfg.session_concurrency:
        print(f"(session mode: concurrency lowered to {cfg.session_concurrency} "
              f"to protect the account)")
        concurrency = cfg.session_concurrency

    # ---- Parse and de-duplicate input URLs ---------------------------------
    items, skipped, seen_codes = [], [], set()
    for raw in urls:
        url = clean(raw.strip())
        code, user = parse(url)
        if not code:
            skipped.append((raw, "no shortcode found"))
        elif code in seen_codes:
            skipped.append((raw, "duplicate"))
        else:
            seen_codes.add(code)
            items.append({"url": url, "code": code, "user": user})
    for raw, why in skipped:
        print(f"skipped ({why}): {raw}")

    if not items:
        return [], False

    state = {"done": 0, "abort": False, "expired": False}
    by_code = {}
    reported = set()
    sem = asyncio.Semaphore(concurrency)
    t0 = time.time()

    def _emit(code, *, force=False):
        """Print progress for `code`, once. Deferred unless force=True."""
        if code in reported:
            return
        got = by_code.get(code, ({}, None))[0]
        has = _has_views(got)
        if not force and not has:
            return
        reported.add(code)
        state["done"] += 1
        print(f"[{state['done']}/{len(items)}] {code} "
              f"@{got.get('_username') or '?'}: "
              f"{'views ok' if has else 'no views'} "
              f"({int(time.time() - t0)}s)")

    async with browser_context(cfg, use_session) as ctx:
        pages = []
        try:
            # ------------------------------------------------------------
            # Phase 1: API batch on ALL reels
            # ------------------------------------------------------------
            batches = [items[i:i + cfg.batch_size]
                       for i in range(0, len(items), cfg.batch_size)]
            print(f"\nphase 1: {len(items)} reel(s) via "
                  f"{len(batches)} API batch(es)")

            pool_size = min(cfg.pool_size, len(batches), concurrency)
            pool_size = max(1, pool_size)
            pages = await warm_pages(ctx, pool_size, use_session)

            pool: asyncio.Queue = asyncio.Queue()
            for p in pages:
                pool.put_nowait(p)

            async def api_batch(batch):
                if state["abort"]:
                    return
                await asyncio.sleep(random.uniform(0.3, 1.0))
                if state["abort"]:
                    return
                codes = [i["code"] for i in batch]
                got_map = {c: dict(by_code.get(c, ({}, None))[0]) for c in codes}
                page = await pool.get()
                try:
                    await asyncio.wait_for(
                        scrape_reels_api_batch(page, codes, got_map, use_session),
                        timeout=25,
                    )
                except SessionExpired as e:
                    print("!! session problem:", e)
                    state["expired"] = state["abort"] = True
                except asyncio.TimeoutError:
                    pass
                except Exception as e:
                    print(f"batch: {type(e).__name__} {e}")
                finally:
                    pool.put_nowait(page)
                for c in codes:
                    by_code[c] = (got_map.get(c, {}), now())
                    _emit(c)

            await asyncio.gather(*[api_batch(b) for b in batches])

            # ------------------------------------------------------------
            # Phase 2: resolve owners for reels the API missed (bare URLs only)
            # ------------------------------------------------------------
            if not state["abort"]:
                no_owner = [
                    i for i in items
                    if not _has_views(by_code.get(i["code"], ({}, None))[0])
                    and not (
                        by_code.get(i["code"], ({}, None))[0].get("_username")
                        or i["user"]
                    )
                ]
                if no_owner:
                    print(f"\nphase 2: resolving owner for {len(no_owner)} reel(s) "
                          f"via reel-page visit")

                    async def resolve_owner(item):
                        async with sem:
                            if state["abort"]:
                                return
                            got = by_code.setdefault(item["code"], ({}, None))[0]
                            try:
                                await asyncio.wait_for(
                                    scrape_reel(ctx, item["code"], got, use_session),
                                    cfg.account_timeout,
                                )
                            except SessionExpired as e:
                                print("!! session problem:", e)
                                state["expired"] = state["abort"] = True
                            except asyncio.TimeoutError:
                                pass
                            except Exception as e:
                                print(f"{item['code']}: {type(e).__name__} {e}")
                            by_code[item["code"]] = (got, now())
                            _emit(item["code"])
                            u = got.get("_username") or item["user"]
                            print(f"  owner: {item['code']} -> @{u or '?'}")

                    await asyncio.gather(*[resolve_owner(i) for i in no_owner])

            # ------------------------------------------------------------
            # Phase 3: grid fallback for anything still missing views
            # ------------------------------------------------------------
            if not state["abort"]:
                missing = [
                    i for i in items
                    if not _has_views(by_code.get(i["code"], ({}, None))[0])
                ]
                groups = defaultdict(list)
                for i in missing:
                    got = by_code.get(i["code"], ({}, None))[0]
                    owner = got.get("_username") or i["user"]
                    if owner:
                        groups[owner].append(i["code"])

                if groups:
                    total = sum(len(c) for c in groups.values())
                    print(f"\nphase 3: grid fallback for {total} reel(s) "
                          f"across {len(groups)} account(s)")

                    async def grid_group(user, codes):
                        async with sem:
                            if state["abort"]:
                                return
                            got = {c: dict(by_code.get(c, ({}, None))[0])
                                   for c in codes}
                            try:
                                await asyncio.wait_for(
                                    scrape_user(ctx, user, codes, got,
                                                cfg, use_session),
                                    cfg.account_timeout,
                                )
                            except SessionExpired as e:
                                print("!! session problem:", e)
                                state["expired"] = state["abort"] = True
                            except asyncio.TimeoutError:
                                print(f"{user}: grid timeout (keeping partial data)")
                            except Exception as e:
                                print(f"{user}: grid {type(e).__name__} {e}")
                            for c in codes:
                                by_code[c] = (got.get(c, {}), now())
                                _emit(c)
                            found = sum(1 for c in codes
                                        if _has_views(got.get(c, {})))
                            print(f"   {user}: recovered {found}/{len(codes)} reels")

                    await asyncio.gather(*[grid_group(u, c)
                                           for u, c in groups.items()])

            for i in items:
                _emit(i["code"], force=True)
        finally:
            for p in pages:
                try:
                    await p.close()
                except Exception:
                    pass

    # ---- Build output rows ------------------------------------------------
    rows = []
    for i in items:
        got, at = by_code.get(i["code"], ({}, None))
        user = got.get("_username") or i["user"]
        rec = {k: v for k, v in got.items() if not k.startswith("_")}
        plays = rec.get("play_count")
        likes = rec.get("like_count")
        if _has_views(rec):
            status = "ok"
        elif likes is not None:
            status = "likes only (no views)"
        elif state["expired"]:
            status = "session_expired"
        elif not user:
            status = "owner unknown"
        else:
            status = "not found"
        rows.append({
            "url": i["url"], "username": user, "shortcode": i["code"],
            "plays": plays,
            "ig_play_count": rec.get("ig_play_count"),
            "fb_play_count": rec.get("fb_play_count"),
            "view_count": rec.get("view_count"),
            "video_view_count": rec.get("video_view_count"),
            "video_play_count": rec.get("video_play_count"),
            "likes": likes,
            "comments": rec.get("comment_count"),
            "likes_suspect": bool(likes == 3 and (plays or 0) > 1000),
            "posted_at": epoch_iso(rec["taken_at"]) if rec.get("taken_at") else None,
            "status": status,
            "fetched_at": at,
        })
    return rows, state["expired"]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def n(v):
    return "-" if v is None else f"{v:,}"


def print_rows(rows):
    for r in rows:
        print(f"{r['shortcode']:<13} @{(r['username'] or '?'):<22} "
              f"plays {n(r['plays']):>10}  "
              f"ig {n(r['ig_play_count']):>9}  "
              f"fb {n(r['fb_play_count']):>9}  "
              f"vc {n(r['view_count']):>9}  "
              f"vvc {n(r['video_view_count']):>9}  "
              f"vpc {n(r['video_play_count']):>9}  "
              f"likes {n(r['likes']):>9}  "
              f"comments {n(r['comments']):>7}  "
              f"{r['status']}")
    counts = Counter(r["status"] for r in rows)
    print(f"\n{len(rows)} reels: " + ", ".join(f"{c} {s}" for s, c in counts.items()))


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_login(cfg):
    """
    Open a real Chrome window, let the user log in by hand, then save the
    cookies to ig_state.json. This is the only place a login page is
    touched; the script never sees a password.

    In CDP mode this command is a no-op: the browser is already running,
    so you log in there instead.
    """
    if cfg.cdp_url:
        print(f"CDP mode: you're attached to {cfg.cdp_url}.")
        print("Log in directly in that Chrome window. ig_state.json is not used.")
        print(f"Then run:  python ig-scrapV4.py check --cdp-url {cfg.cdp_url}")
        return

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=False)
        except Exception:
            browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(locale="en-US",
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto("https://www.instagram.com/accounts/login/")
        print("\n1. In the Chrome window that opened, log in yourself "
              "(2FA / 'Was this you?' too).")
        print("2. Wait until you can see your Instagram feed or profile.")
        input("3. Come back here and press Enter to save the session... ")
        cookies = ctx.cookies("https://www.instagram.com")
        sid = next((c for c in cookies if c["name"] == "sessionid"), None)
        if not sid:
            print("\nNo sessionid cookie found, so the login did not complete. "
                  "Nothing saved.")
            browser.close()
            sys.exit(1)
        ctx.storage_state(path=cfg.state)
        browser.close()
    print(f"\nSaved {cfg.state}")
    if sid.get("expires", -1) and sid["expires"] > 0:
        print("sessionid expires:", time.strftime("%Y-%m-%d",
                                                   time.gmtime(sid["expires"])))
    print("Reminder: ig_state.json is as sensitive as a password. "
          "Do not share or commit it.")
    print("Next:  python ig-scrapV4.py check")


def cmd_check(cfg):
    """
    Verify the session. In CDP mode we skip the ig_state.json read for
    the local check (the running browser holds the cookies) but still
    inject the state file into the CDP context when probing.
    """
    if not cfg.cdp_url:
        sid, err = state_file_ok(cfg.state)
        if err:
            print(f"Session: {err}. Run  python ig-scrapV4.py login  first.")
            sys.exit(1)
        exp = sid.get("expires", -1)
        if exp and exp > 0:
            left = (exp - time.time()) / 86400
            print(f"Session file OK. sessionid cookie expires "
                  f"{time.strftime('%Y-%m-%d', time.gmtime(exp))} "
                  f"({left:.0f} days left).")
        else:
            print("Session file OK (sessionid is a session cookie with no "
                  "fixed expiry).")
    else:
        print(f"CDP mode: checking the running browser at {cfg.cdp_url}...")

    async def live():
        async with browser_context(cfg, True) as ctx:
            page = await ctx.new_page()
            try:
                await page.goto("https://www.instagram.com/accounts/edit/",
                                wait_until="domcontentloaded", timeout=60000)
                await page.wait_for_timeout(3000)
                return page.url
            finally:
                await page.close()

    print("Checking with Instagram (opens a browser briefly)...")
    url = asyncio.run(live())
    if "/accounts/login" in url or "/challenge" in url:
        print(f"RESULT: session NOT valid (Instagram sent it to "
              f"{url.split('?')[0]}).")
        if cfg.cdp_url:
            print("Log into Instagram in the running Chrome window, then "
                  "run check again.")
        else:
            print("Run  python ig-scrapV4.py login  again. If you see a "
                  "challenge, approve it in the Instagram app.")
        sys.exit(1)
    print("RESULT: session is VALID.")


def cmd_fetch(cfg, file):
    """Run a fetch and write the results as CSV + JSON into cfg.out_dir."""
    urls = read_urls(file)
    rows, expired = asyncio.run(run_fetch(urls, cfg.concurrency, cfg.session_on, cfg))
    print()
    print_rows(rows)
    if expired:
        print("\n!! The session expired or needs a challenge during this run. "
              "Run login again.")
    if rows:
        out_dir = Path(cfg.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        tag = "session" if cfg.session_on else "loggedout"
        csv_path = out_dir / f"fetch_{tag}_{stamp}.csv"
        json_path = out_dir / f"fetch_{tag}_{stamp}.json"
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=rows[0].keys())
            w.writeheader()
            w.writerows(rows)
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        print(f"saved {csv_path} and {json_path}")


def cmd_compare(cfg, file):
    """Run logged-out vs session side-by-side."""
    urls = read_urls(file)
    print("=== run 1: logged out ===")
    if cfg.cdp_url:
        print("(CDP mode: both runs use the running browser's session)")
    out, _ = asyncio.run(run_fetch(urls, cfg.concurrency, cfg.session_on, cfg))
    print("\n=== run 2: with the login session ===")
    inn, expired = asyncio.run(run_fetch(urls, cfg.concurrency, True, cfg))
    a = {r["shortcode"]: r for r in out}
    b = {r["shortcode"]: r for r in inn}

    def line(label, r):
        return (f"   {label:<11}: plays {n(r['plays']):>12}  "
                f"ig {n(r['ig_play_count']):>10}  "
                f"fb {n(r['fb_play_count']):>9}  "
                f"vc {n(r['view_count']):>9}  "
                f"vvc {n(r['video_view_count']):>9}  "
                f"vpc {n(r['video_play_count']):>9}  "
                f"likes {n(r['likes']):>9}  "
                f"comments {n(r['comments']):>7}  [{r['status']}]")

    print("\n================ SIDE BY SIDE ================")
    for code in a:
        ra, rb = a[code], b.get(code)
        print(f"\n{code}  @{ra['username']}")
        print(line("logged out", ra))
        if rb:
            print(line("session", rb))
            if ra["plays"] and rb["plays"]:
                d = rb["plays"] - ra["plays"]
                print(f"   plays diff (session - logged out): "
                      f"{d:+,} ({d / ra['plays']:+.2%})")
            elif ra["view_count"] and rb["view_count"]:
                d = rb["view_count"] - ra["view_count"]
                print(f"   view_count diff: {d:+,} ({d / ra['view_count']:+.2%})")
    if expired:
        print("\n!! The session expired or needs a challenge during run 2. "
              "Run login again.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--state", default="ig_state.json",
                        help="session file (default ig_state.json)")
    common.add_argument("--headless", action="store_true",
                        help="hide the browser window (launch mode only)")
    common.add_argument("--cdp-url", default=None,
                        help="attach to a running Chrome via CDP "
                             "(e.g. http://ig-reel-chrome:9222); implies "
                             "session mode. Falls back to IG_CDP_URL env var.")
    common.add_argument("--timeout", type=int, default=DEFAULT_ACCOUNT_TIMEOUT,
                        help=f"per-account timeout in seconds "
                             f"(default {DEFAULT_ACCOUNT_TIMEOUT})")
    common.add_argument("--max-scrolls", type=int, default=DEFAULT_MAX_SCROLLS,
                        help=f"max scroll steps per grid page "
                             f"(default {DEFAULT_MAX_SCROLLS})")
    common.add_argument("--session-concurrency", type=int,
                        default=DEFAULT_SESSION_CONCURRENCY,
                        help=f"max parallel jobs in session mode "
                             f"(default {DEFAULT_SESSION_CONCURRENCY})")
    common.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE,
                        help=f"reels per API batch (default {DEFAULT_BATCH_SIZE})")
    common.add_argument("--pool-size", type=int, default=DEFAULT_POOL_SIZE,
                        help=f"warm pages for API batches "
                             f"(default {DEFAULT_POOL_SIZE})")
    common.add_argument("--out-dir", default="out",
                        help="directory for CSV/JSON output (default out)")

    ap = argparse.ArgumentParser(description="Instagram login-session test tool")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", parents=[common],
                   help="log in by hand once and save the session")
    sub.add_parser("check", parents=[common],
                   help="verify the saved session is still valid")

    pf = sub.add_parser("fetch", parents=[common], help="fetch reel metrics")
    pf.add_argument("file", nargs="?", default="reels.txt")
    pf.add_argument("--session", action="store_true",
                    help="use the saved login session")
    pf.add_argument("--concurrency", type=int, default=1)

    pc = sub.add_parser("compare", parents=[common],
                        help="logged out vs session, side by side")
    pc.add_argument("file", nargs="?", default="reels.txt")
    pc.add_argument("--concurrency", type=int, default=1)

    args = ap.parse_args()

    # Env fallback for CDP URL so the container doesn't need to pass it
    # on every command line.
    if args.cdp_url is None:
        args.cdp_url = os.environ.get("IG_CDP_URL")

    # Cap scrolls so total scroll time can't exceed the account timeout.
    effective_scrolls = min(
        args.max_scrolls,
        max(1, (args.timeout - 10) // (SCROLL_WAIT_MS // 1000)),
    )

    # CDP mode implies session.
    session_on = bool(args.cdp_url) or getattr(args, "session", False)

    cfg = Config(
        state=args.state,
        headless=args.headless,
        max_scrolls=effective_scrolls,
        account_timeout=args.timeout,
        concurrency=getattr(args, "concurrency", 1),
        session_concurrency=args.session_concurrency,
        batch_size=args.batch_size,
        pool_size=args.pool_size,
        session_on=session_on,
        cdp_url=args.cdp_url,
        out_dir=args.out_dir,
    )

    if args.cmd == "login":
        cmd_login(cfg)
    elif args.cmd == "check":
        cmd_check(cfg)
    elif args.cmd == "fetch":
        cmd_fetch(cfg, args.file)
    elif args.cmd == "compare":
        cmd_compare(cfg, args.file)


if __name__ == "__main__":
    main()
