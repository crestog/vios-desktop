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
        channel is a network away.
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
        log("app up — boot running in the background", SUB)
        yield
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

    # engine/ and admin/ routers land here as they are written. Nothing is
    # stubbed: a route that does not exist yet returns 404, which is the truth.

    _mount_web(app)
    log(f"{total} API route(s) on one app", SUB)
    return app


app = create_app()
