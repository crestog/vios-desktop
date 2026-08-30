# Operations

What to do, in order, for each thing you will actually need to do.

## First run

1. `pip install -r requirements.txt`
2. `cd web && npm install && npm run build`
3. `python -m desktop` (or double-click `VIOS.bat`)
4. The window opens on Home. It will say the archive is empty. That is correct.
5. Go to **Admin → Credentials**. Enter the bot token, the channel id (`-100…` form),
   the API id and the API hash. Save.
6. Admin will now show the channel: title, member count, whether a bundle is pinned.
   If it does not, see *Telegram will not connect* below.
7. Go to **Admin → Restore**. Click **Inspect**. It reads the pinned message, finds the
   newest bundle, downloads it and reports what is inside — without touching anything.
8. If the contents look right, click **Apply**.
9. The mirror starts on its own. Watch it on **Engine**, or in the status bar.

Step 7 and step 8 are separate on purpose. A restore overwrites your database.

## A desktop icon

```bash
python -m desktop.make_shortcut
```

Writes a `.lnk` on your desktop pointing at `pythonw -m desktop` with the repo root as
the working directory, and draws the icon first if it is missing. `pythonw` rather than
`python` so there is no console window behind the app.

## Watching the import

**Status bar**, always visible: whether the server is answering, reel and claim counts,
whether semantic search is on, the mirror's progress, the engine's queue depth, free
disk, the GPU.

**Engine tab** for the detail: what is pending, running, completed, failed, which reel
and pass the worker is on, and the mirror panel with per-reel state.

The mirror announces completion once, in the log:

> archive complete — 30 reels local and derived, 30 byte-verified. Nothing further to
> download.

Then it drops from checking every 30 seconds to every 5 minutes. It does not stop — a
new reel could arrive in the channel tomorrow — it just stops being busy about it.

## Forcing one reel

Library or the player → **Download now**. It answers in words, not with a silent
acknowledgement:

- *queued — 3rd in line*
- *already downloading — 41%*
- *already here, and being prepared for playback*
- *this reel is in neither the search index nor the channel's own upload ledger, so
  there is nothing to download — it may not have finished uploading yet*

Clicking it also **clears that reel's retry timer**, so a reel that has been failing and
backing off jumps to the front instead of waiting out an hour. And if the Telegram
connection is dead, clicking it reconnects — you clicking a button is the clearest
possible signal that someone is present and wants something to happen now.

The worker checks the priority queue between **every** item, not between sweeps of the
whole list, so a click is honoured within seconds.

## Re-verifying what is on disk

`POST /api/mirror/verify` re-weighs everything against Telegram's declared byte counts,
ignoring the proof ledger. Use it after moving files by hand, or if you suspect
`mirror.db` is out of step with reality.

`GET /api/mirror/backlog` lists what is outstanding and why.

Both are also reachable from the Engine tab's mirror panel.

## Processing locally

Select reels in Library → **Process here**. It enqueues them one at a time, which looks
like a missed optimisation and is not: SQLite allows one writer, and a hundred
simultaneous writes against a single-writer database on a local socket produces
`database is locked` for no gain, because there was no network latency to hide.

The queue knows which passes each reel has already had, so enqueuing means enqueuing
only the **missing** ones. That is why the bulk action reports things like *"nothing to
do — 14 passes already ran"* rather than pretending to work.

**Ten of the catalogue's passes run on this machine** — everything ffmpeg can measure.
Every other pass shows *"no runner on this machine"* with the library it would need. That
is a different row from a pass that ran and failed, and the distinction is deliberate.

## Adding local videos

Library → add a watched folder. `library.py` scans it and the videos appear alongside the
archive. **Nothing is copied** — the files stay where they are, and the path is recorded.

The folder picker is a native Windows dialog, which is the decisive reason this is a
pywebview app and not a browser tab: no web page can open one.

## Capturing from Instagram

Capture tab. Three input kinds: permalinks pasted in, a saved collection, or an Instagram
data-export ZIP.

It runs for **days**, deliberately. `capture/pacing.py` is 264 lines whose whole job is
to go slower, and that is correct when the penalty for exceeding a rate limit is losing
the account. Leave it running; it survives restarts by reading the ledger.

Every capture writes to `capture_ledger.db` before and after upload, so an interrupted
run resumes rather than restarting.

## When something goes wrong

### Telegram will not connect

Check **Admin → Credentials** shows all four Telegram fields as present. The panel says
which are missing by name.

The bot must be an administrator of the channel, and the channel id must be the `-100…`
form. `tgcompat.check()` validates the id shape without importing pyrogram, so a bad id
is named before a run rather than three calls into one.

If Admin shows the channel but downloads fail, the MTProto session is the problem, not
the credentials — the Bot API and MTProto are two different paths. Admin → **Reconnect**.

### `Peer id invalid`

This is fixed, in four separate places, and the fix is `tgcompat.py`. If it reappears,
the cause is almost certainly that something constructed a pyrogram client without going
through `tgcompat.client()`:

```bash
git grep -n "from pyrogram import Client"
```

That grep is a complete audit. It should return nothing.

The four causes it covers: a channel-id range check with a constant that stopped being
true in 2024; a channel id arriving as a string, which pyrogram reads as a phone number;
a failed import latching a permanent wrong verdict; and pyrogram's import failing on
worker threads with an error that is not `ImportError`.

### The mirror looks frozen and the status is green

That was a real bug and it is fixed: the old code checked whether the client *object*
existed rather than whether its socket did, so the first dropped connection was permanent
for the life of the process. Measured: one aborted connection at 10:53:19 poisoned every
download until 11:49, retrying against a dead socket every five seconds and reporting
itself healthy the whole time.

Now a dead connection has its own symbol and its own words in the status bar, and it
**outranks everything else** the mirror segment could say — because the failure looked
exactly like an idle mirror.

If you see it: Admin → **Reconnect**, or click Download now on any reel. The status
endpoints report the session generation number, so you can see a rebuild happen.

### "This reel has not been fully derived yet"

One or more of the four playback artefacts is missing. Engine → the reel's row names
which. Usually it means `derive.py` has not run yet, which resolves itself.

If it persists for a reel whose original is on disk, the original may be short — look for
a file ending `.short` in `media/video/`, which is a download that did not match
Telegram's declared byte count. Those retry with backoff from 1 minute to 1 hour, and
Download now clears the timer.

### "Frames have not been extracted for this reel yet"

`media/frames/<key>/index.json` is missing, which is the completion marker written last.
Its absence means extraction did not finish, which is exactly what it is there to tell
you. Re-enqueue the `allframes` pass from Engine.

### A database will not open

Nothing to do. Boot handles it: `dbhealth.probe()` runs before anything else, and a file
that will not open is moved to `quarantine/` with a `.why.txt` note, and a fresh one is
created. The app then pulls the pinned bundle back from the channel.

**Check `quarantine/`** to read what happened. Nothing there is ever deleted by the app,
so it is safe for you to delete once you have read the note.

If you have a known-good copy of `atlas.db`, put it in place **before** starting the app.
Boot will find a healthy database and skip the rebuild entirely — which saves a channel
re-scan.

### Search finds nothing / `semantic —`

Expected without torch. Keyword search still works; paraphrase does not. Either install
`requirements-gpu.txt` or ignore it. The home screen states this in words rather than
silently returning worse results.

If keyword search also finds nothing, the index has not been built — Admin → reindex.

### Free space is amber

Under the 12 GB floor. The mirror has stopped downloading and **will not delete anything
to make room** — choosing what to throw away is not a decision the app makes silently
about your archive.

Free space yourself, or set `VIOS_FREE_FLOOR_GB` lower if you know what you are doing.
Or delete `quarantine/`, which is the safest thing in the folder.

### Blank window

The window waits for the server to answer before opening, so this should not happen. If
it does, the server did not start: read `VIOS-Data/logs/`. Run `python -m desktop` from a
terminal rather than the `.bat` to see the traceback directly.

## Reclaiming disk

Safe to delete, in increasing order of inconvenience:

1. **`quarantine/`** — nothing depends on it. Free.
2. **`scratch/`** — anything may delete this by design.
3. **`media/proxy/`** — playback falls back to the original and says so. Re-derived
   automatically.
4. **`atlas.db`** (+ its `-wal`, `-shm`, and `moments.vec*`) — rebuilt from the pinned
   bundle. All four together or none.
5. **The entire `VIOS-Data` folder** — supported, and the whole point of the design. See
   [DATA.md](DATA.md) for what is genuinely lost (anything captured but not yet
   uploaded, and locally-written shards in `shards/`).

## Making a change

```bash
cd web && npm run audit
```

Runs both audits. `api_audit.py` checks every URL the client builds against every route
the server answers, including whether parameters match and whether a required one is
declared optional. `css_audit.py` checks every class the components ask for against every
class the stylesheet defines — that one catches the failure that is worst to find by eye,
because a component styled with a class that does not exist renders as unstyled and looks
like a layout bug.

```bash
cd web && npm run build
```

Type-checks with `tsc --noEmit`, then bundles. Run it before committing; `web/dist/` is
gitignored, so a broken build is invisible until someone clones.

```bash
cd web && npm run gen:api
```

Regenerates `src/api/schema.d.ts` from the running server's OpenAPI document. Needs the
app running on port 7000. The file is gitignored — it is generated, and a generated file
in a repository is a file that will be stale.

## Security posture

- **No authentication.** Binds `127.0.0.1` only. Single user. Do not forward the port,
  do not bind `0.0.0.0`, do not put it behind a tunnel. The server has full read access
  to your archive and to the credentials panel.
- **No credential has a fallback literal anywhere in this repository.** A default value
  once put a live bot token in a public repo. Env-or-file, no defaults. `CHANNEL_ID` is
  the one exception and it is an address, not a key.
- **Log lines name credentials and never contain them**, enforced at the single function
  every line passes through — not at the call sites, because `requests` and `httpx` put
  the full request URL into their own exception messages, and per-caller redaction makes
  every future log line a leak vector.
- **Scan staged diffs before pushing.** This repository is public.
- **If a secret does land in a commit, rotating it is the fix**, not rewriting history.
