"""
mirror — bring the whole Telegram channel onto this disk, once, and prove it.

This module is what turns "reads from Telegram" into "everything is local". It
removes the network and Telegram's rate limits from the hot path entirely, and
it is the only thing standing between a fresh machine and a complete archive.

Three questions it exists to answer, each of which it previously got wrong:

  **"Is this reel downloaded?"**  The old answer was `os.path.getsize(p) > 0`.
  A connection dropped at 40% leaves a file that passes that test forever, so
  "30 videos downloaded" counted six files that were not whole. The new answer
  compares the bytes on disk against the byte count Telegram itself declared for
  that message, and remembers the comparison in `mirror.db` so it is made once
  per file rather than once per boot.

  **"Which reels are there to download?"**  The old answer read `video_index`,
  which only holds what Kaggle has finished processing and published as a
  bundle. A reel sitting in the channel that Kaggle had not got to yet was
  invisible to the mirror — and "Download now" on such a reel silently did
  nothing, because the key was not in the target list to be found. The new
  answer unions `video_index` with the capture ledger's uploaded rows, which is
  the channel's own record of every reel it holds, and carries that ledger's
  `file_size` along as the expected byte count the first question needs.

  **"Did the user's Download-now button do anything?"**  The old `prioritize()`
  returned `None`, the route returned `{"ok": true}` unconditionally, and the
  UI said "moved to the front of the download queue" whether or not anything had
  moved. Worse, the queue was only consulted *between* full sweeps, so a click
  during a sweep waited for every remaining reel first. It now reports what
  actually happened, and the worker checks the queue between items.

The shape of the work:
  1. Merge the two target lists (`_targets`), newest and most-cited first.
  2. Verify what is already on disk against its declared size (`_verified_src`).
  3. Download what is missing, to `.part`, and only rename after the byte count
     matches (`_download_video`).
  4. Derive proxy, sprite, posters and keyframes for whatever is whole.
  5. Back off per reel on failure, so one deleted message cannot spin the loop.
  6. When every reel is local and verified, say so and go quiet — the archive is
     finished, and a finished archive should not re-stat 5,000 files every 30 s.

Disk is never the constraint by design, but it is checked anyway
(`paths.below_floor()`), and no whole video is ever held in RAM: downloads go
straight to a `.part` file and are renamed into place.
"""

from __future__ import annotations

import collections
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

import derive
import paths
from atlas import config, tgchannel
from atlas.media import safe_name
from logger import vios_log as log

SUB = "MIRROR"

# Concurrency limits
_MAX_DOWNLOAD_SLOTS = int(os.environ.get("VIOS_MIRROR_DOWNLOAD_JOBS", "2"))
_DOWNLOAD_SEMAPHORE = threading.Semaphore(_MAX_DOWNLOAD_SLOTS)

# State & Stats
_LOCK = threading.RLock()
_RUNNING = False
_PAUSED = False
_STOP_EVENT = threading.Event()
_WAKE_EVENT = threading.Event()
_WORKER_THREAD: Optional[threading.Thread] = None

# In-flight tracking
_ACTIVE_DOWNLOADS: Dict[str, Dict[str, Any]] = {}
_ACTIVE_DERIVES: Dict[str, Dict[str, Any]] = {}
_PRIORITY_QUEUE: collections.deque = collections.deque()
_RECENT_ERRORS: collections.deque = collections.deque(maxlen=50)

# The merged target list, cached between cycles so `prioritize()` can answer a
# question about a key without opening two databases on the request thread.
_TARGETS: List[Dict[str, Any]] = []
_TARGET_AT = 0.0
_TARGET_TTL = 20.0

_STATS = {
    "total_videos": 0,
    "downloaded": 0,
    "verified": 0,
    "unverified": 0,
    "missing": 0,
    "derived": 0,
    "failing": 0,
    "bytes_downloaded": 0,
    "cycles": 0,
    "last_cycle_at": 0.0,
    "complete_at": 0.0,
    "last_error": "",
    "note": "",
}

# How long to wait after a failed download of one reel before trying it again.
# A message deleted from the channel fails identically to a network blip, and
# without a backoff the pair of them turn a 30 s sweep into a log-filling retry
# of something that will never succeed. Doubling, capped at an hour: a transient
# failure costs a minute, a permanent one costs one line per hour.
_BACKOFF_BASE = 60.0
_BACKOFF_CAP = 3600.0

# A reel whose declared size we cannot learn is still worth keeping, but it must
# not be *claimed* as verified. Anything smaller than this is not a video at
# all — it is an error page or a truncated first chunk — so it is rejected even
# with no expected size to compare against.
_MIN_PLAUSIBLE_BYTES = 16 * 1024


# ══════════════════════════════════════════════════════════════════════════
# THE PROOF LEDGER — mirror.db
# ══════════════════════════════════════════════════════════════════════════
# One row per reel, recording what Telegram said the file weighs and what landed
# on this disk. Its own database file, not a table in `atlas.db`, because
# `atlas.db` is the disposable one: `dbhealth` will quarantine and rebuild it
# from the channel, and the point of this ledger is to make that rebuild free of
# re-downloads. See the note on `paths.MIRROR_DB`.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS mirrored (
    video_key       TEXT PRIMARY KEY,
    msg_id          INTEGER,
    expected_bytes  INTEGER NOT NULL DEFAULT 0,
    got_bytes       INTEGER NOT NULL DEFAULT 0,
    verified_at     REAL    NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL    NOT NULL DEFAULT 0,
    last_error      TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS mirrored_due ON mirrored(next_attempt_at);
"""

_MDB = threading.local()


def _mdb() -> sqlite3.Connection:
    """This thread's connection to the proof ledger.

    Thread-local because the worker thread and every API request thread reach
    it, and a sqlite connection may not be shared across threads. WAL so a
    status read never blocks a download's write.
    """
    conn = getattr(_MDB, "conn", None)
    if conn is not None:
        return conn
    os.makedirs(os.path.dirname(paths.MIRROR_DB), exist_ok=True)
    conn = sqlite3.connect(paths.MIRROR_DB, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("pragma journal_mode=WAL")
        conn.execute("pragma synchronous=NORMAL")
    except sqlite3.Error:
        pass
    conn.executescript(_SCHEMA)
    conn.commit()
    _MDB.conn = conn
    return conn


def _record(key: str) -> Dict[str, Any]:
    """This reel's row, or an empty-but-shaped dict if it has never been seen."""
    try:
        row = _mdb().execute(
            "SELECT * FROM mirrored WHERE video_key=?", (str(key),)).fetchone()
    except sqlite3.Error:
        row = None
    if row is None:
        return {"video_key": str(key), "msg_id": None, "expected_bytes": 0,
                "got_bytes": 0, "verified_at": 0.0, "attempts": 0,
                "next_attempt_at": 0.0, "last_error": ""}
    return dict(row)


def _all_records() -> Dict[str, Dict[str, Any]]:
    """Every row, keyed. One query per cycle beats one query per reel."""
    try:
        return {r["video_key"]: dict(r)
                for r in _mdb().execute("SELECT * FROM mirrored")}
    except sqlite3.Error:
        return {}


def _mark_verified(key: str, msg_id: Optional[int], expected: int,
                   got: int) -> None:
    """Record that this file is whole. Clears any backoff it was serving."""
    try:
        _mdb().execute(
            "INSERT INTO mirrored (video_key, msg_id, expected_bytes, "
            "got_bytes, verified_at, attempts, next_attempt_at, last_error) "
            "VALUES (?,?,?,?,?,0,0,'') "
            "ON CONFLICT(video_key) DO UPDATE SET msg_id=excluded.msg_id, "
            "expected_bytes=excluded.expected_bytes, "
            "got_bytes=excluded.got_bytes, verified_at=excluded.verified_at, "
            "attempts=0, next_attempt_at=0, last_error=''",
            (str(key), int(msg_id or 0), int(expected or 0), int(got or 0),
             time.time()))
        _mdb().commit()
    except sqlite3.Error as exc:
        log(f"could not record {key} as verified — {exc}", SUB, "WARN")


def _mark_failed(key: str, msg_id: Optional[int], expected: int,
                 err: str) -> float:
    """Record a failed attempt and return when the next one may happen.

    The attempt count is what sets the delay, so a reel that has failed nine
    times is asked for once an hour while a reel that just blipped is retried a
    minute later. `verified_at` is cleared: a file that failed verification must
    not keep reading as proven from an earlier success.
    """
    rec = _record(key)
    attempts = int(rec.get("attempts") or 0) + 1
    delay = min(_BACKOFF_BASE * (2 ** (attempts - 1)), _BACKOFF_CAP)
    when = time.time() + delay
    try:
        _mdb().execute(
            "INSERT INTO mirrored (video_key, msg_id, expected_bytes, "
            "got_bytes, verified_at, attempts, next_attempt_at, last_error) "
            "VALUES (?,?,?,0,0,?,?,?) "
            "ON CONFLICT(video_key) DO UPDATE SET msg_id=excluded.msg_id, "
            "expected_bytes=excluded.expected_bytes, got_bytes=0, "
            "verified_at=0, attempts=excluded.attempts, "
            "next_attempt_at=excluded.next_attempt_at, "
            "last_error=excluded.last_error",
            (str(key), int(msg_id or 0), int(expected or 0), attempts, when,
             str(err)[:300]))
        _mdb().commit()
    except sqlite3.Error:
        pass
    return when


def _forget(key: str) -> None:
    """Drop a reel's row — used when its local file is deliberately discarded."""
    try:
        _mdb().execute("DELETE FROM mirrored WHERE video_key=?", (str(key),))
        _mdb().commit()
    except sqlite3.Error:
        pass


# ══════════════════════════════════════════════════════════════════════════
# WHAT THERE IS TO MIRROR
# ══════════════════════════════════════════════════════════════════════════
_LEDGER_DB = os.path.join(paths.HOME, "capture_ledger.db")


def _from_video_index() -> Dict[str, Dict[str, Any]]:
    """Reels Kaggle has finished processing and published as a bundle.

    Read read-only through a URI so a corrupt `atlas.db` cannot be made worse by
    this module, and so the mirror keeps working while `dbhealth` is deciding
    what to do about it. An unreadable reader database means the mirror falls
    back to the capture ledger alone — fewer facts per reel, same reels.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(paths.DB_PATH):
        return out
    conn = None
    try:
        conn = sqlite3.connect(f"file:{paths.DB_PATH}?mode=ro", uri=True,
                               timeout=config.SQLITE_TIMEOUT)
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
                "SELECT video_key, msg_id, local_path, duration, moment_count, "
                "created_at FROM video_index"):
            key = str(r["video_key"])
            out[key] = {
                "key": key,
                "msg_id": int(r["msg_id"]) if r["msg_id"] else None,
                "local_path": r["local_path"],
                "duration": float(r["duration"] or 0.0),
                "moment_count": int(r["moment_count"] or 0),
                "created_at": float(r["created_at"] or 0.0),
                "expected": 0,
                "indexed": True,
            }
    except sqlite3.Error as exc:
        log(f"could not read video_index — {str(exc)[:160]}", SUB, "WARN")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return out


def _from_capture_ledger() -> Dict[str, Dict[str, Any]]:
    """Every reel the channel holds, with the byte count Telegram gave it.

    This is the table the capture engine writes as it uploads, so a row with a
    `msg_id` is a reel that is *in the channel* whether or not Kaggle has
    processed it yet. That makes it the honest answer to "download the entire
    channel", and its `file_size` column is the number `_download_video` checks
    the landed file against.
    """
    out: Dict[str, Dict[str, Any]] = {}
    if not os.path.exists(_LEDGER_DB):
        return out
    conn = None
    try:
        conn = sqlite3.connect(f"file:{_LEDGER_DB}?mode=ro", uri=True,
                               timeout=30.0)
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
                "SELECT key, msg_id, file_size, duration, position, "
                "coalesce(done_at, added_at, 0) AS at FROM item "
                "WHERE msg_id IS NOT NULL AND msg_id > 0"):
            key = str(r["key"])
            out[key] = {
                "key": key,
                "msg_id": int(r["msg_id"]),
                "local_path": None,
                "duration": float(r["duration"] or 0.0),
                "moment_count": 0,
                "created_at": float(r["at"] or 0.0),
                "expected": int(r["file_size"] or 0),
                "indexed": False,
            }
    except sqlite3.Error as exc:
        log(f"could not read the capture ledger — {str(exc)[:160]}", SUB, "WARN")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
    return out


def _targets(force: bool = False) -> List[Dict[str, Any]]:
    """The merged, ordered list of every reel that should end up on this disk.

    Union, not either-or. `video_index` knows which reels are searchable and how
    much evidence each carries — that is the right order to mirror in, because
    the reel a user is most likely to open next is the one the archive has the
    most to say about. The capture ledger knows which reels *exist* and what they
    weigh. A reel in the ledger and not the index is one Kaggle has not reached
    yet; it still gets downloaded, just after the ones that are searchable.

    Cached for `_TARGET_TTL` seconds so `prioritize()` on a request thread can
    answer truthfully without two database opens per click.
    """
    global _TARGETS, _TARGET_AT
    with _LOCK:
        if not force and _TARGETS and (time.time() - _TARGET_AT) < _TARGET_TTL:
            return list(_TARGETS)

    merged = _from_capture_ledger()
    for key, row in _from_video_index().items():
        if key in merged:
            # The index wins on everything it knows and the ledger keeps the one
            # thing it alone knows: the declared byte count.
            row["expected"] = merged[key]["expected"]
            row["msg_id"] = row["msg_id"] or merged[key]["msg_id"]
        merged[key] = row

    items = sorted(merged.values(),
                   key=lambda d: (0 if d["indexed"] else 1,
                                  -d["moment_count"], -d["created_at"]))
    with _LOCK:
        _TARGETS = items
        _TARGET_AT = time.time()
    return list(items)


def _get_target_list() -> List[Dict[str, Any]]:
    """Kept as the old name because callers outside this module still use it."""
    return _targets()


# ══════════════════════════════════════════════════════════════════════════
# IS IT REALLY HERE?
# ══════════════════════════════════════════════════════════════════════════
def _dest_path(key: str) -> str:
    return os.path.join(paths.VIDEO_DIR, f"{safe_name(str(key))}.mp4")


def _local_candidates(key: str) -> List[str]:
    """Where a mirrored original for this key could be sitting.

    The legacy unsanitised name is still checked because reels downloaded before
    `safe_name` was applied are on real disks, and refusing to see them would
    re-download files that are already whole.
    """
    key = str(key)
    out = [_dest_path(key)]
    legacy = os.path.join(paths.VIDEO_DIR, f"{key}.mp4")
    if legacy != out[0]:
        out.append(legacy)
    return out


def _discard(path: str, why: str) -> None:
    """Get an unusable file out of the way, keeping it if that is cheap.

    A short file is renamed to `.short` rather than deleted on the first pass:
    if the byte comparison is ever wrong, a rename is recoverable and a delete
    is not. A second short download of the same reel overwrites the first, so
    this cannot accumulate more than one carcass per reel.
    """
    try:
        os.replace(path, path + ".short")
        log(f"{os.path.basename(path)} set aside — {why}", SUB, "WARN")
    except OSError:
        try:
            os.remove(path)
        except OSError:
            pass


def _verified_src(item: Dict[str, Any],
                  rec: Optional[Dict[str, Any]] = None) -> str:
    """The path to this reel's complete original, or "" if there isn't one.

    The old version of this function returned any file of non-zero length, which
    is why the app reported thirty downloads over twenty-four whole files. The
    order of the checks here is the whole fix:

      1. A file the user pointed us at (`local_path`, from their own library) is
         theirs. It is never measured against a Telegram byte count and never
         set aside — we did not download it and we do not get to judge it.
      2. A mirrored file whose recorded proof matches its current size on disk is
         trusted without re-measuring. This is the common case and it costs one
         `stat`.
      3. A mirrored file with a known expected size is measured. Equal means
         whole: record the proof and use it. Short means a download that died
         mid-flight: set it aside and report nothing local, so it is fetched
         again.
      4. A mirrored file with *no* known expected size is accepted if it is
         plausibly a video at all, and recorded with `expected_bytes = 0` — an
         honest "present but unproven", which `status()` reports separately so
         the number on screen never overstates what is known.
    """
    key = str(item["key"])
    local = item.get("local_path")
    if local and os.path.exists(local):
        try:
            if os.path.getsize(local) > 0:
                return local
        except OSError:
            pass

    rec = rec if rec is not None else _record(key)
    expected = int(item.get("expected") or rec.get("expected_bytes") or 0)

    for path in _local_candidates(key):
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size <= 0:
            _discard(path, "zero bytes")
            continue
        proven = int(rec.get("got_bytes") or 0)
        if proven and proven == size and float(rec.get("verified_at") or 0) > 0:
            return path
        if expected:
            if size == expected:
                _mark_verified(key, item.get("msg_id"), expected, size)
                return path
            _discard(path, f"{size} bytes on disk, Telegram declared {expected}")
            _mark_failed(key, item.get("msg_id"), expected,
                         f"incomplete file: {size} of {expected} bytes")
            continue
        if size >= _MIN_PLAUSIBLE_BYTES:
            _mark_verified(key, item.get("msg_id"), 0, size)
            return path
        _discard(path, f"only {size} bytes")
    return ""


def verify_now() -> Dict[str, Any]:
    """Re-measure every local original against its declared size.

    Runs once when the worker starts and is exposed to the Admin panel, because
    "it says thirty and I do not believe it" needs an answer a user can ask for
    and watch. Cheap: one `stat` per reel plus one query, no network.
    """
    items = _targets(force=True)
    recs = _all_records()
    whole = unproven = short = absent = 0
    for it in items:
        key = it["key"]
        before = _record(key) if key not in recs else recs[key]
        path = _verified_src(it, before)
        if not path:
            # Distinguish "was never here" from "was here and was not whole",
            # because the second is the one the user reported and it should be
            # visible in the log rather than only in a count.
            if any(os.path.exists(p + ".short") for p in _local_candidates(key)):
                short += 1
            else:
                absent += 1
            continue
        rec = _record(key)
        if int(rec.get("expected_bytes") or 0):
            whole += 1
        else:
            unproven += 1
    note = (f"{whole} original(s) proven complete, {unproven} present but "
            f"unproven, {short} incomplete and set aside, {absent} still to "
            f"download")
    log(note, SUB)
    with _LOCK:
        _STATS["note"] = note
    return {"ok": True, "total": len(items), "verified": whole,
            "unverified": unproven, "incomplete": short, "missing": absent,
            "note": note}


# ══════════════════════════════════════════════════════════════════════════
# FETCHING
# ══════════════════════════════════════════════════════════════════════════
def _note_error(key: str, err: str) -> None:
    with _LOCK:
        _RECENT_ERRORS.append({"key": key, "error": str(err)[:300],
                               "at": time.time()})
        _STATS["last_error"] = str(err)[:300]


def _download_video(item: Dict[str, Any]) -> bool:
    """Fetch one reel's original from the channel, and refuse a short file.

    The sequence matters and it is the answer to "it says downloaded and it is
    not":

      fetch the message → read the size Telegram declares for it → stream to
      `.part` → compare what landed against what was declared → only then rename.

    A rename is the only thing that makes a file visible to the rest of the app,
    so a transfer that dies at 40% leaves a `.part` nobody reads, and the next
    sweep starts it again. Before this, the same failure left a playable-looking
    truncated `.mp4` in place forever.
    """
    key = str(item["key"])
    msg_id = item.get("msg_id")
    if not msg_id:
        # A key that is neither in the ledger nor carries a msg_id in the index
        # cannot be fetched at all. Saying so once is better than a per-cycle
        # failure, so it earns a backoff row like any other failure.
        _mark_failed(key, None, 0, "no Telegram message id is known for this reel")
        return False

    if not config.telegram_ready():
        # No credentials means no channel, and the worker comes back for every
        # missing reel every cycle. Without this the log fills with the same
        # failure per reel per cycle and buries the useful half of this worker —
        # deriving proxies for the files that *are* here. The mirror still runs
        # on a machine with no Telegram; it just cannot fetch what is only in
        # the channel.
        return False

    if paths.below_floor():
        log(f"disk below the {paths.FREE_FLOOR_GB:.1f} GB floor — not "
            f"downloading {key}", SUB, "WARN")
        return False

    dest = _dest_path(key)
    tmp = dest + ".part"
    declared = int(item.get("expected") or 0)

    with _DOWNLOAD_SEMAPHORE:
        if _STOP_EVENT.is_set():
            return False
        with _LOCK:
            _ACTIVE_DOWNLOADS[key] = {
                "key": key, "msg_id": msg_id, "got": 0, "total": declared,
                "started_at": time.time(), "percent": 0.0, "speed_kbps": 0.0,
            }
        last_tick = [time.time(), 0]

        def _progress(current: int, total: int):
            now = time.time()
            dt = now - last_tick[0]
            if dt >= 0.5:
                speed = ((current - last_tick[1]) / dt) / 1024.0
                last_tick[0], last_tick[1] = now, current
                pct = round(100.0 * current / total, 1) if total else 0.0
                with _LOCK:
                    if key in _ACTIVE_DOWNLOADS:
                        _ACTIVE_DOWNLOADS[key].update(
                            got=current, total=total or declared, percent=pct,
                            speed_kbps=round(speed, 1))

        try:
            # The message is fetched here rather than inside
            # `tgchannel.download_by_id` because its declared size is the only
            # thing that can tell us afterwards whether the transfer finished,
            # and `download_by_id` throws that object away.
            msgs = tgchannel.get_messages([int(msg_id)])
            msg = msgs[0] if msgs else None
            if msg is None:
                err = (f"Telegram returned no message {msg_id} — it may have "
                       f"been deleted from the channel")
                _note_error(key, err)
                _mark_failed(key, msg_id, declared, err)
                return False

            facts = tgchannel.media_facts(msg) or {}
            declared = int(facts.get("size") or declared or 0)
            with _LOCK:
                if key in _ACTIVE_DOWNLOADS:
                    _ACTIVE_DOWNLOADS[key]["total"] = declared

            if os.path.exists(tmp):
                # A leftover from a killed run. Restarting from zero is correct:
                # `download_media` writes from the beginning, so a resumed name
                # would interleave two transfers into one file.
                try:
                    os.remove(tmp)
                except OSError:
                    pass

            ok = tgchannel.download_message(msg, tmp, progress=_progress)
            got = os.path.getsize(tmp) if os.path.exists(tmp) else 0

            if not ok or got <= 0:
                err = (tgchannel.last_download_error()
                       or "the transfer produced no file")
                _note_error(key, err)
                _mark_failed(key, msg_id, declared, err)
                return False

            if declared and got != declared:
                err = (f"transfer ended early — {got} of {declared} bytes "
                       f"({100.0 * got / declared:.1f}%)")
                _note_error(key, err)
                _mark_failed(key, msg_id, declared, err)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False

            if not declared and got < _MIN_PLAUSIBLE_BYTES:
                err = f"transfer produced only {got} bytes"
                _note_error(key, err)
                _mark_failed(key, msg_id, 0, err)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False

            os.replace(tmp, dest)
            _mark_verified(key, msg_id, declared, got)
            for stray in _local_candidates(key):
                if os.path.exists(stray + ".short"):
                    try:
                        os.remove(stray + ".short")
                    except OSError:
                        pass
            with _LOCK:
                _STATS["bytes_downloaded"] += got
            log(f"mirrored {key} — {got / (1024 * 1024):.1f} MB, byte-for-byte "
                f"what Telegram declared", SUB, "SUCCESS")
            return True

        except Exception as exc:
            err = f"{type(exc).__name__}: {str(exc)[:200]}"
            _note_error(key, err)
            _mark_failed(key, msg_id, declared, err)
            log(f"could not download {key} (msg {msg_id}) — {err}", SUB, "WARN")
            return False
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            with _LOCK:
                _ACTIVE_DOWNLOADS.pop(key, None)


def _process_item(item: Dict[str, Any], force: bool = False) -> str:
    """Get this reel whole and derived. Returns what it ended up as.

    `force` is what a user's click means: ignore the backoff this reel is
    serving. An automatic sweep must respect the backoff or a deleted message
    costs a Telegram round trip every thirty seconds forever; a person pressing
    Download now has new information — they are watching — and their click should
    not be silently swallowed by a timer they cannot see.
    """
    key = str(item["key"])
    rec = _record(key)
    src = _verified_src(item, rec)

    if not src:
        if _STOP_EVENT.is_set():
            return "stopped"
        due = float(rec.get("next_attempt_at") or 0)
        if not force and due > time.time():
            return "waiting"
        if not _download_video(item):
            return "failed"
        src = _verified_src(item)
        if not src:
            return "failed"

    if derive.complete(key):
        return "ready"

    if _STOP_EVENT.is_set():
        return "stopped"
    if paths.below_floor():
        log(f"disk below the floor — not deriving {key}", SUB, "WARN")
        return "held"

    with _LOCK:
        _ACTIVE_DERIVES[key] = {"key": key, "started_at": time.time()}
    try:
        derive.derive(key, src, force=False)
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:200]}"
        _note_error(key, err)
        log(f"derivation failed for {key} — {err}", SUB, "WARN")
        return "derive-failed"
    finally:
        with _LOCK:
            _ACTIVE_DERIVES.pop(key, None)
    return "derived" if derive.complete(key) else "partial"


# ══════════════════════════════════════════════════════════════════════════
# "DOWNLOAD NOW"
# ══════════════════════════════════════════════════════════════════════════
def prioritize(video_key: str) -> Dict[str, Any]:
    """Move one reel to the front, and report what actually happened.

    Every part of this returns a real answer because every part of it used to
    return `None`: the route said `{"ok": true}` unconditionally and the player
    said "moved to the front of the download queue" whether or not the key was
    something the mirror had ever heard of. A key absent from both the index and
    the ledger was dropped on the floor with a success message on screen.

    Three things happen here that did not before. The reel's backoff is cleared,
    so a click on something that failed nine minutes ago is honoured now. A dead
    Telegram socket is reconnected, because the most common reason a click did
    nothing was a session that died hours earlier — the app had been open
    overnight and every download was failing against a closed connection. And
    the worker is woken, which it now honours between items instead of at the end
    of a full sweep.
    """
    key = str(video_key or "").strip()
    if not key:
        return {"ok": False, "key": key, "state": "invalid",
                "note": "no reel was named"}

    items = _targets()
    match = next((it for it in items if it["key"] == key), None)
    if match is None:
        items = _targets(force=True)
        match = next((it for it in items if it["key"] == key), None)
    if match is None:
        return {"ok": False, "key": key, "state": "unknown",
                "note": ("this reel is in neither the search index nor the "
                         "channel's own upload ledger, so there is nothing to "
                         "download — it may not have finished uploading yet")}

    with _LOCK:
        if key in _ACTIVE_DOWNLOADS:
            live = dict(_ACTIVE_DOWNLOADS[key])
            return {"ok": True, "key": key, "state": "downloading",
                    "percent": live.get("percent", 0.0),
                    "note": f"already downloading — {live.get('percent', 0):.0f}%"}
        if key in _ACTIVE_DERIVES:
            return {"ok": True, "key": key, "state": "deriving",
                    "note": "already being prepared for playback"}

    src = _verified_src(match)
    if src and derive.complete(key):
        return {"ok": True, "key": key, "state": "ready", "position": 0,
                "note": "this reel is already complete on this disk"}

    # A manual request clears the timer. See the docstring.
    rec = _record(key)
    if float(rec.get("next_attempt_at") or 0) > time.time():
        try:
            _mdb().execute(
                "UPDATE mirrored SET next_attempt_at=0 WHERE video_key=?",
                (key,))
            _mdb().commit()
        except sqlite3.Error:
            pass

    reconnected = ""
    if not src:
        health = tgchannel.mtproto_health()
        if health.get("available") and not health.get("connected"):
            tgchannel.mtproto_reconnect("a reel was requested by hand")
            reconnected = " Telegram was disconnected, so it was reconnected."

    with _LOCK:
        if key in _PRIORITY_QUEUE:
            position = list(_PRIORITY_QUEUE).index(key) + 1
        else:
            _PRIORITY_QUEUE.appendleft(key)
            position = 1
        depth = len(_PRIORITY_QUEUE)
    _WAKE_EVENT.set()

    what = "download" if not src else "prepare for playback"
    return {"ok": True, "key": key, "state": "queued", "position": position,
            "queue_depth": depth,
            "note": (f"next in line to {what}" if position == 1
                     else f"number {position} of {depth} in line to {what}")
                    + reconnected}


def reconnect() -> Dict[str, Any]:
    """Retire the Telegram session and open a new one. For the Admin panel."""
    tgchannel.mtproto_reconnect("on request from the mirror panel")
    _WAKE_EVENT.set()
    return {"ok": True, "transport": tgchannel.mtproto_health()}


# ══════════════════════════════════════════════════════════════════════════
# THE SWEEP
# ══════════════════════════════════════════════════════════════════════════
def _tally(items: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count the state of the archive from the disk and the proof ledger.

    Deliberately measured rather than accumulated. The old worker incremented a
    counter as it walked, and set `_STATS["downloaded"]` from a variable computed
    *before* the download it was counting had run — so the number on screen was
    always one cycle behind reality and, after a failure, permanently wrong. A
    count that is recomputed from what is on disk cannot drift.
    """
    recs = _all_records()
    now = time.time()
    have = ver = unver = miss = fail = done = 0
    for it in items:
        key = it["key"]
        rec = recs.get(key) or {}
        path = ""
        local = it.get("local_path")
        if local and os.path.exists(local):
            path = local
        else:
            for cand in _local_candidates(key):
                if os.path.exists(cand):
                    path = cand
                    break
        if path:
            have += 1
            if (float(rec.get("verified_at") or 0) > 0
                    and int(rec.get("expected_bytes") or 0) > 0):
                ver += 1
            else:
                unver += 1
        else:
            miss += 1
        if int(rec.get("attempts") or 0) and float(
                rec.get("next_attempt_at") or 0) > now:
            fail += 1
        if derive.complete(key):
            done += 1
    return {"total_videos": len(items), "downloaded": have, "verified": ver,
            "unverified": unver, "missing": miss, "failing": fail,
            "derived": done}


def _publish(tally: Dict[str, int]) -> None:
    with _LOCK:
        _STATS.update(tally)
        _STATS["last_cycle_at"] = time.time()


def _sleep(seconds: float) -> None:
    _WAKE_EVENT.wait(timeout=seconds)
    _WAKE_EVENT.clear()


def _worker_loop() -> None:
    """Walk the archive until it is whole, then go quiet.

    The inner loop takes its next reel from the priority queue if there is one
    and from the ordered list otherwise, so a click during a sweep is served
    after the reel in flight rather than after the sweep. That is the difference
    between "Download now" meaning now and meaning "in about four minutes".
    """
    global _RUNNING
    log("mirror worker started", SUB)
    try:
        verify_now()
    except Exception as exc:
        log(f"the opening verification pass failed — {type(exc).__name__}: "
            f"{str(exc)[:160]}", SUB, "WARN")

    announced = False
    while not _STOP_EVENT.is_set():
        if _PAUSED:
            _sleep(10.0)
            continue
        if paths.below_floor():
            log(f"paused — less than {paths.FREE_FLOOR_GB:.1f} GB free", SUB,
                "WARN")
            _sleep(30.0)
            continue

        items = _targets(force=True)
        by_key = {it["key"]: it for it in items}
        order = [it["key"] for it in items]
        _publish(_tally(items))
        with _LOCK:
            _STATS["cycles"] += 1

        seen = set()
        cursor = 0
        while cursor < len(order) and not _STOP_EVENT.is_set():
            if _PAUSED or paths.below_floor():
                break

            forced = False
            key = None
            with _LOCK:
                while _PRIORITY_QUEUE:
                    candidate = _PRIORITY_QUEUE.popleft()
                    if candidate not in seen:
                        key, forced = candidate, True
                        break
            if key is None:
                key = order[cursor]
                cursor += 1
                if key in seen:
                    continue
            seen.add(key)

            item = by_key.get(key)
            if item is None:
                # Queued between sweeps and not in the list this sweep started
                # from — refresh rather than drop it, which is what the old
                # worker did.
                items = _targets(force=True)
                by_key = {it["key"]: it for it in items}
                item = by_key.get(key)
                if item is None:
                    continue

            outcome = _process_item(item, force=forced)
            if outcome == "stopped":
                break
            if outcome in ("derived", "partial", "derive-failed"):
                _publish(_tally(items))
            elif outcome == "failed":
                _publish(_tally(items))

        tally = _tally(items)
        _publish(tally)

        whole = (tally["total_videos"] > 0
                 and tally["missing"] == 0
                 and tally["derived"] == tally["total_videos"])
        if whole:
            if not announced:
                announced = True
                with _LOCK:
                    _STATS["complete_at"] = time.time()
                    _STATS["note"] = (
                        f"the whole channel is on this disk — "
                        f"{tally['total_videos']} reel(s), "
                        f"{tally['verified']} byte-verified against Telegram")
                log(f"archive complete — {tally['total_videos']} reel(s) local "
                    f"and derived, {tally['verified']} byte-verified. Nothing "
                    f"further to download unless the channel grows.", SUB,
                    "SUCCESS")
            # A finished archive should not re-stat everything twice a minute.
            # Five minutes is still fast enough to notice a new upload, and a
            # click wakes it instantly.
            _sleep(300.0)
        else:
            announced = False
            with _LOCK:
                _STATS["note"] = (
                    f"{tally['missing']} reel(s) still to download, "
                    f"{tally['total_videos'] - tally['derived']} still to "
                    f"prepare for playback")
            _sleep(30.0)

    _RUNNING = False
    log("mirror worker stopped", SUB)


# ══════════════════════════════════════════════════════════════════════════
# LIFECYCLE
# ══════════════════════════════════════════════════════════════════════════
def start() -> None:
    """Start the mirror worker in a daemon thread."""
    global _RUNNING, _WORKER_THREAD, _PAUSED
    with _LOCK:
        if _RUNNING and _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            return
        _STOP_EVENT.clear()
        _WAKE_EVENT.clear()
        # A worker restarted after a paused stop() must wake runnable, or Start
        # appears to do nothing.
        _PAUSED = False
        _RUNNING = True
        _WORKER_THREAD = threading.Thread(target=_worker_loop,
                                          name="vios-mirror", daemon=True)
        _WORKER_THREAD.start()


def stop() -> None:
    """Stop the mirror worker."""
    global _RUNNING
    _STOP_EVENT.set()
    _WAKE_EVENT.set()
    with _LOCK:
        _RUNNING = False


def pause() -> None:
    global _PAUSED
    _PAUSED = True
    _WAKE_EVENT.set()
    log("mirror paused", SUB)


def resume() -> None:
    global _PAUSED
    _PAUSED = False
    _WAKE_EVENT.set()
    log("mirror resumed", SUB)


def status() -> Dict[str, Any]:
    """Everything the status strip, the Engine tab and the player ask about.

    `transport` is here because of the failure that started this rewrite: the
    session died at 10:53 and the mirror went on reporting `running: true` with
    frozen counters for the next hour. A dead socket now reads as a dead socket,
    with the reason attached, so the screen says "Telegram connection is down"
    rather than leaving a user to infer it from numbers that stopped moving.
    """
    with _LOCK:
        active_dl = list(_ACTIVE_DOWNLOADS.values())
        active_drv = list(_ACTIVE_DERIVES.values())
        recent_errs = list(_RECENT_ERRORS)
        stats = dict(_STATS)
        queued = list(_PRIORITY_QUEUE)
        alive = bool(_WORKER_THREAD is not None and _WORKER_THREAD.is_alive())

    try:
        transport = tgchannel.mtproto_health()
    except Exception as exc:
        transport = {"available": False, "connected": False,
                     "error": f"{type(exc).__name__}: {str(exc)[:120]}"}

    total = int(stats.get("total_videos") or 0)
    done = int(stats.get("downloaded") or 0)
    return {
        "running": bool(_RUNNING and alive),
        "paused": _PAUSED,
        "below_floor": paths.below_floor(),
        "total_videos": total,
        "downloaded": done,
        "verified": int(stats.get("verified") or 0),
        "unverified": int(stats.get("unverified") or 0),
        "missing": int(stats.get("missing") or 0),
        "failing": int(stats.get("failing") or 0),
        "derived": int(stats.get("derived") or 0),
        "percent": round(100.0 * done / total, 1) if total else 0.0,
        "complete": bool(total and stats.get("complete_at")),
        "complete_at": stats.get("complete_at") or 0.0,
        "bytes_downloaded": int(stats.get("bytes_downloaded") or 0),
        "cycles": int(stats.get("cycles") or 0),
        "last_cycle_at": stats.get("last_cycle_at") or 0.0,
        "note": stats.get("note") or "",
        "active_downloads": active_dl,
        "active_derives": active_drv,
        "priority_queued": len(queued),
        "queue": queued[:12],
        "recent_errors": recent_errs,
        "last_error": stats.get("last_error") or "",
        "transport": transport,
        "telegram_ready": config.telegram_ready(),
        "disk": paths.usage(),
    }


def backlog(limit: int = 40) -> Dict[str, Any]:
    """The reels that are not finished, and why — for the Engine tab's queue.

    A list of names with a reason beside each is what makes "see the queue"
    honest. Without it the queue link in the player's warning leads to a page
    that cannot say which reel it is waiting on.
    """
    items = _targets()
    recs = _all_records()
    now = time.time()
    rows = []
    for it in items:
        key = it["key"]
        rec = recs.get(key) or {}
        src = ""
        local = it.get("local_path")
        if local and os.path.exists(local):
            src = local
        else:
            for cand in _local_candidates(key):
                if os.path.exists(cand):
                    src = cand
                    break
        if src and derive.complete(key):
            continue
        due = float(rec.get("next_attempt_at") or 0)
        if not src:
            why = ("waiting to download" if due <= now
                   else f"download failed, retrying in {int(due - now)}s")
        else:
            missing = [n for n, ok in derive.have(key).items() if not ok]
            why = "preparing " + ", ".join(missing) if missing else "finishing"
        rows.append({
            "key": key, "msg_id": it.get("msg_id"),
            "expected_bytes": int(it.get("expected") or 0),
            "have_original": bool(src), "why": why,
            "attempts": int(rec.get("attempts") or 0),
            "last_error": rec.get("last_error") or "",
            "indexed": bool(it.get("indexed")),
        })
        if len(rows) >= max(1, limit):
            break
    return {"ok": True, "waiting": len(rows), "items": rows}
