"""
paths — where this application keeps things, on this machine, forever.

The Kaggle program had two disks with opposite properties: `/kaggle/working`
survived the session but shared a 19.5 GB quota, and `/kaggle/temp` was large but
died every twelve hours. Every storage decision over there is a negotiation
between those two facts, and the negotiation shows: videos live in the disposable
half and are re-downloaded from Telegram on every boot, because they have to be.

None of that is true here. There is one disk, it survives reboots, and the
instruction that shaped this file was explicit: *"dont constrain my application on
disk."* So the layout below is not a cache hierarchy. It is a mirror — the place
the archive actually lives, with Telegram demoted from "storage" to "the thing
that got it here and the backup if this disk dies".

    <HOME>/
      atlas.db            the reader's database — search, graph, library, roadmap
      jobs.db             the local processing queue (see engine/queue.py)
      library.db          watched local folders and their content hashes
      moments.vec         flat float32 matrix, moment vectors
      frames/             flat float32 matrices, per frame-vector space
      bundles/            downloaded bundle parts, mid-restore
      media/
        video/            original mp4s, full resolution, permanent
        proxy/            H.264 faststart short-GOP transcodes — what plays
        poster/           still frames at three tiers, for grids
        sprite/           one scrub sprite-sheet JPEG per video
        frames/           extracted keyframes
      models/             HuggingFace weights (HF_HOME points here)
      session/            pyrogram .session files — CREDENTIAL MATERIAL
      logs/               rotating text logs
      scratch/            anything safe to delete while the app is not running

Two rules this module exists to enforce:

  1. **Nothing precious lives outside HOME.** A user's own video folders are read
     and never written, so every derived artefact — proxy, sprite, poster,
     keyframes, claims — is addressed by content hash under `media/`, never
     placed beside the original. `library.py` depends on this.
  2. **HOME is one environment variable.** `VIOS_LOCAL_HOME` moves the entire
     application state to another drive without touching code, which is the
     answer to the one measured constraint on this machine: C: is the only drive
     and had 69.8 GB free when this was written.

**HOME is `%USERPROFILE%\\VIOS-Data`, and it is deliberately somewhere you can
see.** It used to be `%LOCALAPPDATA%\\VIOS`, which is correct by Windows
convention and wrong for this application, because the instruction that shaped
the layout was *"if i ever need to free up space i can simply clear one folder
where entire data is stored and visible to me in my file manager"* — and
`AppData\\Local` is a hidden directory. A folder you are meant to be able to
delete has to be a folder you can find. `VIOS_LOCAL_HOME` still overrides it, so
moving the archive to another drive is one variable.

Beside the *source* is a different question, and still no: the source directory
is a git working tree, and a 30 GB media mirror inside a git working tree is one
`git clean -xdf` away from gone. `%USERPROFILE%` is visible without being
inside anything that gets cleaned.

Deleting HOME is a supported operation, not a recovery procedure. Everything
under it is either downloaded from the channel (originals, bundles, shards) or
computed from those (proxies, posters, sprites, keyframes, the databases,
the vector matrices), so the whole tree is reconstructible and the app rebuilds
it unattended on the next launch. `stamp_readme()` writes that sentence into the
folder itself, because the person deleting it will be looking at Explorer and
not at this docstring.
"""

from __future__ import annotations

import ctypes
import os
import shutil

# ── HOME ──────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))

# Where state lived before it needed to be visible. Kept as a constant rather
# than inlined because two things read it: the one-time adoption below, and the
# Admin panel, which should be able to say "there is still 900 MB over there"
# if an adoption was ever blocked.
LEGACY_HOME = os.path.join(os.environ.get("LOCALAPPDATA") or
                           os.path.join(_HERE, "_nolocalappdata"), "VIOS")


def _default_home() -> str:
    profile = os.environ.get("USERPROFILE")
    if profile and os.path.isdir(profile):
        return os.path.join(profile, "VIOS-Data")
    local = os.environ.get("LOCALAPPDATA")
    if local and os.path.isdir(local):
        return os.path.join(local, "VIOS")
    # Not Windows, or a stripped environment. Beside the source is wrong for the
    # reason in the docstring, so go one level up instead — a sibling of the
    # working tree, not inside it.
    return os.path.join(os.path.dirname(_HERE), "VIOS-Data")


def _has_content(path: str) -> bool:
    try:
        return any(os.scandir(path))
    except OSError:
        return False


def _adopt_legacy(home: str) -> str:
    """Move a pre-existing hidden home to the visible one. Returns a note.

    One `os.rename` of the whole directory, and no per-file fallback, on purpose.
    A directory rename on the same volume is atomic: either the archive is at the
    new address or it is entirely at the old one, and there is no third state to
    reason about later. Per-file copying is where a half-migrated home comes
    from, and a half-migrated home is worse than either — the databases and the
    media they describe would be in different folders, each looking complete.

    If the rename fails — most likely because a running instance holds
    `atlas.db` open — this returns the legacy path and the caller keeps using it.
    That is not a degraded mode: it is the address the data is at. The next
    launch, with nothing holding the files, adopts it.
    """
    if os.path.abspath(home) == os.path.abspath(LEGACY_HOME):
        return ""
    if not _has_content(LEGACY_HOME):
        return ""
    if _has_content(home):
        # Both exist with content. Adopting would either merge two archives or
        # clobber one, and neither is a decision this module gets to make
        # silently. The visible one wins and the legacy one is left untouched.
        return (f"both {home} and the older {LEGACY_HOME} hold data — using "
                f"the visible one and leaving the other alone")
    try:
        if os.path.isdir(home):
            os.rmdir(home)                   # empty, from a previous `ensure()`
        os.makedirs(os.path.dirname(home), exist_ok=True)
        os.rename(LEGACY_HOME, home)
        return f"moved the data folder out of hidden AppData to {home}"
    except OSError as exc:
        _ADOPT_BLOCKED.append(f"{type(exc).__name__}: {exc}")
        return ""


_ADOPT_BLOCKED: list = []
_WANTED = os.path.abspath(os.environ.get("VIOS_LOCAL_HOME") or _default_home())
ADOPTION_NOTE = _adopt_legacy(_WANTED)

# The adoption failed and the data is still at the old address, so that is the
# address this process uses. Pointing HOME at an empty new folder while 900 MB
# of originals sit in the old one would read to every consumer as "the archive
# is gone" and start a full re-download.
HOME = (LEGACY_HOME if (_ADOPT_BLOCKED and _has_content(LEGACY_HOME))
        else _WANTED)

# ── The layout ────────────────────────────────────────────────────────────
DB_PATH      = os.path.join(HOME, "atlas.db")
JOBS_DB      = os.path.join(HOME, "jobs.db")
LIBRARY_DB   = os.path.join(HOME, "library.db")

# The mirror's proof-of-download ledger, deliberately its own file rather than a
# table in `atlas.db`. `atlas.db` is the one database this app is willing to
# throw away and rebuild (see `dbhealth.py`), and the whole value of the mirror
# ledger is that it can say "these 30 originals on disk are byte-complete"
# *without* re-downloading them to find out. Putting that proof inside the
# disposable database would mean every schema corruption costs a re-download of
# the entire archive.
MIRROR_DB    = os.path.join(HOME, "mirror.db")
VECTOR_PATH  = os.path.join(HOME, "moments.vec")
VECTOR_META  = os.path.join(HOME, "moments.vec.json")

FRAME_VEC_DIR = os.path.join(HOME, "frames")
BUNDLE_DIR    = os.path.join(HOME, "bundles")
MODEL_DIR     = os.path.join(HOME, "models")
SESSION_DIR   = os.path.join(HOME, "session")
LOG_DIR       = os.path.join(HOME, "logs")
SCRATCH_DIR   = os.path.join(HOME, "scratch")

# Evidence shards this machine wrote. Beside `bundles/` rather than inside
# `scratch/` because the two words mean opposite things here: scratch is what a
# pass may leave behind and anything may delete, and a local shard is the only
# copy of work the engine did. `atlas.ingest.import_local_shard` keeps the file
# after replaying it — a downloaded shard is a cache of something the channel
# still holds, a locally written one is not — so this directory is also what
# makes publishing to the channel a later and separate decision.
SHARD_DIR     = os.path.join(HOME, "shards")

# Where a database goes when it cannot be opened. Not `scratch/`, which anything
# may delete, and not deletion, which is the one irreversible option: a corrupt
# `atlas.db` is still the only copy of whatever the last pass computed, and a
# later sqlite build or a `.recover` pass may read what this one cannot. See
# `dbhealth.py` — the app moves the file here, starts a fresh one, and rebuilds
# from Telegram rather than refusing to boot.
QUARANTINE_DIR = os.path.join(HOME, "quarantine")

MEDIA_DIR    = os.path.join(HOME, "media")
VIDEO_DIR    = os.path.join(MEDIA_DIR, "video")
PROXY_DIR    = os.path.join(MEDIA_DIR, "proxy")
POSTER_DIR   = os.path.join(MEDIA_DIR, "poster")
SPRITE_DIR   = os.path.join(MEDIA_DIR, "sprite")
KEYFRAME_DIR = os.path.join(MEDIA_DIR, "frames")

# Where the frontend build lands. `npm run build` writes web/dist; the server
# serves that directory in production and proxies to the Vite dev server when
# VIOS_DEV=1. Both paths are inside the source tree because both are build
# output, not state.
WEB_DIR = os.path.join(_HERE, "web", "dist")
WEB_SRC = os.path.join(_HERE, "web")

_ALL_DIRS = (HOME, FRAME_VEC_DIR, BUNDLE_DIR, MODEL_DIR, SESSION_DIR, LOG_DIR,
             SCRATCH_DIR, SHARD_DIR, QUARANTINE_DIR, MEDIA_DIR, VIDEO_DIR,
             PROXY_DIR, POSTER_DIR, SPRITE_DIR, KEYFRAME_DIR)

_README_NAME = "READ ME - deleting this folder is safe.txt"

_README = """\
This folder is everything VIOS keeps on this computer.

You can delete it.

Not "you can delete it if you are careful" — the application is built so that
this folder is disposable. Every file in here is one of two things:

  * a copy of something that is still in your Telegram channel
      media\\video    the original reels
      bundles        database snapshots the Kaggle side published
      shards         evidence files, which are also uploaded to the channel

  * something the application computed from those, and can compute again
      atlas.db       search index, graph, library, roadmap
      jobs.db        the local processing queue
      library.db     your watched folders and their hashes
      moments.vec    the vectors search runs against
      frames         more vectors
      media\\proxy    the versions that actually play
      media\\poster   thumbnails
      media\\sprite   the images the scrub bar shows
      media\\frames   extracted keyframes
      models         downloaded AI model weights

Delete the whole folder, or any single sub-folder, whenever you need the space.
The next time you open VIOS it will notice what is missing and start fetching
and rebuilding it, unattended. It will take as long as your connection takes.
Nothing is lost that was not already in Telegram.

Two exceptions worth knowing:

  session\\   your signed-in Telegram session. Deleting it is harmless but you
             will be asked for your credentials again.
  logs\\      text logs. Nothing reads them but you.

If you want this folder somewhere else — another drive, for instance — set the
environment variable VIOS_LOCAL_HOME to the path you want and restart the app.
It will use that instead, and you can move the existing folder there yourself.

This file is rewritten by the application every time it starts, so editing it
will not stick.
"""


def stamp_readme() -> None:
    """Write the delete-me-freely note into HOME itself.

    The person who needs this sentence is looking at Explorer, wondering what
    the 30 GB folder called VIOS-Data is and whether removing it breaks
    anything. A docstring in a source file cannot reach them; a text file next
    to the folders can. Rewritten on every boot rather than written once,
    because the folder is meant to be deletable and a note that only exists if
    it was never deleted is the wrong note.
    """
    try:
        with open(os.path.join(HOME, _README_NAME), "w",
                  encoding="utf-8") as fh:
            fh.write(_README)
    except OSError:
        pass                       # a missing note never justifies a failed boot


def ensure() -> None:
    """Create the whole layout. Idempotent, and cheap enough to call on import."""
    for d in _ALL_DIRS:
        os.makedirs(d, exist_ok=True)


ensure()
stamp_readme()

# ── Model weights ─────────────────────────────────────────────────────────
# Set before transformers or huggingface_hub is imported anywhere, which is why
# this lives in the module that everything imports first. Without it HF writes to
# %USERPROFILE%\.cache\huggingface, which is (a) invisible to the disk readout in
# the UI and (b) on C: even when HOME has been moved to another drive — so a user
# who moved HOME to escape a full disk would fill the full disk anyway.
os.environ.setdefault("HF_HOME", MODEL_DIR)
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME",
                      os.path.join(MODEL_DIR, "sentence_transformers"))
os.environ.setdefault("TRANSFORMERS_CACHE", MODEL_DIR)


# ── Free space, measured ──────────────────────────────────────────────────
def free_bytes(path: str = "") -> int:
    """Bytes free on the volume holding `path`, or HOME.

    `shutil.disk_usage` is accurate on Windows and needs no dependency. The
    ctypes fallback exists for the case where HOME is a UNC path, which
    disk_usage has historically mishandled — it costs four lines and removes a
    class of "reported 0 GB free, refused to mirror" report.
    """
    target = path or HOME
    try:
        return int(shutil.disk_usage(target).free)
    except OSError:
        pass
    try:
        free = ctypes.c_ulonglong(0)
        ok = ctypes.windll.kernel32.GetDiskFreeSpaceExW(  # type: ignore[attr-defined]
            ctypes.c_wchar_p(target), None, None, ctypes.pointer(free))
        return int(free.value) if ok else 0
    except Exception:
        return 0


def dir_bytes(path: str) -> int:
    """Recursive size of a directory, tolerant of files vanishing mid-walk.

    A file can disappear between `scandir` and `stat` because the mirror worker
    is writing while the UI is reading. That is normal here, not an error, so the
    missing entry is skipped and the total is reported as slightly stale rather
    than as an exception in a status endpoint.
    """
    total = 0
    stack = [path]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif e.is_file(follow_symlinks=False):
                    total += e.stat().st_size
            except OSError:
                continue
    return total


def usage() -> dict:
    """What the status strip and the Admin disk panel read.

    Deliberately reports both halves — what the app is using and what the volume
    has left — because the eviction safety valve arms on the second number, and a
    panel that shows only the first cannot explain why it armed.
    """
    return {
        "home": HOME,
        "free_bytes": free_bytes(),
        "video_bytes": dir_bytes(VIDEO_DIR),
        "proxy_bytes": dir_bytes(PROXY_DIR),
        "derived_bytes": (dir_bytes(POSTER_DIR) + dir_bytes(SPRITE_DIR)
                          + dir_bytes(KEYFRAME_DIR)),
        "model_bytes": dir_bytes(MODEL_DIR),
        "db_bytes": sum(os.path.getsize(p) for p in
                        (DB_PATH, JOBS_DB, LIBRARY_DB, VECTOR_PATH)
                        if os.path.exists(p)),
    }


# ── The floor, not a quota ────────────────────────────────────────────────
# There is no cache size here on purpose. The old design bounded the video cache
# at 12 GB and evicted LRU, which is correct when the disk dies every twelve
# hours and wrong when it does not: it means the archive is never actually local,
# so every session pays Telegram's rate limits again.
#
# What replaces it is a floor. The mirror keeps pulling until the *volume* is
# down to FREE_FLOOR_GB, and then it stops and says so. It does not delete. A
# background worker silently deleting an archive to make room for more of the
# same archive is the failure mode that turns "my videos are safe in Telegram"
# into "which ones did it drop?", and there is no answer to that question worth
# the disk it saved.
FREE_FLOOR_GB = float(os.environ.get("VIOS_FREE_FLOOR_GB", "12"))


def below_floor() -> bool:
    return free_bytes() < FREE_FLOOR_GB * (1 << 30)
