# Decisions

Every entry is a choice that looks wrong, or arbitrary, until you know what was measured.
Format: the decision, the obvious alternative, and why the alternative loses.

## Retrieval

### Fuse ranks, not scores

**Reciprocal rank fusion** — `score(d) = Σ 1/(k + rank_r(d))`, k=60 from the original
paper — over a weighted sum of BM25 and cosine.

BM25 scores are unbounded and depend on the corpus; cosine sits in [-1, 1]. They are not
on the same scale, and no fixed weight makes them comparable across queries. A blend
needs a normalisation step, and every normalisation step is a tuning knob that is wrong
for some query.

RRF discards the scores and keeps only the positions, which is the part both methods
agree is meaningful. The effect worth naming: a passage ranked 1st by one method and 40th
by the other beats a passage ranked 15th by both. That is the behaviour you want, because
one method being confident is evidence and both being lukewarm is not.

Published figures for the same corpus shape: dense-only ~78% recall@10, fusion ~91%.

### No vector database

A few hundred thousand passages at 384 dimensions in float32 is under 1 GB resident. A
query is one `(N, 384) @ (384,)` matmul — memory-bandwidth-bound, milliseconds, and
**exact**.

An ANN index would add a service to run, a build step to keep in sync, a tuning knob
(`ef_search`, `nprobe`, whatever) and an approximation. It buys nothing until the matmul
stops fitting in RAM, which is around 10M passages. At that point this decision flips;
until then it is pure cost.

### Results are videos, not passages

A video's score is its best moment plus a damped contribution from the rest. Not the best
moment alone, and not the sum.

Best-alone throws away corroboration: a reel that mentions a technique once and a reel
built around it rank the same. A plain sum lets a long reel win on volume. Damping makes
extra evidence help while keeping it from dominating.

### Cross-space image comparison is forbidden, not discouraged

CLIP and SigLIP2 produce vectors of the same shape in unrelated spaces. A dot product
between them is a number, and the number is meaningless — not "worse", *meaningless*.

So text→frames and image→frames run in the `clip` space only, and the code refuses rather
than silently returning garbage that looks like results. Frame→frames needs no model at
all, which is why it works instantly.

### Strided first, exact second

~900 frames per video, 62 videos, ≈56,000 vectors ≈257 MB. Holding every frame resident
does not scale with the archive.

A strided matrix bounded by `VSEARCH_MAX_MB` ranks videos, then the top
`VSEARCH_CANDIDATES` are re-ranked against full-rate rows read from `vec_payload`. Coarse
where coarse is enough, exact where the answer is decided.

## Storage

### `select count(*) from sqlite_master`, not `PRAGMA integrity_check`

SQLite parses a database's schema **once per connection** and caches it. A process that
connected before the damage was written keeps serving pages perfectly — which is exactly
what happened on 30 August: every new connection raised
`malformed database schema (ix_vecpay_space) - no such table: main.vec_payload` while the
running app looked healthy.

Counting the schema table forces a fresh parse, so **the probe and the symptom are the
same event** rather than a proxy for it. And it is one cheap statement.

`integrity_check` walks every tree, is slow on a large archive, and on the real corrupt
file failed at `vtable constructor failed: moments_fts` **before reaching the actual
damage**. A boot check that cannot finish is a boot check that gets removed.

### Quarantine, never delete

Three rules, and each one was a way to make the failure worse:

1. **The file moves aside with a note.** A future SQLite build, or `sqlite3 .recover`,
   may read what this one cannot — and it is the only copy of whatever the last pass
   computed. Disk is not the constraint here; an unrecoverable mistake is.
2. **The journals go with it, database first.** A `-wal` left beside a *new empty*
   database is a second corruption, freshly manufactured: SQLite would replay committed
   frames belonging to pages that no longer mean what they meant. Dying between the two
   steps the right way round leaves a log with no database, which SQLite ignores. The
   wrong way round leaves a database that reads as complete while silently missing every
   change the log held.
3. **The derived sidecars go too.** `moments.vec` is a flat matrix whose row order is
   defined by rows in `atlas.db`. Keeping it beside a rebuilt database means search
   answers with vectors belonging to moments that no longer exist — wrong answers,
   silently, which is worse than no answers.

### `mirror.db` is its own file

It could be a table in `atlas.db`. It is not, because `atlas.db` is the one file the app
is willing to throw away — and the only way to rebuild proof-of-download is to download
everything again. **A corrupted search index would cost a re-download of the entire
archive.** One file, one job, outside the disposable one.

### `moments.vec` needs a build stamp; frame vectors do not

`moments.vec` is keyed by `moments.id`, and a reindex reassigns those ids — so the matrix
carries the build id it was computed against and a stale one is detected instead of
silently mis-ranking.

Frame vectors are keyed `(video_key, frame_idx)`, which no rebuild touches. They survive
any number of index rebuilds and can be built incrementally as shards land. Same problem,
two different answers, because the key is different.

### Completion markers instead of counting

`media/frames/<key>/index.json` is written **last**, after every image. A folder with
three of two hundred frames because ffmpeg was killed halfway is otherwise
indistinguishable from a finished one — you would have to count, and count against what?

Same rule as the mirror's byte check, and the same rule as the manifest: **a thing that
exists is not a thing that is finished, and the only reliable way to know the difference
is to write down a marker that can only exist afterwards.**

### Parse `pg_dump`, don't run PostgreSQL

`atlas/pgdump.py` is 294 lines that read a plain-format dump straight into SQLite. The
alternative is a PostgreSQL server on a laptop: a cluster to initialise, a service to
run, a version to match, a port, and a second thing that can be down.

One consequence worth knowing, because it shapes the graph: the plain format carries real
foreign keys as `ALTER TABLE` statements that the importer skips. So dimension links are
derived by the `<x>_id` → `<x>s` naming convention instead of from declared keys — which
is also why adding a `mood_id` column and a `moods` table tomorrow makes mood nodes
appear with no code change.

### WAL, and a 30-second timeout

`PRAGMA journal_mode=WAL` everywhere, and `SQLITE_TIMEOUT = 30`.

The default timeout is 5 seconds, which is shorter than a WAL checkpoint under a mirror
worker writing while the UI reads. The symptom of getting this wrong is `database is
locked` appearing in a search box.

### One writer, so bulk actions enqueue sequentially

Looks like a missed optimisation. It is not: SQLite allows one writer, and a hundred
concurrent writes against a local single-writer database produce `database is locked` for
no gain, because there was no network latency to hide in the first place.

## Telegram

### One door to a pyrogram client

`tgcompat.client()` is the only place a `Client` is constructed. It creates the event loop
*and* widens the channel-id floor, so this grep is a complete audit:

```bash
git grep -n "from pyrogram import Client"
```

The rule exists because the alternative failed in the most confusing possible way.
pyrogram's `MIN_CHANNEL_ID` is a **process-global constant**; the real channel id
`-1004435513595` is below it. With 3 of 5 construction sites patched, the app worked or
did not depending on which site ran first — "it works sometimes" is a much worse bug
report than "it never works".

**`MIN_CHAT_ID` must not be widened.** The chat branch is tested first with a bare
`MIN_CHAT_ID <= peer_id`, so widening it turns a loud `ValueError` into a confident,
wrong `"chat"`.

### A generation counter, not a boolean

The dead-socket fix. The old code answered "is the client up?" with
`self._client is not None` — an object check, not a socket check — so the first dropped
connection was permanent **by construction**. Measured: one `WinError 10053` at 10:53:19
poisoned every download until 11:49, retrying every 5 seconds and reporting itself
healthy the whole time.

A boolean flag is not enough either. With 20 threads on one dead socket, a flag lets 19
of them destroy a session that a sibling just finished negotiating. So a failing call asks
for **the generation it saw** to be retired, and a request to retire a generation that is
already gone is a no-op. `_REBUILD_COOLDOWN = 20.0` prevents a connect storm on top.

The second latch is the instructive part: `_run` set `_ready` in a `finally`, so a
*failed* build latched too, and `if self._thread is None` saw the dead thread and never
respawned. Fixing only the first latch would have looked like a fix and not been one.

### One transport predicate, shared

`tgcompat.is_transport_error()` is used by both MTProto clients — the reader
(`atlas/tgchannel.py`) and the uploader (`capture/mtproto.py`) — because a predicate
copied into both is a predicate that will be improved in one.

Both directions of getting it wrong are expensive. Too narrow, and a dead socket is
treated as a refusal: 56 minutes of `WinError 10053` every 5 seconds, six reels stranded,
`running: true`. Too broad, and a real refusal triggers an endless session rebuild.

### HTTPS first, MTProto only for what it cannot do

`tg_transport.py` is the Bot API over HTTPS and its guarantee is one sentence: **a call
either finishes or raises, never parks.**

What it replaced had no timeout anywhere in the path, no cancel, and a progress bar
computed as `pct = 55 + int(35 * n / total)` — so 72% of a two-part upload is a
*constant*, not a byte count. A number that cannot be wrong because it never measured
anything.

The Bot API costs are real: a 50 MB upload cap, a 20 MB `getFile` cap, and no
fetch-by-id. Bought off with an 18 MB part size, the `file_id` recorded into the manifest
at upload time, and the manifest located through `getChat`'s `pinned_message`. MTProto is
kept for exactly the things those caps forbid.

### Match disconnects by name, not by import

`atlas/media.py` recognises `ClientDisconnect`, `BrokenResourceError`,
`ClosedResourceError`, `EndOfStream`, `BrokenPipeError` and `ConnectionResetError` by
class name rather than importing them.

Two reasons. It survives whatever versions the environment happens to pin — the async
library, the web framework and the OS each report a mid-transfer teardown as a different
type. And **a missing package cannot turn the guard itself into the failure**, which is
what an import at the top of a module risks. It also recurses into `ExceptionGroup`,
because a disconnect inside a group of failures is still a disconnect.

None of these mean anything is wrong. Scrubbing the bar, closing the tab, or the player
deciding it has buffered enough all produce one. This is the difference between a log you
read and a log you ignore.

## Honesty

### Redact at the choke point, not at the call sites

Nobody wrote a credential to a log. `requests` and `httpx` put the **full request URL
into their own exception messages**, so a wifi drop arrived at `/api/log` as
`.../bot<id>:<secret>/getMe`, and from there into the browser and the file on disk.

Redacting in callers is the obvious fix and it is wrong, because it makes every future log
call a potential leak vector — somebody adds a line next year, forgets, and the hole is
back.

`logger.redact()` runs at the top of the single function every line passes through, so
console, ring buffer and rotating file are clean by construction. Live values replaced
with their *names*, longest first so a secret containing another is replaced whole, then
two shape-based regexes for anything token-shaped whose value has since changed, with a
5-second TTL so a token typed into Admin is redacted from the next line without a
restart. `config` is imported lazily inside the call: printing a line must never require
the credential system to be working.

`atlas/tgchannel.log()` delegates to the same function rather than keeping a second copy.

### `unrunnable` is not `failed`

The worst bug in the project's history was `time.sleep(0.05)` followed by marking the job
**completed**. Every pass in the catalogue reported success; none of them ran.

And the lie was **sticky**: the queue only re-queues jobs in state `failed` or
`unrunnable`, so a job that claimed success could never be re-queued. Re-running the
sweep would not have found it.

The fix is not really the runners — it is the counting. **Ten of ~50 passes have an
implementation on this laptop**, that number is not hidden, and every pass without one
returns a *reason* that is written into the job row and shown in Engine as *"no runner on
this machine"*, naming the absent library. Retryable the instant a runner exists, and a
completely different row from a pass that ran and failed. The whole point of the change is
that those two stop looking the same.

### The observer id folds in the method

The same measurement can be made two ways. Upstream computes motion by fitting an affine
transform with OpenCV; this laptop takes the mean absolute difference between frames out
of ffmpeg. Same pass name, same catalogue row, entirely different method.

If they hashed alike, `INSERT OR IGNORE` would let whichever landed first **silently claim
the other's rows**. So the method is part of the observer's identity: two observers, two
sets of numbers, both traceable to how they were made. That is what `/data` is for.

### An em dash, never a placeholder number

v1 shipped `4,812` reels and `68.4 GB` **typed into the page**. They looked right on the
machine they were typed on and lied everywhere else.

A number that has not arrived shows as `—` with a tooltip saying what would have filled
it. A count of zero is shown as zero. This is the rule that produced both visible honesty
markers: `semantic —` in the status bar, and *"Semantic search is off — searches are
keyword-only"* on Home.

Same rule for documentation: **measured or absent**. A specific number is checkable; a
vague one is unfalsifiable.

### An honest absence over a dead control

The design listed four bulk actions in Library. Two had no server behind them — there is
no collections table and no playback queue. It ships the ones that work and states
*"collections aren't built yet"*.

**A dead control teaches you the app is broken. An honest absence does not.**

### Warn, never evict

The mirror stops at the 12 GB floor and deletes nothing. There is no eviction policy,
because choosing what to throw away is not a decision an app makes silently about your
archive.

The floor itself is not politeness: **filling the system drive is not a recoverable state
for Windows**, so the app will not participate in it.

### Inspect, then apply

Restore is two buttons on purpose. The first downloads the bundle and reports what is in
it, touching nothing. A restore overwrites your database; you should be able to look
before you leap.

## Shape of the app

### pywebview, not a browser tab

One decisive reason: **a native folder picker.** No web page can open one, and watching
local folders was a requirement.

The bridge exposes four methods, and the two that take input validate it, because a string
that arrived over the bridge must not reach a shell resolver unchecked. `open_path`
requires an existing path — `startfile` on a non-path hands it to the URL handler.
`open_url` requires `https` and an allowlisted host, because inside a webview an external
`<a href>` navigates **the application**, and there is no back button.

### The player is a route, not a modal

`/watch/<key>?t=14.32&q=hook`. A modal cannot satisfy "a link shares the exact moment with
markers intact", because a modal is not addressable. Making the player a route means the
timestamp, the query and the markers are all in the URL.

### Credentials into the environment before any importer runs

The startup order is the whole file, and this is step one. Upstream, a session with all
four secrets stored correctly still printed *"Telegram disabled"* — because nothing had
asked yet, and the module that decides had already been imported.

### The window waits for the server

WebView2 renders a browser error page for a refused connection, and **that page is not
replaced** when the port comes up a moment later. So racing the server costs a blank
window and a relaunch; waiting for one successful request costs a few hundred
milliseconds and cannot fail that way.

### A hand-written router and one stylesheet

The router is 194 lines. A routing library is larger than that, and this app has ten
routes and one parameterised one.

One stylesheet rather than per-component CSS, because the CSS audit can then check every
class the components ask for against every class that exists — which catches the failure
that is worst to find by eye. A component styled with a class that does not exist renders
unstyled and looks like a layout bug.

### Two audits instead of two conventions

`api_audit.py` checks 102 client URLs against 112 server routes, including parameter
names and whether a required one is declared optional. `css_audit.py` checks classes.
Current state: 0 broken links.

The general form of this decision, and it is the one that recurs most in this project:
**audits instead of conventions, one door instead of a rule, a marker written last
instead of counting, a choke point instead of discipline at the call sites, a cheap probe
instead of an expensive one nobody runs.**

Make the check impossible to skip rather than easy to remember.

### Split requirements files

`requirements.txt` runs the whole app. `requirements-gpu.txt` is ~2.5 GB and turns on
semantic search. Two files rather than one, so a first install is a minute and not an
afternoon, and so the app has to work without the big one — which is what forces the
`semantic —` marker to exist.

Two pins that are not arbitrary: **pywebview>=6.2**, because 5.x deprecates
`FOLDER_DIALOG` for `FileDialog.FOLDER` and drops `webview.__version__`; and
**tgcrypto-pyrofork**, not `TgCrypto`, which has no cp312 Windows wheel and fails at
`Microsoft Visual C++ 14.0 or greater is required`.

### Generated files are gitignored

`web/dist/` and `web/src/api/schema.d.ts`. A generated file committed to a repository is a
file that will be stale, and a stale type definition is worse than none because it
type-checks.

The cost is that a fresh clone needs `npm run build`, which the README states as not
optional.

## The one-line version

Degrade, don't die. Say what you cannot do, by name. A thing that exists is not a thing
that is finished.

See [ARCHITECTURE.md](ARCHITECTURE.md) for what each module owns, [DATA.md](DATA.md) for
what each file costs to lose, and [OPERATIONS.md](OPERATIONS.md) for what to do about it.
