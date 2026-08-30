# Architecture

Two programs, one wire, and a laptop that must work with the other one switched off.

## The shape of it

```
┌─────────────────┐        ┌──────────────────┐        ┌─────────────────┐
│     Kaggle      │        │ Telegram channel │        │  this laptop    │
│                 │ upload │                  │download│                 │
│  GPU passes     ├───────▶│  reels           │───────▶│  mirror.py      │
│  transcription  │        │  evidence shards │        │  atlas/ingest   │
│  vision models  │        │  db bundles      │        │  the ten screens│
│                 │        │  (newest pinned) │        │                 │
│  transient      │        │  permanent       │        │  permanent      │
└─────────────────┘        └──────────────────┘        └─────────────────┘
        │                           ▲                          │
        └───────────────────────────┴──────────────────────────┘
                    capture/ uploads reels from here too
```

Neither program imports the other. Neither can call the other. The channel is the
entire interface and `WIRE.md` is its specification — read that first if you are
changing anything that crosses the gap, because no type checker can catch a mistake
there.

**The laptop must run standalone.** Kaggle can be off for a week; everything except
new model output still works. That constraint is why `runners/` exists, why the
mirror's target list comes from the capture ledger rather than the search index, and
why nothing in the reader path requires the network.

## Process layout

One Python process. Three things in it:

1. **The window** — pywebview driving Edge WebView2, on the main thread.
2. **The server** — uvicorn in a daemon thread, `127.0.0.1:7000`.
3. **Background workers** — the mirror loop, the engine worker, the capture engine.
   Each is a daemon thread with its own cadence.

`desktop/__main__.py` starts them in a fixed order: credentials into the environment
first (before any module that reads one is imported), then the server on a port
confirmed free, then the window once the server answers. Racing step 3 against step 2
costs a WebView2 error page that is never replaced.

**One FastAPI app, one lifespan, one middleware stack** — `server/app.py`. The
upstream repository ran two applications and mounted one inside the other; this is the
structural fix for that.

## Request path

```
web/src/lib/api.ts ──▶ 127.0.0.1:7000 ──▶ server/app.py
                                            ├─ atlas/server.py        (search, graph, data, media)
                                            ├─ server/desktop_routes.py (mirror, derived, engine)
                                            ├─ server/admin_routes.py   (creds, restore, disk, log)
                                            └─ capture/routes.py        (intake)
```

**112 routes.** The largest groups: `api/capture` 23, `api/graph` 9, `api/map` 9,
`api/mirror` 8, `api/admin` 7, `api/engine` 7, `api/derived` 6, `api/library` 6,
`api/roadmap` 5, `api/vsearch` 5.

`web/tools/api_audit.py` checks every URL the client builds against every route the
server answers, including parameter names and whether a required parameter is declared
optional. It is the only thing standing between a rename and a silent dead link.
Current: **102 client URLs, 0 broken.**

## Packages

### Root — one file per concern

| module | lines | owns |
|---|---|---|
| `paths.py` | 383 | where everything lives. Every other module asks this |
| `config.py` | 163 | credentials resolved live via PEP 562 `__getattr__`, no fallbacks |
| `creds.py` | 1,464 | the credential store: env, Kaggle secrets, local file |
| `logger.py` | 236 | one log line format, and the redaction choke point |
| `dbhealth.py` | 234 | probe, quarantine, sidecar invalidation |
| `mirror.py` | 1,136 | the whole channel onto this disk, once, byte-verified |
| `derive.py` | 716 | the four playback artefacts, and `have()` |
| `engine_queue.py` | 817 | single-worker queue in `jobs.db` |
| `library.py` | 367 | watched local folders, indexed in place |
| `studio.py` | 1,598 | deconstruct, patterns, beat sheet |
| `tgcompat.py` | 442 | the only door to a pyrogram `Client` in this repo |
| `tg_transport.py` | 381 | Bot API over HTTPS, every call deadlined |
| `shardwriter.py` | 221 | the writer half of the wire format |
| `db_export.py` | 695 | seal, compress, upload, pin |
| `db_restore.py` | 908 | find pinned, inspect, apply |

### `atlas/` — the reader

| module | lines | owns |
|---|---|---|
| `server.py` | 1,611 | routes, boot sequence, status |
| `media.py` | 1,589 | range requests, channel streaming, disconnect classification |
| `ingest.py` | 1,468 | channel scan, shard import, bundle import |
| `graph.py` | 1,179 | five schema-derived edge families, precomputed |
| `index.py` | 921 | FTS5 build, vector build, build ids |
| `reflect.py` | 896 | dimension links, list columns, EAV pairs |
| `maps.py` | 874 | three archive-wide projections |
| `tgchannel.py` | 984 | MTProto reader, generation counter, log ring |
| `search.py` | 757 | BM25 + dense, RRF, video grouping |
| `vsearch.py` | 714 | frame/image/text → frames, strided then exact |
| `roadmap.py` | 701 | subsumption → layered DAG |
| `pgdump.py` | 294 | plain `pg_dump` → SQLite, no server |
| `encoder.py` | 230 | CLS pooling, asymmetric prefix, L2 norm |
| `config.py` | 226 | credential forwarding into the package |

### `capture/` — the intake

`engine.py` 985 · `ledger.py` 836 · `backfill.py` 733 · `upload.py` 612 ·
`fetch.py` 527 · `seed.py` 479 · `inputs.py` 467 · `routes.py` 444 ·
`assets.py` 423 · `mtproto.py` 302 · `pacing.py` 264 · `igexport.py` 228

`ledger.py` is the permanent record and the mirror's target list. `pacing.py` is 264
lines whose entire job is to go slower, which is correct when the penalty for
exceeding a rate limit is losing the account.

### `runners/` — local compute

`__init__.py` 905 · `signal.py` 755 · `ff.py` 503 · `structure.py` 401

**Ten of the catalogue's fifty-odd passes have an implementation.** Every other id
returns a reason from `blocked()`, the queue writes it into the job row, and the
Engine tab shows it. `unrunnable` with *"no runner on this machine"* is a different
row from a pass that ran and failed — that distinction is the entire point of the
package, because the code it replaced slept 50 ms and reported success.

The observer id folds the method into the hash. Upstream computes `motion` with an
OpenCV affine fit; this computes it from ffmpeg frame differences. Same component id,
different method — they must not hash alike, or whichever landed first would claim
the other's rows.

### `sizing/` — will it fit

`registry.py` 1,407 · `base.py` 990 · `resources.py` 273

Every model declared as data: size, requirements, output. `resources.probe()` measures
free VRAM, RAM, disk, compute capability and usable precision *now*. So the answer to
"can I run this" arrives before a 2.5 GB download, by name and with a reason.

### `server/` and `desktop/`

`server/app.py` 454 · `admin_routes.py` 480 · `desktop_routes.py` 461
`desktop/__main__.py` 365 · `make_icon.py` 217 · `make_shortcut.py` 124

The pywebview bridge exposes four methods to the page — a native folder picker, reveal
HOME in Explorer, reveal a watched path, and open a permalink in the real browser. The
last two validate their input, because a string that arrived over the bridge must not
reach a shell resolver unchecked: `open_path` requires an existing path, and `open_url`
requires `https` plus an allowlisted host. A plain `<a href>` cannot do the last one —
inside a webview an external link navigates *the application* to Instagram, and there
is no back button, because the window is the app.

### `web/src/` — the interface

Ten views (one per route), seventeen components, seven library modules, one
stylesheet.

| file | lines |
|---|---|
| `styles/main.css` | 5,649 — all of it, no CSS-in-JS |
| `types.ts` | 1,722 |
| `views/Studio.tsx` | 1,697 |
| `views/Capture.tsx` | 1,545 |
| `views/Admin.tsx` | 1,119 |
| `views/Graph.tsx` | 976 |
| `lib/api.ts` | 947 |
| `views/Engine.tsx` | 780 |
| `views/Watch.tsx` | 726 |
| `views/Roadmap.tsx` | 704 |
| `components/GraphCanvas.tsx` | 614 |
| `views/Data.tsx` | 480 |
| `views/Home.tsx` | 423 |
| `views/Search.tsx` | 299 |
| `views/Library.tsx` | 282 |

`lib/store.ts` holds the shared poll — 3 s for mirror and queue, 60 s for hardware.
The status bar reads from it rather than fetching its own copy, because a strip that
fetched independently would double the request rate to display the same values.

`lib/router.ts` is 194 hand-written lines for ten routes and one dynamic segment. The
player is a route: `/watch/<key>?t=14.32&q=hook`.

`web/src/api/schema.d.ts` and `web/dist/` are generated and **not committed**. A fresh
clone needs `npm run build`.

## Threading and the database

SQLite, WAL mode, **30-second busy timeout on every connection** — the default 5 is
shorter than a WAL checkpoint under a mirror worker writing while the UI reads, and
the symptom of getting it wrong is "database is locked" surfacing in a search box.

**One writer at a time.** This is why bulk actions enqueue sequentially rather than
concurrently: a hundred simultaneous writes against a single-writer database on a
local socket does not go faster, it produces lock errors for no gain, because there
was never any network latency to hide.

Read paths open the archive and the ledger **read-only via URI**, so a corrupt
`atlas.db` degrades the mirror to ledger-only rather than breaking it.

## Two Telegram clients, one predicate

`atlas/tgchannel.py` reads the channel for playback and the scan.
`capture/mtproto.py` uploads files over the Bot API's 50 MB ceiling.

They are independent clients with independent sessions, and they share exactly one
thing: `tgcompat.is_transport_error()` — the judgement of whether a failure is a dead
socket (rebuild the session) or a refusal (rebuilding cannot help). A predicate copied
into both is a predicate that will be improved in one.

Both go through `tgcompat.client()`, which is the **only** way to construct a pyrogram
`Client` in this repository. It creates an event loop on the calling thread — pyrogram
imports fail on worker threads since Python 3.10, with an error that is not
`ImportError` — and widens the channel-id floor before the object that will resolve a
peer exists. Grepping for `from pyrogram import Client` is a complete audit of whether
anyone bypassed it.

## Degradation, by design

Every optional dependency is a feature that reports its own absence:

| absent | consequence | how it is stated |
|---|---|---|
| torch / sentence-transformers | no dense retrieval | `semantic —` in the status bar; home screen says so in words |
| fts5 | LIKE-based search | logged at index time |
| sklearn / umap | no Maps view | the view reports it |
| cv2, soundfile, faster-whisper | those passes are `unrunnable` | Engine names the library |
| PostgreSQL | none — dumps are parsed directly | n/a |

Nothing in this table raises on import. A slower search is a working app; an exception
at import is not.

See [DECISIONS.md](DECISIONS.md) for why each of these is shaped the way it is.
