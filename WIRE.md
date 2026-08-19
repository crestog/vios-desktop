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
| `atlas/*.py` (15 files, ~11,900 lines) | `atlas/*.py` | `config.py` rewritten for one disk; `ingest.py` scan made incremental |
| `capture/*.py` (12 files, ~5,870 lines) | `vios/capture/*.py` | 3 import rewrites only (`vios.creds`→`creds`, `vios.tgcompat`→`tgcompat`, `vios.process.intake`→`.mtproto`) |
| `capture/mtproto.py` | `vios/process/intake.py` lines 278–473, class `Channel` | header rewritten, class verbatim |
| `sizing/registry.py` | `vios/process/registry.py` | unchanged |
| `sizing/resources.py` | `vios/process/resources.py` | `_system_ram_mb()` gained a Windows path; `total_ram_mb()` added |
| `sizing/base.py` | `vios/process/runners/base.py` | unchanged |
| `sizing/__init__.py` | `vios/process/__init__.py` (`SCHEMA_VERSION`, `CHANNELS` only) | reduced to the two constants |
| `tg_transport.py` | same | unchanged |
| `db_restore.py`, `db_export.py` | same | unchanged |
| `creds.py` | `vios/creds.py` | unchanged |
| `tgcompat.py` | `vios/tgcompat.py` | unchanged |
| `config.py` | *not copied* — rewritten from 412 lines to the 10 names the lifted code uses |
| `logger.py` | *not copied* — rewritten without Redis |

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
{"_": "vios-evidence-shard", "schema": 3, "session": "...", "at": 1786219385.7,
 "tables": {"claim": {"columns": {...}, "keys": [...]}, ...}}
{"t": "claim", "video_key": "...", "kind": "...", "value": "...", ...}
```

Three properties make this safe across two independently-evolving repositories,
and they are the reason separating them was cheap rather than reckless:

1. **Self-describing.** The header declares each table's columns, types and keys.
   The reader does not validate against a fixed schema — `_ensure_shard_table()`
   (`atlas/ingest.py:634`) takes the declaration from the shard and
   `ALTER TABLE … ADD COLUMN`s anything it has not seen. So *additive* drift, the
   likely kind, is absorbed with no coordination at all.
2. **Forward-tolerant to truncation.** `read_shard()` (`atlas/ingest.py:604`)
   validates only the magic string. A shard torn by a session that died
   mid-upload is explicitly not an error: every line before the tear is still
   good evidence.
3. **Loud about the dangerous direction.** A shard whose `schema` is *higher*
   than `sizing.SCHEMA_VERSION` must be reported, not skipped. The upstream store
   already refuses a newer database with *"Update the code — do not let an older
   binary write to a newer database"* (`vios/process/store.py:458`); this side
   raises the same alarm as a banner in the UI.

**Writer here:** `shardwriter.py`. **Reader here:** `atlas/ingest.py:import_shard`.
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
