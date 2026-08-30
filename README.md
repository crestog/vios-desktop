# VIOS Desktop

A native Windows application for searching, watching and understanding an archive
of short-form video — locally, offline, and permanently.

It is the reading half of a two-part system. The other half runs on Kaggle and does
the GPU work. **They never talk to each other.** A Telegram channel sits between them
as the only shared surface, and this laptop's job is to bring that channel home once
and then never need the network again.

```
Kaggle  ──uploads──▶  Telegram channel  ──downloads──▶  this laptop
(GPUs, transient)     (the archive, permanent)          (yours, forever)
```

Current state on the machine it was built for: **30 reels, 30 byte-verified against
Telegram, 110 moments, 872 claims, 100% mirrored, 0 outstanding.**

---

## What it does

- **Search moments, not videos.** Hybrid retrieval — BM25 for names and jargon,
  dense vectors for paraphrase — fused by reciprocal rank. Results are timestamped
  moments grouped under the reel they came from.
- **Instant playback.** Every reel is pre-derived into a proxy, a sprite sheet,
  posters and keyframes before you ever click it. Nothing is computed on demand.
- **A graph read out of the schema.** Concepts, people, techniques and objects, with
  every edge carrying the row that justifies it.
- **A curriculum and a craft view.** The same tables ordered as a learning path, and
  read as measurable structure — pacing, hooks, cuts.
- **The raw database, browsable.** Every number in the interface traces to rows you
  can see.
- **Capture.** Feed it permalinks or an Instagram export; it fetches, gathers
  evidence and uploads to the channel, paced to keep the account alive.
- **Local processing.** Everything ffmpeg can measure runs here for free. Models run
  on Kaggle.

## Requirements

- Windows 11 (WebView2 ships with it)
- Python 3.12
- ffmpeg on `PATH`
- Node 20+ — only to build the interface once
- A Telegram bot, a channel, and API credentials from `my.telegram.org`

## Install

```bash
pip install -r requirements.txt
```

```bash
cd web && npm install && npm run build
```

That is the whole install and it takes about a minute. The compiled interface is not
committed, so the build step is not optional.

**Optional, and 2.5 GB:**

```bash
pip install -r requirements-gpu.txt --index-url https://download.pytorch.org/whl/cu124
```

Without it the app runs completely, and semantic search is off — searches are
keyword-only, and the interface says so in two places rather than pretending
otherwise.

## Run

```bash
python -m desktop
```

Or double-click `VIOS.bat`. For a desktop icon:

```bash
python -m desktop.make_shortcut
```

The window opens once the server answers on `127.0.0.1:7000`. It waits on purpose —
WebView2 renders an error page for a refused connection and does not replace it when
the port comes up a moment later.

**There is no authentication, and there must not be a reason to add any.** The server
binds to the loopback interface only. Do not forward the port, do not bind it to
`0.0.0.0`, and do not put it behind a tunnel: it is a single-user local application
with full read access to your archive and your credentials panel.

## Credentials

Enter them once in **Admin → Credentials**. They are stored in
`~/.vios/credentials.json` at mode 0600 — deliberately outside this repository,
because a credential file one `git add -A` away from a public repo is a credential
that will eventually be committed.

Environment variables override the file, and are the only mechanism used on Kaggle:

| variable | what it is |
|---|---|
| `VIOS_BOT_TOKEN` | bot token from @BotFather |
| `VIOS_CHANNEL_ID` | channel id, the `-100…` form |
| `VIOS_API_ID` | from `my.telegram.org` |
| `VIOS_API_HASH` | from `my.telegram.org` |
| `VIOS_HF_TOKEN` | Hugging Face, optional |
| `VIOS_IG_COOKIES` | Instagram cookie jar, Netscape format, optional |

**No module in this repository has a fallback literal for any credential.** A default
value once put a live bot token in a public repository; the rule now is env-or-file
with no defaults, and there is nothing to leak by reading the source.

Log lines name credentials and never contain them. That is enforced at the single
function every log line passes through, not at the call sites — because HTTP client
libraries put the full request URL into their own exception messages, and redacting
per-caller makes every future log line a leak vector.

## Where your data lives

**One folder: `%USERPROFILE%\VIOS-Data`.** Visible in your file manager, deletable
from your file manager. Override with `VIOS_LOCAL_HOME`.

```
VIOS-Data/
  READ ME - deleting this folder is safe.txt
  atlas.db            the archive's knowledge — disposable, rebuilt from the channel
  capture_ledger.db   what has ever been captured and uploaded — the precious one
  mirror.db           proof that files on disk are byte-complete
  library.db          folders on your PC you asked it to watch
  jobs.db             the local processing queue
  media/              video, proxy, sprite, poster, frames
  frames/             frame embedding matrices
  bundles/            downloaded database bundles
  shards/             evidence shards this machine wrote — the only copy
  lake/               restore target for the Kaggle-side harvest index
  models/             locally downloaded model weights
  session/            Telegram session files
  logs/               rotating log
  scratch/            anything may delete this
  quarantine/         anything set aside — safe to delete
```

Delete the whole folder and the app rebuilds it from the channel on next launch. The
only thing that is genuinely lost is anything captured but not yet uploaded.

See [docs/DATA.md](docs/DATA.md) for what each file costs to lose.

## The ten screens

| route | screen |
|---|---|
| `/` | Home — what the system knows, and one box to ask it |
| `/search` | Search — three retrieval methods, faceted, returns moments |
| `/library` | Library — everything you have, virtualised grid, bulk actions |
| `/graph` | Graph — concepts and connections, clickable to the row behind each edge |
| `/roadmap` | Roadmap — the archive ordered as a curriculum |
| `/studio` | Studio — the archive read as craft |
| `/data` | Raw database — every table, every row, every query |
| `/capture` | Capture — the Instagram intake |
| `/engine` | Engine — local processing, the mirror, what the machine has |
| `/admin` | Admin — credentials, the wire contract, restore, disk, log |

Plus `/watch/<key>?t=14.32` — **the player is a route, not a modal**, so a link
shares the exact moment.

## The relationship to Kaggle

`WIRE.md` is the contract, and it is the most important document in this repository:
the two programs are separate codebases that never call each other, so no compiler
can check across the gap. The document *is* the interface.

Three kinds of message go into the channel:

- **Reels** — the video files, uploaded by this laptop's capture module
- **Evidence shards** — what the GPU passes produced, newline-delimited JSON
- **Database bundles** — a sealed, compressed snapshot, with the newest one pinned

This laptop reads all three and never fetches the same one twice. Restore is
inspect-then-apply, two separate steps, because a restore overwrites your database
and you should be able to look before you leap.

## Verifying a change

```bash
cd web && npm run audit
```

Two audits, and they are the reason this interface does not accumulate dead links:
every URL the client builds is checked against every route the server answers
(including parameter names), and every CSS class the components ask for is checked
against every class the stylesheet defines. Current state: **102 client URLs against
112 server routes, 0 broken.**

```bash
cd web && npm run build
```

Type-checks, then bundles. If the server is running, `npm run gen:api` regenerates
the typed schema from its live OpenAPI document.

## Troubleshooting

| symptom | what it means | what to do |
|---|---|---|
| `Peer id invalid` | pyrogram's channel-id range check, or an id passed as text | already fixed in `tgcompat.py` — if it reappears, something constructed a client without going through `tgcompat.client()` |
| Mirror frozen, status green | a dead socket that used to be permanent | fixed; the status bar now shows the dead state explicitly. Admin → Reconnect, or click Download now on any reel |
| "not fully derived" | the four playback artefacts are incomplete | Engine → the reel's row shows which pass is missing |
| Files ending `.short` | a download that did not match Telegram's declared byte count | left visible on purpose; it retries with backoff from 1 minute to 1 hour |
| `semantic —` in the status bar | torch is not installed | expected. Install `requirements-gpu.txt` or ignore it |
| Database will not open | physical page damage | boot quarantines it automatically and rebuilds from the channel. Check `quarantine/` for the note |
| Amber free-space segment | under the 12 GB floor | free space; the mirror will not fill your system drive |
| Blank window | server not up yet | should be impossible now — the window waits. Check `VIOS-Data/logs/` |

## Documentation

| document | what it covers |
|---|---|
| [WIRE.md](WIRE.md) | the channel contract — the only thing the two programs share |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | every module and what it owns |
| [docs/DATA.md](docs/DATA.md) | the folder, the five databases, what is rebuildable |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | first run, credentials, restore, what to do when X |
| [docs/DECISIONS.md](docs/DECISIONS.md) | the non-obvious choices, and what was measured |

And the reasoning lives at the top of each source file. Open any module: several
paragraphs on why it exists, what failed before it, and which alternatives were
rejected. That is where the real documentation is, and it is in the diff — so it
cannot drift far from the code it describes.
