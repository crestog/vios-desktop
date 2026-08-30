# Data

Where everything is, what it costs to lose, and what rebuilds itself.

## One folder

**`%USERPROFILE%\VIOS-Data`** — on the machine this was built for, that is
`C:\Users\devansh\VIOS-Data`. Override with `VIOS_LOCAL_HOME` and restart.

This was a requirement, not a convention:

> *"if i ever need to free up space i can simply clear one folder where entire data is
> stored and visible to me in my file manager and i will delete everything from there,
> once i again need to use vios it can again rebuild and download eveything from
> telegram"*

Two consequences that shape the whole design:

1. **It is visible.** Under your user profile, not in `%LOCALAPPDATA%`. A folder you
   are expected to delete has to be a folder you can find. There is a
   `READ ME - deleting this folder is safe.txt` inside it saying so.
2. **Deleting it is a supported operation**, not a recovery procedure. Every code path
   that writes there assumes the folder may be absent on next launch.

There is exactly one home. An earlier version had two — a visible one and a hidden one
— and the app read one while the media lived in the other. Six reels reported *"not
fully derived"* truthfully, about the wrong folder. The hidden home no longer exists.

## Layout

```
VIOS-Data/
  READ ME - deleting this folder is safe.txt
  atlas.db  (+ -wal, -shm)   the archive's knowledge
  capture_ledger.db          what has ever been captured and uploaded
  mirror.db                  proof of byte-complete download
  library.db                 watched local folders
  jobs.db                    the local processing queue
  moments.vec, .vec.json     the dense retrieval matrix and its build stamp
  media/
    video/                   originals, <key>.mp4
    proxy/                   re-encodes for playback, <key>.mp4
    sprite/                  scrub-bar preview sheets + timestamp maps
    poster/                  three still frames per reel
    frames/<key>/            keyframe images + index.json written LAST
  frames/                    frame embedding matrices (frames-clip.vec, frames-siglip2.vec)
  bundles/                   downloaded database bundles
  shards/                    evidence shards this machine wrote — the only copy
  lake/lake.db               restore target for the Kaggle-side harvest index
  models/                    locally downloaded weights
  session/                   Telegram session files
  logs/                      rotating log
  scratch/                   anything may delete this
  quarantine/                set aside, never deleted by the app — safe for you to delete
```

## The databases, and what losing each one costs

| file | holds | if destroyed |
|---|---|---|
| `atlas.db` | every transcript line, moment, claim, concept, graph edge | **rebuild from the channel. Nothing is lost.** |
| `capture_ledger.db` | the permanent record of what has ever been captured and uploaded | **serious.** Rebuildable by re-scanning the channel, slowly |
| `mirror.db` | proof that files on disk are byte-complete | annoying — every file gets re-weighed |
| `library.db` | folders on your PC you asked it to watch | re-add them |
| `jobs.db` | the local processing queue | pending work lost, completed work not |
| `lake/lake.db` | the Kaggle-side harvest index, when restored | empty on a fresh install; a restore target |

### `atlas.db` is disposable, and that is a compliment

Everything in it is either a copy of something in the channel or computed from
something in the channel. That single property is what buys the app its whole recovery
strategy.

It means corruption can be handled aggressively. On boot, `dbhealth.probe()` runs
`select count(*) from sqlite_master` against each database. If one will not open, it is
**quarantined, not deleted** — moved into `quarantine/` with a timestamp and a
`.why.txt` note — and the app starts with a fresh empty one and pulls the pinned bundle
back from the channel.

Compare the alternative: an app that refuses to start because a rebuildable cache is
damaged.

**Why that probe and not `PRAGMA integrity_check`.** SQLite parses the schema once per
connection and caches it, so a process that has been running for an hour with a corrupt
file will not notice — its copy was parsed successfully when it connected. Counting
`sqlite_master` forces a fresh parse, which makes the probe and the symptom the same
event rather than a proxy for it. `integrity_check` is slow on a large file and, on the
real corrupt file measured on 30 August, failed on `moments_fts` before reaching the
actual damage. A boot check that cannot finish gets removed.

**Three rules when quarantining, each of which was a way to make it worse:**

1. **Quarantine, never delete.** A future SQLite build or `sqlite3 .recover` may read
   what this one cannot, and the file is the only copy of whatever the last pass
   computed. Disk is not the constraint; an unrecoverable mistake is.
2. **The journals go with it, database first.** A `-wal` beside a *new empty* database
   is a second corruption, freshly manufactured — SQLite would replay committed frames
   belonging to pages that no longer mean what they meant. Dying between the two steps
   the right way round leaves a log with no database, which SQLite ignores. The wrong
   way round leaves a database that reads as complete while silently missing every
   committed change the log held.
3. **The derived sidecars go too.** `moments.vec` is a flat matrix whose row order is
   defined by rows in `atlas.db`. Keeping it beside a rebuilt database means search
   answers with vectors belonging to moments that no longer exist — wrong answers,
   silently, which is worse than no answers.

### `capture_ledger.db` is the precious one

For every reel: its Instagram shortcode, which Telegram message it was uploaded as, how
many bytes, how long, when captured. Currently 30 reels and 45 collection memberships.

Two things depend on it and both are load-bearing:

1. **It is the mirror's target list.** Not the search index — the ledger. The search
   index only contains reels Kaggle has *finished processing*; a reel sitting in the
   channel unprocessed is not in it. Building the target list from the union of both is
   what makes "download the entire channel" true rather than "download the processed
   part".
2. **It is the declared byte count** the mirror compares a landed file against.

`capture/seed.py` can rebuild it by walking the channel, but that is a slow walk over
every message. Treat it as the file you would rather not lose.

### `mirror.db` is deliberately its own file

It holds one kind of fact: *this file, this many bytes, checked against Telegram at
this time, and it matched.*

It could be a table inside `atlas.db`. It is not, for a specific reason: `atlas.db` is
the one file the app is willing to throw away. If the proof of download lived inside
it, throwing it away would also throw away the proof — and the only way to rebuild
proof is to download everything again. **A corrupted search index would cost a
re-download of the entire archive.** One file, one job, outside the disposable one.

## Completion markers

The rule that recurs everywhere in this project:

> **A thing that exists is not a thing that is finished, and the only reliable way to
> know the difference is to write down a marker that can only exist afterwards.**

Three instances:

- **Downloads.** Telegram declares a file's exact byte count. A download is accepted
  only when the landed file matches, and the match is recorded in `mirror.db` so a
  proven file is never weighed again. A mismatch is renamed to end in `.short` — left
  visible on purpose — and retried with backoff from 1 minute to 1 hour.
- **Keyframes.** `media/frames/<key>/index.json` is written **last**, after every
  image. Its presence is the proof that extraction finished. Without it, a folder with
  three of two hundred frames because ffmpeg was killed halfway looks identical to a
  finished one.
- **Vector files.** `moments.vec.json` carries the build id of the index the matrix was
  computed against. A reindex reassigns moment row ids, so a stale matrix is detected
  rather than silently mis-ranking. Frame vectors are keyed by `(video_key, frame_idx)`
  — which no rebuild touches — so those survive any number of index rebuilds and can be
  built incrementally as shards land.

## What playback costs on disk

For 30 reels: **182 MB of originals became 244 MB of proxies plus about 47 MB of
sprites, posters and frames.** The preparation is larger than the thing being prepared.

That is deliberate and stated as such: disk is unconstrained, the machine has 186 GB
free, and spending 1.5× the archive size to make every interaction instant is obviously
worth it. It stops being worth it only at a size that is a different problem for a
different day.

There is a floor. The mirror stops before it fills the drive: the status bar's
free-space segment turns amber and the log says *"disk below the 12.0 GB floor — not
downloading"*. Default 12 GB, `VIOS_FREE_FLOOR_GB` to change it. **It warns instead of
evicting** — nothing is ever deleted to make room. Filling the system drive is not a
recoverable state for Windows, so the app will not participate in it.

## Rebuilding from nothing

Delete `VIOS-Data` entirely, relaunch, and:

1. `paths.py` recreates the folder tree and the READ ME.
2. `dbhealth.boot()` finds no databases and creates empty ones.
3. `atlas/server.py:_boot()` reads the channel's pinned message, which points at the
   newest database bundle (currently `20260810-211200`).
4. `db_restore.py` fetches it, and the archive's knowledge is back.
5. `mirror.py` builds its target list from the restored ledger and starts pulling
   originals, most-documented first.
6. `derive.py` produces the four playback artefacts as each original lands.

**What is genuinely lost:** anything captured but not yet uploaded to the channel, and
any evidence shard in `shards/` that was written locally and never published. Those are
the only two things in the folder that are not a copy of something the channel holds.

## Restoring on purpose

Admin → Restore. Two steps, deliberately separate:

1. **Inspect** — download the bundle and report what is inside, touching nothing.
2. **Apply** — unpack it into place.

A restore overwrites your database. You should be able to look before you leap.

Bundles are zstd-compressed, which is why `zstandard` is a hard requirement rather than
optional: `atlas/ingest.py` can fall back to a `zstd` binary, which Kaggle's image
ships and Windows does not, and `db_restore.py` has no fallback at all.

## Credentials are not in this folder

`~/.vios/credentials.json`, mode 0600 — outside both the data folder and the
repository. The repository is public; a credential file one `git add -A` away from a
public repo is a credential that will eventually be committed.

Environment variables override the file. No module has a fallback literal for any
credential. See [OPERATIONS.md](OPERATIONS.md).
