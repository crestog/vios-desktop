# WIRE.md — the only contract this application shares with anything else

This repository and `crestog/VideoIntelligenceOS` (branch `atlas`) are two
separate programs on two separate machines. They share **no code and no runtime**.
They exchange exactly one thing: files appended to a Telegram channel.

That makes this file load-bearing. It is the whole cost of having separated the
repositories, and the only defence against them drifting apart silently.

**Update this file whenever shard or manifest handling changes on either side,
including the upstream SHA it was verified against.**

---

## Provenance

| | |
|---|---|
| Upstream repository | `crestog/VideoIntelligenceOS`, branch `atlas` |
| Upstream commit lifted from | **`c105313b82475db6cb00ee8ca4d9b5a41ae22684`** |
| Lifted on | 2026-08-19 |
| `SCHEMA_VERSION` at that commit | **3** (`vios/process/__init__.py`) |

### What was copied, and from where

| Here | Upstream | Modified? |
|---|---|---|
| `atlas/*.py` (15 files, ~11,900 lines) | `atlas/*.py` | `config.py` rewritten for one disk; `ingest.py` scan made incremental; `media.py` eviction replaced by a free-space floor; `reflect.py` excludes two derived tables from the text index |
| `capture/*.py` (12 files, ~5,870 lines) | `vios/capture/*.py` | 3 import rewrites only (`vios.creds`→`creds`, `vios.tgcompat`→`tgcompat`, `vios.process.intake`→`.mtproto`) |
| `capture/mtproto.py` | `vios/process/intake.py` lines 278–473, class `Channel` | header rewritten, class verbatim; trimmed at 209 lines (the extraction had over-run into `class Source:`); `SourceError` defined locally (upstream `intake.py:57`, outside the copied range); one function-local `from vios.tgcompat import patch` at `:59` rewritten — the copy script only rewrote top-level imports, and without it every MTProto call dies `Peer id invalid` |
| `sizing/registry.py` | `vios/process/registry.py` | catalogue list byte-identical; three functions added — `register()` (so a laptop-only pass joins the catalogue from outside the list, keeping the two copies comparable line for line), `missing_modules()` (thirty-one components declare a `requires` tuple *"importable module names, for preflight"* and nothing read it, so the Engine tab printed **ready** for `transcribe` on a machine with no `faster_whisper`), and `unrunnable()` now appends a missing-library reason **last**, after the hardware reasons — on a machine with no GPU, "no GPU in this session" is the useful fact and "install torch" would be advice that changes nothing |
| `sizing/resources.py` | `vios/process/resources.py` | `_system_ram_mb()` gained a Windows path; `total_ram_mb()` added |
| `sizing/base.py` | `vios/process/runners/base.py` | unchanged |
| `sizing/__init__.py` | `vios/process/__init__.py` (`SCHEMA_VERSION`, `CHANNELS` only) | reduced to the two constants |
| `tg_transport.py` | same | unchanged |
| `db_restore.py`, `db_export.py` | same | unchanged |
| `creds.py` | `vios/creds.py` | `export_to_env()` now also reads `~/.vios/credentials.json` — see below |
| `tgcompat.py` | `vios/tgcompat.py` | unchanged |
| `config.py` | *not copied* — rewritten from 412 lines to the 10 names the lifted code uses |
| `logger.py` | *not copied* — rewritten without Redis |

### New here, with no upstream counterpart

| Here | What it is |
|---|---|
| `paths.py` | The one place that decides where anything lives. Honours `VIOS_LOCAL_HOME`. `SHARD_DIR` (`<HOME>/shards`) is new: shards this machine wrote are kept rather than scratched, because they are the only replayable record of a local pass and `import_local_shard` defaults to `keep=True`. |
| `runners/` | The three modules that actually run a pass here — `ff.py`, `signal.py`, `structure.py` — plus `install()`, which registers **`shots-cpu`** into the copied catalogue at import. `shots-cpu` has no upstream counterpart: the processing plane detects shots on a GPU, and `ffmpeg/scdet` is what a laptop has. It is stage 0, and `cuts`, `motion`, `colour` and `loudness` read its shots. Three declared kinds are deliberately **withheld** rather than approximated — `camera_move` ("needs optical flow — mafd has no direction"), `stability` ("1−CV(mafd) inverts: it measures stillness, not shake") and `sharpness_mean` ("needs pixel gradients"). Each is reported in the run's `not_emitted` note, so a missing kind is a stated refusal and not a silent gap. |
| `studio.py` | The archive read as craft: how was this made, and what do the ones that work have in common. `deconstruct(key)` finds a reel's sections by change-point segmentation over its channel mix, `patterns(scope)` reports the distributions across many reels and ranks opening phrases by log-odds with an informative Dirichlet prior, `script_draft(scope)` turns both into a beat sheet where every number is a median of real reels and every line cites the reel and timecode it came from. **No model is called anywhere in it**, and nothing is stored — every answer is derived on read and cached against a fingerprint of the tables it read, so a scan or a re-index invalidates it and no answer can outlive its data. |
| `server/app.py` | **One** FastAPI app. Adopts `atlas.server`'s, `capture.routes`'s, `server.desktop_routes`'s and `server.admin_routes`'s finished `/api/*` route objects onto a single router (51 + 23 + 28 + 7 = 109), leaving both old frontends behind, and serves `web/dist` with an SPA fallback so `/watch/<key>?t=` is a real cold-loadable link. Replaces upstream `ui_server.py:940`'s `app.mount("/atlas", _atlas_server.app)`, which was a whole second FastAPI instance. |
| `server/desktop_routes.py` | The 28 routes with no upstream counterpart: mirror worker, local library, engine queue, derived artefacts (poster tiers, sprite sheet, keyframes), disk and host, and the three `/api/studio/*` that expose `studio.py`. |
| `server/admin_routes.py` | Credentials (`creds.save_local` / `forget_local` had no route anywhere — see defect 1 below), the schema-drift banner, and `db_restore` inspect/apply. **No export route, deliberately:** `db_export` pins its manifest to the channel, and on this machine `config.DB_PATH` is `<HOME>/lake/lake.db`, which nothing here reads — so an export from the laptop would publish an empty index over the pinned manifest the Kaggle side restores from. |
| `server/__main__.py` | `python -m server` — the API with no window, for `npm run dev` to proxy. |
| `desktop/__main__.py` | The window: credentials → uvicorn on a free port → `webview.create_window`. |
| `VIOS.bat`, `backup.bat` | Launch, and `git bundle` (there is no remote). |

### Three upstream defects fixed here, worth carrying back if that repo is ever touched

1. **`creds.export_to_env()` never read the local credential file.** `save_local()`
   has always written `~/.vios/credentials.json` and nothing ever exported it, so on
   a machine with no Kaggle Secrets the Admin form could store all four credentials
   correctly and the next launch would still say *"Telegram disabled"* — the exact
   failure that function's docstring was written to describe, one layer down. The
   file is now the lowest-priority source, matching `resolve()`'s existing
   FILE < ENV < SECRET ranking so the two cannot disagree.
2. **Reflection volunteered two derived tables as full-text search sources.** The
   first boot here logged `indexing 2 text source(s): map_point.source,
   scan_seen.verdict`. `map_point` is the UMAP projection and its `source` column is
   a channel label, not prose — up to 180k one-word passages, per `maps.py:114`.
   That one is upstream's and was invisible because it only appears once a map has
   been built and nothing read the log line that said so. Both are now in
   `reflect._ATLAS_OWN`.
3. **`store.py:export_shard` declares no schema in its header.** Its header is a set
   of id ranges — the shard's *provenance* — and carries no `tables` map, so a reader
   that wants to know what a column is has only the values in front of it. A column
   that is NULL in every row of a shard is therefore invisible: not declared, not
   created, and silently absent on the far side until some later shard happens to
   carry a value for it. `shardwriter._DECLARED_TYPES` is the fix on this side and it
   is nine lines; the reader half is `ingest.replay_shard`'s `declared` parameter.
   Both are described under the wire format below.

### What was deliberately **not** copied

`vios/process/` minus the three files above — `store.py`, `coverage.py`, `jobs.py`,
`engine.py`, `intake.py`, `runners/*` (17,688 lines). All of it exists to survive
Kaggle: ten notebooks on ten accounts sharing work without talking, each killed at
twelve hours. One laptop with one worker thread that nobody kills has none of those
problems, so copying them would mean importing a distributed system to run a
for-loop. `engine/queue.py` and `shardwriter.py` replace them in ~300 lines.

`ui_server.py`, `boot.py`, `v17_backend.py`, `admin_backend.py`, `omni_engine.py`
and every `*.html` — the frontend here is new, so those are reference material read
from the old folder, not files that moved.

---

## The wire format

### Evidence shards — `vios-evidence-*.jsonl.gz`

gzip, newline-delimited JSON. Line 1 is a header; every later line is a row.

```jsonc
// written here, by shardwriter.py
{"_": "vios-evidence-shard", "schema": 3, "session": "...", "at": 1786219385.7,
 "tables": {"claim": {"columns": {...}, "keys": [...]}, ...}}
{"t": "claim", "video_key": "...", "kind": "...", "value": "...", ...}
```

```jsonc
// written upstream, by store.py:export_shard — no `tables`, no `session`
{"_": "vios-evidence-shard", "schema": 3, "component": "transcribe",
 "lo_id": 0, "hi_id": 4210, "lo_vec": 0, "hi_vec": 0,
 "lo_fvec": 0, "hi_fvec": 0, "lo_fmet": 0, "hi_fmet": 0, "at": 1786219385.7}
```

Only the magic string is common to both, which is the one thing `read_shard`
requires. Everything else in the header is advisory and the reader must treat a
missing key as "not stated": **an upstream shard declares no columns at all**, so
for those the reader still has nothing but the values, and a column NULL in every
row of the shard is dropped exactly as described under property 1 below. It is
bounded rather than fatal, because upstream fills `t0`/`t1` on every claim and the
next shard that carries a value `ALTER TABLE … ADD COLUMN`s it — but it is the same
defect on the other side of the wire, and it is the first thing to carry back.

Three properties make this safe across two independently-evolving repositories,
and they are the reason separating them was cheap rather than reckless:

1. **Self-describing.** The header declares each table's columns, types and keys.
   The reader does not validate against a fixed schema — `_ensure_shard_table()`
   (`atlas/ingest.py:634`) takes the declaration from the shard and
   `ALTER TABLE … ADD COLUMN`s anything it has not seen. So *additive* drift, the
   likely kind, is absorbed with no coordination at all.

   **This was aspirational until it was measured.** Both sides used to infer the
   declaration from the values in front of them — the writer built the header from
   its rows (`shardwriter._build_table_meta`) and the reader parsed that header,
   discarded it, and re-inferred from the same rows a second time
   (`ingest.replay_shard`). A column that is NULL in every row of one shard was
   therefore declared by nobody and created by nobody. That is not an edge case,
   it is the *first* shard: a reel whose shot pass has not run emits whole-reel
   claims with `shot_idx`, `t0`, `t1`, `frame_idx` and `frame_hi` all empty, and
   all five were dropped — leaving a `claim` table with no time column and no link
   to `shot`, the one shape from which no moment can ever be placed on a timeline.
   The writer now states the types it knows from the schema it builds rows against
   (`shardwriter._DECLARED_TYPES`, restricted to columns the rows actually carry,
   so the header stays a true description of the payload), and the reader falls
   back to the declared type **only** when the values teach nothing. `_sql_type`
   is still right to refuse to default an empty column to TEXT — a `duration`
   typed TEXT on a null then stores a later shard's `30.0` as the string
   `"30.0"`, which does not compare and blanks the moment ribbon — but a *stated*
   type is neither a guess nor a default.
2. **Forward-tolerant to truncation.** `read_shard()` (`atlas/ingest.py:604`)
   validates only the magic string. A shard torn by a session that died
   mid-upload is explicitly not an error: every line before the tear is still
   good evidence.
3. **Loud about the dangerous direction.** A shard whose `schema` is *higher*
   than `sizing.SCHEMA_VERSION` must be reported, not skipped. The upstream store
   already refuses a newer database with *"Update the code — do not let an older
   binary write to a newer database"* (`vios/process/store.py:458`); this side
   raises the same alarm as a banner in the UI. **`GET /api/admin/wire`** is what
   feeds it: it compares `sizing.SCHEMA_VERSION` against the highest `schema`
   any imported bundle or shard was written with and returns
   `verdict: "ahead"` when the channel is newer than this reader. It also reports
   `wire_stale`, which catches the quieter failure — this file's recorded schema
   number no longer matching the constant in the tree, meaning the contract
   document was not updated with the code.

**Writer here:** `shardwriter.py`. **Reader here:** `atlas/ingest.py:import_shard`
for a shard off the channel, `import_local_shard` for one this machine wrote.
The local path exists because `import_shard`'s first act is
`tgchannel.fetch_document`, so before it the only way a locally-produced shard
could reach the reader was to upload it and download it back — on a laptop meant
to work with the channel unreachable, that is not a slow path but no path. A
locally-replayed shard is recorded in `bundles` with `seq` prefixed **`local:`**
and `manifest_id = NULL`, because there is no channel message behind it; that
`NULL` is what lets the Sources view separate evidence this machine produced from
evidence it received.
**Writer upstream:** `vios/process/store.py:export_shard` (`:1095`), header at
`:1143`. **Reader upstream:** `vios/process/intake.py:restore_shards` (`:679`).

### Database bundles — pinned manifest + parts

A full snapshot, split into parts, with a manifest pinned in the channel so the
newest bundle is discoverable in **one** API call (`atlas/tgchannel.py:121`
`pinned_message()`) rather than by scanning history. `db_restore.start_restore()`
has an `"inspect"` mode that reports what *would* change before anything is
overwritten; the Admin view here always runs `inspect` first and shows the diff.

`BUNDLE_SCHEMA` lives in `db_export.py` and is shared verbatim by both sides.

### Capture records — the load-bearing caption

Every uploaded reel's caption ends with `🔗 Link: {original_url}`. That is a
**format, not decoration**: it is the parse anchor `capture/seed.py` uses to
rebuild the ledger from the channel, which is what makes "never re-download after
months or years" survive the loss of any local database. Both sides must keep
writing it. `capture/upload.py:build_caption` is the writer;
`capture/seed.py:parse_caption` is the reader.

---

## What the evidence means once it lands

### The evidence schema has four live shapes

`ensure_schema` widens and never renames, and both sides have been writing for
longer than either has been reading, so `claim` exists in several shapes at once.
These four are real, and the times each one resolves to were **measured**, not
assumed:

| | `claim` columns | `time_columns` | borrows from `shot` |
|---|---|---|---|
| **fresh / shard replay** — all a new machine has | `uid, video_key, shot_idx, channel, kind, value, num, confidence, observer_id, ordinal, created_at` | `('', '')` | **yes** — this shape has no time of its own at all |
| **local** — what a `runners/` shard now declares | the above **plus `t0, t1, frame_idx, frame_hi`** | `('t0', 't1')` | **yes, per row** — `t0` is filled only for a frame claim |
| **production** — `vios/process/store.py:154` | same as local, plus `id INTEGER PRIMARY KEY` (dropped on export) | `('t0', 't1')` | yes, but nothing is left to borrow |
| **early fixture** — this laptop's `atlas.db`, widened by import | `uid, video_key, kind, name, confidence, t0, t1` + `channel, value, num, observer_id, ordinal, created_at` | `('t0', 't1')` | no — it has no `shot_idx` |

`shot` is `video_key, idx, t0, t1, score, detector, keyframe` everywhere except
the early fixture, which calls the boundary confidence `scene_score` and has
neither `detector` nor `keyframe`.

Three consequences, all of which have already cost real answers:

- **Probe the columns, never assume them.** `studio._columns` runs `PRAGMA
  table_info` per read and picks `value` or `name`, `score` or `scene_score` or
  `NULL`. It is deliberately uncached: the first shard to reach a table widens it
  mid-process, so a set memoised at import would be the pre-widening answer for
  the life of the app. And `studio._rows` swallows `sqlite3.Error`, logs at DEBUG
  and returns `[]` — so a wrong column name does not raise, it answers `ok: True`
  with zeros. There is no error to notice; the only symptom is an empty Studio
  over a table holding three shots and thirty-seven claims.
- **A column existing is not a row having a value in it.** Branching on the
  *schema* is right only while each shape means one thing, and the header repair
  ended that: `claim` now always has `t0`/`t1`, and a per-shot or whole-reel claim
  leaves both NULL. Two places read the schema and believed it — `reflect.time_link`
  returned early on *"the row already knows its own span"*, and `studio._claims`
  chose one of three queries off the column list — and both dropped the shot join
  for exactly the rows that needed it. Both now resolve per row with `COALESCE`.
  Anything new that reads these tables must do the same.
- **`artifact` carries `path`, and it is local-only in meaning.** A local artefact
  row records where the file is on this disk, which is harmlessly ignorable on the
  other side. It is not ignorable here: `paths` decides where anything lives, and
  an `artifact.path` written under a different `VIOS_LOCAL_HOME` will not resolve.

**The header's `keys` list is written and read by nobody.** `shardwriter` computes
it, and neither reader applies it: this side measures the real key from the values
(`ingest._dedup_columns`, which requires the key to contain an identifier so a
narrow sample's accidentally-unique `idx` cannot become an archive-wide index), and
upstream restores into its own fixed schema. Leave it that way. Honouring the
declared keys would hand `artifact` the key `["video_key"]` — every unique-index
insert after the first would then collapse a reel's five artefacts into one.

### How a moment gets a time

`moments.t_start` is what makes a search hit seekable and what the Studio timeline
is drawn from. There is no single column it comes from, and the resolution order
is a contract:

1. **The row's own start**, by name — `reflect.time_columns` matches a normalised
   name against `_START_NAMES` / `_END_NAMES`. `frame_t`/`frame_t1` are on those
   lists and matched **last**, so a table carrying both a real span and a frame
   stamp keeps the span: `t0` is what the row is about, `frame_t` is where it was
   sampled. Both writers currently rename `frame_t` to `t0` before storage
   (`runners/__init__.py:780`, upstream `store.py:832`), so no table declares that
   column today and the two names are defensive — one line in one writer is all
   that separates the raw name from reaching a table, and recognising it costs
   nothing. `frame_idx` and `frame_hi` are deliberately **not** on the lists: they
   are frame numbers, and a moment at t=142s because it was frame 142 is worse than
   one with no time at all.
2. **Otherwise the shot the row points at** — `reflect.time_link` LEFT JOINs
   `shot` on `(video_key, shot_idx)` and takes `COALESCE(t.t0, s.t0)`. The join is
   by that one exact name: a generic *"any `*_idx` points at a table"* rule would
   happily join `ordinal` or `frame_idx` and put moments at the wrong second. The
   end comes from the shot **only when the start did**, so a point claim with no
   end of its own is not stretched across the shot it happens to fall in. This is
   the step that carries the fresh shard-replay shape, which has no time column at
   all — 87 of 87 moments with a NULL `t_start` before it existed.
3. **Otherwise NULL, and NULL means *whole reel*** — never second zero. A caption
   describes the video, not its first frame. Everything downstream must keep the
   two apart: `studio._moments` carries a `timed` flag, `_timeline` lists only
   channels with a placed moment, `_channels.first_at`/`last_at` and
   `hook.silent_open` are nullable, and `patterns()` drops an untimed reel from
   the opening-rate denominator and reports it as `hook.untimed` instead. Reading
   NULL as `0.0` — which is what used to happen — fabricates a section on an
   all-zero occupancy matrix, asserts a reel opens silent when nothing in it was
   ever placed, and counts whole-reel caption text as hook language.

**The two writers disagree about step 3, and this side is the strict one.**
Upstream always fills `t0`/`t1`: from `frame_t` if the claim has one, else from the
shot, else from `whole` — the video's entire span (`store.py:831`). So a whole-reel
claim arrives from Kaggle at `0 .. duration`, indistinguishable from an observation
that genuinely covers the reel. `runners/__init__.py:780` writes only the frame
stamp and leaves both NULL, so locally the distinction survives into
`moments.t_start` and Studio can tell *"we know nothing about when"* from *"this
lasts the whole reel"*. Nothing may reintroduce the flattening on this side; when
imported evidence is being judged, remember its zeros may not be zeros.

Of two identical texts for one video and channel, the **placed** one wins.
`moments` is `UNIQUE(video_key, source, text_hash)` written with `INSERT OR
IGNORE`, so the first insert survives; `index.build_passages` therefore emits
timed passages before untimed and drops an untimed string a placed passage already
carries. This is load-bearing because a pass legitimately emits the same value
twice — `runners/signal.py:296` claims `motion_energy` for the whole reel and
`:306` claims it again per shot, both reading `gentle` — and the timeline used to
get the copy that could not seek.

**`reflect._RULES_VERSION` is part of the index fingerprint.** Hashing only the
schema is right while fixed rules are read against moving tables and backwards the
moment the rules move: every fix above would have shipped inert on exactly the
machines that had the defect, their `moments` table already built and their schema
unchanged. Bump it when anything that turns the schema into moments changes, and
one rebuild happens on next boot without anyone knowing they had to ask.

It is also the **only** channel a new *graph* rule has. `atlas/graph.py` keeps no
fingerprint and no dirty counter; it is rebuilt beside the index, so the index's
staleness test is the graph's too. `eav_pair` was added for the graph alone and the
version was bumped for the graph alone, and that is what made an existing archive
pick it up — measured: the fingerprint moved, the index rebuilt 87 passages, the
graph went from 3 nodes to 10, and Atlas was ready in half a second with nobody
pressing anything. A graph rule added without the bump reaches new archives only,
which is the subset least likely to notice it is missing.

**A known rough edge, left alone deliberately.** `build_passages` merges short
adjacent rows so a transcript reads as sentences, and a channel whose claim values
are single banded words gets merged too — three consecutive `still` readings
become the passage `still still still`. It is not wrong, exactly: those really are
three observations. But it is not a sentence either, and it makes such a passage
match a search for `still` three times over. Fixing it means deciding per channel
whether a value is prose or an enum, which is a tuning question and not a defect,
so it is written down rather than guessed at.

### Seven job states, five of them terminal

`local_jobs.state` is `pending`, `running`, `completed`, `skipped`, `deferred`,
`failed` or `unrunnable`. **Four of the five terminal states are not errors**, and
treating them as two — done or broken — is how a re-queue used to walk straight
past the work it was asked to redo. `skipped` is a pass that correctly declined
(no audio track, no shots yet), `deferred` is one waiting on a dependency,
`unrunnable` is one this hardware or this install cannot host at all
(`registry.unrunnable`, which now includes a missing library), and `failed` is the
only one that means something went wrong. A run that ends
`completed=27, skipped=2, failed=1, unrunnable=2` is a healthy run.

The table name is local-only: this side keeps its queue in `paths.JOBS_DB`, never in
`atlas.db`, so a job record cannot be mistaken for evidence and no shard carries
one. What a pass *learned* travels; what a machine *did* does not. The one exception
is upstream's `coverage` table, which travels precisely so a database rebuilt from
shards does not re-attempt three hours of work whose evidence it already holds
(`store.py:_settled_coverage`).

---

## Five local invariants that are easy to break and expensive to notice

### The scan cursor may only advance across ground it actually covered

`ingest.scan_and_import()` walks the channel newest-id first. Upstream wrote
`last_scan_head = head` unconditionally and **never read it back** — `floor` was
hardcoded to `1` (`ingest.py:1086`), so every relaunch re-fetched metadata for
thousands of already-classified messages. Reading it is the fix, and it introduces
a hazard that did not exist while the value was write-only:

`max_messages` caps a walk (`floor = head - max_messages + 1`). If that cap lifts
the floor above `cursor + 1`, the walk **skipped** everything between, and writing
the new head would claim coverage of a range nothing ever looked at. That range
would then be invisible forever — a silent, unrecoverable hole in the archive. So
`scan_floor()` returns an explicit `advance` flag, and the cursor moves only when
the walk joined up with the previous one. When it does not, the log says so.

The `scan_seen` table records the verdict per message id (`shard`, `bundle`,
`asset`, `no-document`, `uninteresting`, `absent`) so plain uploads are not
re-examined either. **Failures never settle** — a manifest that would not parse is
usually a torn download, and the next walk is exactly the retry it needs.

### Nothing on local disk is evicted

Upstream `atlas/media.py` ran an LRU cache bounded at `VIDEO_CACHE_GB` (12 GB),
which is right on Kaggle: the scratch disk is wiped between sessions, so nothing
was precious and everything could be re-fetched from the channel. On a laptop that
keeps its disk a quota guarantees the opposite of what it looks like it buys — the
archive is never actually local, so every session pays Telegram's rate limits over
again, which is the cost the mirror exists to remove.

So `VIDEO_CACHE_GB` is gone and `_check_floor()` replaces `_maybe_evict()`. It
measures free space against `paths.FREE_FLOOR_GB`, logs once per transition, and
**deletes nothing**. A background worker silently dropping videos to make room for
more of the same videos turns "my archive is safe" into "which ones did it drop?",
and that question has no answer worth the gigabytes. Reclaiming space is a user
action in Admin.

### The shared connection hands out tuples, and a named-column read must say so

`ingest.connect()` deliberately leaves `row_factory` unset, and `server.db()` hands
that one connection per thread to every reader in the application. Most of them are
written for tuples — `reflect.columns` builds its dicts from `r[1]`/`r[2]`,
`studio._rows` returns rows raw — so the factory cannot be flipped on the
connection without changing the shape of rows under readers that never asked.

A module that wants `r["video_key"]` therefore sets the factory **on its cursor**.
`vsearch.frame_rows` hit this first and settled it that way; `atlas/maps.py` had it
wrong in eight of its nine readers, every one of which raised `TypeError: tuple
indices must be integers` against any database with rows in it — the whole Data
tab. It read as working because a map needs the encoder, the encoder needs torch,
and the laptop has neither, so `built()` returned False and the readers returned
early before touching a row.

That is the shape of the hazard worth remembering: **a guard that is False for an
unrelated reason hides a broken reader indefinitely.** The one function an empty
database did catch was `axes`, and only because `COUNT`/`MIN`/`MAX` return a row
whether or not the table has any — an aggregate has no empty case to return early
on. Two builders in this tree open their own connection and set the factory there
(`maps._build`, line 455), which is fine and is also why the bug survived review:
the file's own top half is written in the style its bottom half could not use.

### A derived layer is only as fresh as whoever remembered to rebuild it

`moments` and `graph_nodes` are both projections of the same claims, so they go
stale at the same instant — but the index has a fingerprint and a dirty counter and
the graph has neither. Boot rebuilds the pair together (`server._index_if_stale`).
Everything else that changes claims must say so explicitly, and for a while nothing
did: a first local sweep left the Graph tab reading *no relationships found in this
database* over a library the engine had just finished processing. Measured: 88
claims indexed, graph 0 nodes, then 35 nodes and 63 edges from one manual rebuild
with nothing else changed.

There are now three doors and they are the complete set — boot, `/api/reindex`, and
`engine_queue._regraph` after the sweep's idle reindex. The engine's copy is **idle
only**, unlike its mid-sweep text rebuild: this is derivation from rows already
written, and doing it every `INDEX_EVERY` rows would recompute the whole graph for
a result nobody can see until the sweep ends. All three are non-fatal, for boot's
reason — an archive with a stale graph is still searchable, still playable, still
every other tab.

`server._rebuild_graph` says nothing about the boot phase, and that silence is
load-bearing. `_BOOT` has no path back to `ready` once something sets it to
`indexing`, so a post-boot caller announcing through there would leave
`/api/status` reporting the app as mid-boot for the life of the process, with the
boot banner drawn over a database that finished indexing.

### A graph that only reads list columns is empty on a laptop

`graph.rebuild` derives from lists, foreign keys, text columns and hashtags, and
none of those four sees the table this application writes more of than any other.
`claim` holds one assertion per row — `kind='rhythm', value='metronomic'` — so
`_is_list_column` looks at `claim.value`, sees one short item, and correctly says
no. On Kaggle that costs little, because captions and creators and transcripts
supply relationships of their own. On a laptop with none of those it cost
everything: Graph was empty, and Roadmap, which builds a plan out of graph nodes,
offered zero steps. Two of ten tabs, structurally blank on exactly the library this
half of the application exists for.

`reflect.eav_pair` names the `(attribute, value)` pair, `graph._mine_attrs` reuses
every filter the tag pass already had, and *which reels were given the same value
of the same property* turns out to be the only relationship a local archive has
before anybody writes a word about it. Three reels, 88 claim rows: 37 distinct
pairs in, 7 nodes out, and Roadmap from 0 steps to 7.

Two things about it generalise. **The pair is named, not inferred** — the shape
tests were tried first and both are traps: low cardinality needs a threshold to
tune, and "the value space partitions by attribute" holds on a small archive and
stops holding the moment two properties share a word, since `wide` is a dynamic
range and an aspect ratio. A rule that silently switches itself off as data grows
is worse than a list of names, and column *roles* are where `reflect.py` already
uses names (`_KEY_NAMES`, `_ROW_SOURCE_COLUMNS`); the argument against name lists
belongs to token filtering, where the noise class is unbounded. **Colour is read
per row, not per column** — `source_label('claim','value')` can only answer *meta*,
which would paint a transcript and a loudness reading the same shade, so the node
takes `claim.channel`, the row's own declaration, when the group is unanimous. On
the test library that is 4 style and 3 audio where the flat answer was 7 meta.

---

## Transport facts that bite

* **The Bot API caps `getFile` at 20 MB.** Over that, downloads must go via
  MTProto. `atlas/tgchannel.py:132` `http_download()` treats a `getFile` failure
  as *"use MTProto next"* rather than as an error — a comment there records the
  run that retried 60 times instead.
* **A bot cannot list channel history.** `atlas/ingest.py:1075` refuses a full
  scan without MTProto: *"bots cannot list history"*. So pyrogram plus a session
  file is mandatory for history, not optional.
* **Pyrogram cannot name a modern channel id** without `tgcompat.patch()`.
  Channel ids past 2³¹ fall below pyrogram's hardcoded `MIN_CHANNEL_ID` and every
  call dies with `ValueError: Peer id invalid: -100…` before a byte leaves the
  machine. `tgcompat.py` widens that one floor to 2⁴⁰ and **must not** touch
  `MIN_CHAT_ID` — widening both turns a loud `ValueError` into a confident, wrong
  `"chat"`. The file documents the measured session.
* **`FloodWait` is a pause, not a failure.** `atlas/tgchannel.py:320`'s `_guard`
  sleeps and retries.

---

## Standing rules that survive the copy

* **Never a credential literal in any file.** Env-only, no defaults. An earlier
  revision of the upstream harvester carried a live bot token as a default and
  published it to a public repository. This repo being private is a second layer,
  not a replacement. If a secret ever lands in a commit, rotating it is the fix.
* **Never set `PYTORCH_CUDA_ALLOC_CONF`.** `expandable_segments:True` unmaps pages
  that bitsandbytes still holds raw device pointers into; the result is a sticky
  `illegal memory access` that surfaces in a later, unrelated pass. `config.py`
  actively unsets it.
* **The upstream repository is read-only for this work.** No commits to
  `crestog/VideoIntelligenceOS`, no touching branch `atlas`. The Kaggle launch cell
  clones `atlas` and must keep finding it exactly as it is.
