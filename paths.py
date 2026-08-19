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

`%LOCALAPPDATA%\\VIOS` is the default rather than a folder beside the source,
because the source directory is a git working tree and a 30 GB media mirror
inside a git working tree is a trap — one `git clean -xdf` and the archive is
gone. Keeping state out of the tree makes that mistake unreachable.
"""

from __future__ import annotations

import ctypes
import os
import shutil

# ── HOME ──────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))


def _default_home() -> str:
    local = os.environ.get("LOCALAPPDATA")
    if local and os.path.isdir(local):
        return os.path.join(local, "VIOS")
    # Not Windows, or a stripped environment. Beside the source is wrong for the
    # reason in the docstring, so go one level up instead — a sibling of the
    # working tree, not inside it.
    return os.path.join(os.path.dirname(_HERE), "VIOS-Data")


HOME = os.path.abspath(os.environ.get("VIOS_LOCAL_HOME") or _default_home())

# ── The layout ────────────────────────────────────────────────────────────
DB_PATH      = os.path.join(HOME, "atlas.db")
JOBS_DB      = os.path.join(HOME, "jobs.db")
LIBRARY_DB   = os.path.join(HOME, "library.db")
VECTOR_PATH  = os.path.join(HOME, "moments.vec")
VECTOR_META  = os.path.join(HOME, "moments.vec.json")

FRAME_VEC_DIR = os.path.join(HOME, "frames")
BUNDLE_DIR    = os.path.join(HOME, "bundles")
MODEL_DIR     = os.path.join(HOME, "models")
SESSION_DIR   = os.path.join(HOME, "session")
LOG_DIR       = os.path.join(HOME, "logs")
SCRATCH_DIR   = os.path.join(HOME, "scratch")

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
             SCRATCH_DIR, MEDIA_DIR, VIDEO_DIR, PROXY_DIR, POSTER_DIR,
             SPRITE_DIR, KEYFRAME_DIR)


def ensure() -> None:
    """Create the whole layout. Idempotent, and cheap enough to call on import."""
    for d in _ALL_DIRS:
        os.makedirs(d, exist_ok=True)


ensure()

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
