"""
server.app — one FastAPI application, one lifespan, one middleware stack.

This file is the structural half of *"i think our current code breaks the flow,
and not connected properly"*. In the Kaggle repository the answer to "how many
apps am I running?" was two: `ui_server.py:940` did

    app.mount("/atlas", _atlas_server.app)

which mounts a **whole second FastAPI instance** — its own lifespan, its own
middleware, its own exception handlers — and then joined the two halves in the
browser with four iframes and three hard page loads. That is why the tabs could
not share state: they were not one page, and behind them they were not one
application either.

There is no `ui_server.py` in this repository, so fixing it is not a migration.
It is this file existing.

**Why routes are adopted rather than re-decorated.** `atlas/server.py` declares
56 routes with `@app.get` / `@app.post`, and `capture/routes.py` another 24 on
its own `APIRouter`. Rewriting 80 decorators to move them onto a shared router
would be a large diff whose every line could silently change a path, a
`response_class` or a byte-range behaviour — for no behaviour change at all. So
this file takes the finished route objects off those routers and re-registers
them here. A `Route` is self-contained: its endpoint, path, methods and
dependencies were all bound at decoration time.

**What is deliberately left behind.** Both modules also serve their old
frontends — `/`, `/atlas.css`, `/atlas.js`, `/favicon.ico`, `/sitemap.js`,
`/capture`. Only `/api/*` is adopted, so those five HTML/asset routes simply do
not exist here and the new frontend owns the root. That is the whole of the
"delete the old UI" work: not a deletion, a filter.
"""

from __future__ import annotations

import contextlib
import os

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

import paths
from logger import vios_log as log

SUB = "SYS"


# ══════════════════════════════════════════════════════════════════════════
# THE BACKGROUND WORKERS
# ══════════════════════════════════════════════════════════════════════════
def _off(name: str) -> bool:
    """Is this `VIOS_*_AUTOSTART` switch turned off? Follows `capture.backfill`."""
    return str(os.environ.get(name, "1")).strip().lower() in (
        "0", "false", "no", "off")


def start_workers() -> None:
    """Start the two daemon workers this window owns. Never raises; logs instead.

    Neither of them was started anywhere. `mirror.start()` and
    `engine_queue.start()` both existed, both were reachable over POST, and both
    were only ever called by a person pressing a button — so a fresh install
    mirrored nothing until someone found the Admin tab, and a reel queued for
    derive sat in `engine_jobs` in `pending` forever while the Engine tab
    truthfully reported a worker that was idle because it did not exist.

    Both are idempotent and both raise the pause/resume routes' state, not this
    function's, so pressing pause still pauses and a later `start` is a no-op
    rather than a second thread.

    **The mirror runs even with no Telegram credentials**, which looks wrong and
    is not: half of what it does is derive proxies, sprites and keyframes for
    files already on this disk, and that is the half that makes playback instant.
    Only the download half needs the channel, and it now returns early when the
    channel is unreachable instead of failing once per reel per cycle.

    `VIOS_MIRROR_AUTOSTART=0` and `VIOS_ENGINE_AUTOSTART=0` opt out — for a
    session opened to read the archive rather than to fill it in.
    """
    import engine_queue                                        # noqa: PLC0415
    import mirror                                              # noqa: PLC0415

    for name, flag, start, what in (
            ("mirror", "VIOS_MIRROR_AUTOSTART", mirror.start,
             "downloads what is only in the channel, derives what is on disk"),
            ("engine", "VIOS_ENGINE_AUTOSTART", engine_queue.start,
             "runs the components queued against a reel")):
        if _off(flag):
            log(f"{name} worker not started — {flag}=0", SUB)
            continue
        try:
            start()
            log(f"{name} worker started — {what}", SUB)
        except Exception as e:                                  # noqa: BLE001
            log(f"{name} worker did not start — {type(e).__name__}: {e}",
                SUB, "WARN")


def seed_capture() -> None:
    """Read the channel once, in the background, so capture is safe to press.

    The capture engine now refuses to run against a ledger that has never been
    told what the channel already holds — see `capture.engine._seed_gate`. That
    guard is correct and, on its own, it is a wall: a fresh install would open
    the Capture tab, press Start, and be handed a paragraph about a scan it has
    no way to know it needed. This is the other half. The scan is three or four
    MTProto calls over ~600 message ids and takes seconds, so doing it at boot
    costs nothing and the tab is simply ready.

    Four ways this does nothing, all of them normal:
      * `VIOS_SEED_AUTOSTART=0`, for a session opened to read the archive.
      * no bot token or channel id — nothing to scan with, and the Capture tab
        is where those get set. A later manual scan does the same work.
      * no API id and hash — a bot cannot read channel history without them.
        This is the one case worth a WARN: capture will refuse, and the reason
        is a missing credential rather than anything the operator did.
      * already seeded — `seed_from_channel` resumes from its watermark, so this
        reads only what arrived since last time, which is usually nothing.

    Deliberately *not* the asset backfill. `capture.backfill.autostart()` runs
    the same seed and then uploads clips to the channel for every video missing
    an asset set — an outward-facing write, for an hour, from a laptop that in
    this design is the reader and not the processing plane. It stays behind its
    button.
    """
    import threading                                            # noqa: PLC0415

    def _go() -> None:
        try:
            from capture.engine import get_engine                # noqa: PLC0415
            eng = get_engine()
            if eng.telegram is None:
                log("channel not scanned — no bot token yet; set it in Capture "
                    "and the scan runs from there", SUB)
                return
            was = eng.ledger.seeded()["seeded"]
            res = eng.seed_ledger()
            if res.get("error"):
                log(f"channel scan failed — {res['error'][:160]}", SUB,
                    "WARN" if not was else "INFO")
                return
            if res.get("skipped"):
                log("channel unchanged since the last scan", SUB)
                return
            log(f"channel scan: {res.get('in_channel', 0)} video(s) seen, "
                f"{res.get('adopted', 0)} new to the ledger", SUB)
        except Exception as e:                                  # noqa: BLE001
            log(f"channel scan did not run — {type(e).__name__}: {e}",
                SUB, "WARN")

    if _off("VIOS_SEED_AUTOSTART"):
        log("channel not scanned — VIOS_SEED_AUTOSTART=0", SUB)
        return
    threading.Thread(target=_go, name="vios-seed", daemon=True).start()


def _stop_workers() -> None:
    """Ask both workers to finish on the way down. Never raises.

    They are daemon threads, so the process can exit without this — but a
    download in flight leaves a `.part` file behind, and `stop()` sets the event
    the loop checks between items. It is the difference between a clean shutdown
    and one that always has something to clean up next time.
    """
    import engine_queue                                        # noqa: PLC0415
    import mirror                                              # noqa: PLC0415

    for name, stop in (("mirror", mirror.stop), ("engine", engine_queue.stop)):
        try:
            stop()
        except Exception as e:                                  # noqa: BLE001
            log(f"{name} worker did not stop cleanly — "
                f"{type(e).__name__}: {e}", SUB, "WARN")


# ══════════════════════════════════════════════════════════════════════════
# ROUTE ADOPTION
# ══════════════════════════════════════════════════════════════════════════
def _adopt(app: FastAPI, router, label: str, keep=lambda p: True) -> int:
    """Move `router`'s matching routes onto `app`. Returns how many moved.

    Order is preserved, which matters: FastAPI matches by walking the list, so
    `/api/graph/schema` must stay ahead of `/api/graph/expand/{node_id:path}`
    exactly as it was declared.
    """
    moved = 0
    for route in list(getattr(router, "routes", [])):
        path = getattr(route, "path", "")
        if not path or not keep(path):
            continue
        app.router.routes.append(route)
        moved += 1
    log(f"{label}: {moved} route(s)", SUB)
    return moved


def _api_only(path: str) -> bool:
    """Adopt the API, leave both old frontends behind."""
    return path.startswith("/api/")


# ══════════════════════════════════════════════════════════════════════════
# THE FRONTEND
# ══════════════════════════════════════════════════════════════════════════
_UNBUILT = """<!doctype html>
<html><head><meta charset="utf-8"><title>VIOS — frontend not built</title>
<style>
 :root {{ color-scheme: dark }}
 body {{ background:#0b0b0d; color:#e7e7ea; font:15px/1.6 ui-sans-serif,system-ui;
        margin:0; display:grid; place-items:center; min-height:100vh }}
 main {{ max-width:46rem; padding:2rem }}
 h1 {{ font-size:1.5rem; font-weight:600; margin:0 0 .5rem }}
 p  {{ color:#9a9aa2 }}
 code {{ background:#17171b; padding:.15rem .4rem; border-radius:4px;
         font:13px ui-monospace,monospace; color:#c4b5fd }}
 pre {{ background:#17171b; padding:.9rem 1.1rem; border-radius:8px;
        border:1px solid #24242a; overflow-x:auto }}
 a {{ color:#7dd3fc }}
</style></head><body><main>
<h1>The API is running. The frontend is not built yet.</h1>
<p>The server is up and every endpoint below is live — this page is served
because <code>{dist}</code> holds no <code>index.html</code>.</p>
<pre>cd web
npm install
npm run build</pre>
<p>Or, for hot reload against this same live server, <code>npm run dev</code>.</p>
<p>Meanwhile the API answers directly:
<a href="/api/status">/api/status</a> ·
<a href="/api/channel">/api/channel</a> ·
<a href="/api/log">/api/log</a></p>
</main></body></html>"""


def _mount_web(app: FastAPI) -> None:
    """Serve the built frontend, with a real SPA fallback.

    `StaticFiles(html=True)` is not enough on its own. Every view here is a
    route — `/watch/<key>?t=14.32` is the whole point of the player being
    linkable — and a deep link is a cold GET for a path that exists in the
    router only inside the browser. Static files answer 404 for those. So
    unknown non-API paths fall through to `index.html` and let the frontend
    router resolve them, which is what makes a pasted timestamp link work.
    """
    dist = paths.WEB_DIR
    assets = os.path.join(dist, "assets")
    if os.path.isdir(assets):
        # Hashed filenames, so they are immutable and may be cached hard.
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str = ""):
        # Registered last, so every /api/ route above already claimed its path.
        # An unmatched /api/ request must still 404 as an API, not hand a JSON
        # client a page of HTML to fail to parse.
        if full_path.startswith("api/"):
            return Response('{"detail":"Not Found"}', status_code=404,
                            media_type="application/json")

        direct = os.path.normpath(os.path.join(dist, full_path))
        if full_path and direct.startswith(dist) and os.path.isfile(direct):
            return _file(direct)

        index = os.path.join(dist, "index.html")
        if os.path.isfile(index):
            return _file(index, cache=False)
        return HTMLResponse(_UNBUILT.format(dist=dist), status_code=200)


_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript",
          ".mjs": "text/javascript", ".css": "text/css",
          ".json": "application/json", ".svg": "image/svg+xml",
          ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
          ".webp": "image/webp", ".ico": "image/x-icon",
          ".woff2": "font/woff2", ".woff": "font/woff",
          ".map": "application/json", ".webmanifest": "application/manifest+json"}


def _file(path: str, cache: bool = True) -> Response:
    with open(path, "rb") as f:
        body = f.read()
    ext = os.path.splitext(path)[1].lower()
    # index.html must never be cached: it names the hashed bundles, so a stale
    # copy points a fresh page at deleted files and the app boots to a blank
    # screen with two 404s in the console.
    control = ("public, max-age=31536000, immutable" if cache
               else "no-cache, must-revalidate")
    return Response(body, media_type=_TYPES.get(ext, "application/octet-stream"),
                    headers={"Cache-Control": control})


# ══════════════════════════════════════════════════════════════════════════
# THE APP
# ══════════════════════════════════════════════════════════════════════════
def create_app() -> FastAPI:
    from atlas import server as atlas_server

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        """The one lifespan. Both things it does have to happen here.

        The thread ceiling has to be raised from inside the running loop —
        anyio keeps the limiter in a `RunVar` bound to that loop, so setting it
        at import time sets it on nothing. And it has to be raised at all
        because ~50 adopted routes are sync `def`, which FastAPI runs in
        anyio's default 40-token pool; a route waiting on a SQLite lock holds
        its token for the whole `busy_timeout`, so a saturated pool queues in
        arrival order and the page cannot fetch its own script.

        Atlas's own boot — channel scan, then index — runs in a daemon thread
        started here rather than awaited, because the window must be
        interactive against local sqlite in well under two seconds and the
        channel is a network away. The mirror and the engine queue start the
        same way and for the same reason — see `start_workers`. `seed_capture`
        is a third: it teaches the capture ledger what the channel already
        holds, which is what makes the Capture tab's Start button safe to press.
        """
        try:
            import anyio.to_thread
            want = max(40, int(os.environ.get("VIOS_THREADPOOL", "192")))
            limiter = anyio.to_thread.current_default_thread_limiter()
            if want > limiter.total_tokens:
                log(f"worker threads {limiter.total_tokens} → {want}", SUB)
                limiter.total_tokens = want
        except Exception as e:                                  # noqa: BLE001
            log(f"could not raise the thread ceiling — "
                f"{type(e).__name__}: {e}", SUB, "WARN")

        atlas_server.start_boot()
        start_workers()
        seed_capture()
        log("app up — boot running in the background", SUB)
        yield
        _stop_workers()
        log("app down", SUB)

    app = FastAPI(title="VIOS", docs_url="/api/docs", redoc_url=None,
                  openapi_url="/api/openapi.json", lifespan=lifespan)

    # Pure ASGI, lifted from atlas/server.py, and pure ASGI on purpose: as
    # `@app.middleware("http")` this was BaseHTTPMiddleware, which counts the
    # bytes an endpoint emits against the Content-Length it announced. Every
    # `/api/play` response is an exact byte range, and a player seeking
    # mid-playback closes the socket early — completely normal — which surfaced
    # as `RuntimeError: Response content shorter than Content-Length` from
    # several frames inside an anyio ExceptionGroup, naming nothing.
    app.add_middleware(atlas_server._Timing)

    total = _adopt(app, atlas_server.app.router, "atlas", _api_only)

    try:
        from capture.routes import capture_router
        total += _adopt(app, capture_router, "capture", _api_only)
    except Exception as e:                                     # noqa: BLE001
        # Capture needs yt-dlp and gallery-dl. Losing that tab must not cost
        # the reader, which is the part used daily.
        log(f"capture routes unavailable — {type(e).__name__}: {e}",
            SUB, "WARN")

    try:
        from server.desktop_routes import router as desktop_router
        total += _adopt(app, desktop_router, "desktop", _api_only)
    except Exception as e:                                     # noqa: BLE001
        log(f"desktop routes unavailable — {type(e).__name__}: {e}",
            SUB, "WARN")

    try:
        from server.admin_routes import router as admin_router
        total += _adopt(app, admin_router, "admin", _api_only)
    except Exception as e:                                     # noqa: BLE001
        # Admin owns the credential form, which is where a user goes when
        # something else is already broken. Losing it silently would be the
        # worst possible tab to lose, so this logs at WARN like the others and
        # the frontend's own 404 handling says the rest.
        log(f"admin routes unavailable — {type(e).__name__}: {e}",
            SUB, "WARN")

    _mount_web(app)
    log(f"{total} API route(s) on one app", SUB)
    return app


app = create_app()
