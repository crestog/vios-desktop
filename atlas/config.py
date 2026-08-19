"""
Atlas configuration — the reader, on one machine, with one disk.

Atlas is the part of this application that answers questions: search, library,
graph, roadmap, the player, the provenance drill-down. It owns no harvester and no
GPU worker; it reads a local database and the media beside it.

**What changed when this was lifted out of the Kaggle repository.** The original
file is a negotiation between two disks with opposite properties — `/kaggle/working`
survived the session but shared a 19.5 GB quota, `/kaggle/temp` was large but was
wiped every twelve hours — and every storage decision in it is shaped by that. The
consequence was that videos lived in the disposable half and were re-downloaded from
Telegram on every boot, bounded by a 12 GB LRU cache, because there was no
alternative.

Here there is one disk that survives reboots, and the instruction was explicit:
*"why are you using cache? can you use disk space instead?"* So:

  * `_ON_KAGGLE` is gone. In this repository it could only ever be false, and a
    dead branch in a path calculation is a trap for whoever reads it next.
  * There is no cache tier. `paths.py` owns the layout and every directory here is
    a view onto it. What used to be `CACHE_DIR/video` is now the permanent mirror.
  * `VIDEO_CACHE_GB` — the 12 GB LRU bound — is gone, replaced by
    `paths.FREE_FLOOR_GB`. The mirror stops and says so when the volume gets low;
    it does not delete. A background worker silently evicting an archive to make
    room for more of the same archive is the failure mode that turns "my videos are
    safe" into "which ones did it drop?".
  * Credentials are not resolved here at all. They delegate to the root `config`
    module, so there is exactly one place that knows the env names, the aliases and
    the on-disk store. Two resolvers is how a session ends up with all four secrets
    stored correctly and still printing "Telegram disabled".

Everything below the storage section is retrieval tuning and is unchanged from
upstream except where a comment says otherwise — those numbers are measured, and
re-deriving them would re-lose them.
"""

import os

import config as _root
import paths

# ── Disks ─────────────────────────────────────────────────────────────────
# One home, from paths.py. ATLAS_HOME is kept as a name because fifteen files in
# this package read it, and renaming it would be churn with no reader benefit.
ATLAS_HOME = paths.HOME
CACHE_DIR  = paths.MEDIA_DIR      # not a cache any more; see the module docstring

DB_PATH       = paths.DB_PATH
BUNDLE_DIR    = paths.BUNDLE_DIR
VECTOR_PATH   = paths.VECTOR_PATH
VECTOR_META   = paths.VECTOR_META
# One flat matrix per frame-vector space, written and read the same way as
# `moments.vec`. Unlike it, these survive a reindex untouched: they are keyed by
# `(video_key, frame_idx)`, which no rebuild reassigns, where moment vectors are
# keyed by `moments.id`, which every rebuild does.
FRAME_VEC_DIR = paths.FRAME_VEC_DIR
SESSION_DIR   = paths.SESSION_DIR

VIDEO_CACHE  = paths.VIDEO_DIR    # originals, permanent
PROXY_DIR    = paths.PROXY_DIR    # faststart short-GOP transcodes — what plays
POSTER_CACHE = paths.POSTER_DIR
SPRITE_DIR   = paths.SPRITE_DIR   # one scrub sprite-sheet JPEG per video
KEYFRAME_DIR = paths.KEYFRAME_DIR

WEB_DIR = paths.WEB_DIR           # web/dist — the built frontend

# paths.ensure() ran at its import, so every directory above exists.

# ── Telegram ──────────────────────────────────────────────────────────────
# Delegated, per access, with no fallback literal — the same PEP 562 lookup the
# root module uses, forwarded rather than reimplemented. `config.BOT_TOKEN` here
# and `config.BOT_TOKEN` there are guaranteed to be the same value because they
# are the same code path.
#
# `from config import BOT_TOKEN` would snapshot at import and never ask again.
# Every reader in this package uses `config.NAME`, which is what makes a
# credential typed into the Admin form live on the next read with no restart.
_FORWARD = ("API_ID", "API_HASH", "BOT_TOKEN", "CHANNEL_ID", "HF_TOKEN",
            "IG_COOKIES")


def __getattr__(name: str):
    if name in _FORWARD:
        return getattr(_root, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(set(globals()) | set(_FORWARD))


def missing_secrets() -> list:
    """Which Telegram secrets are absent, right now, by the name to set."""
    return _root.missing_telegram_secrets()


def telegram_ready() -> bool:
    return _root.telegram_ready()


# ── Retrieval ─────────────────────────────────────────────────────────────
# bge-small-en-v1.5: 33M params, 384 dimensions, and the smallest model that is
# still genuinely good at retrieval. The whole matrix for 200k moments is
# 200k × 384 × 4B = 307 MB, which stays resident in RAM — so a query is one matmul
# against memory, not a trip to a vector database. At this corpus size exhaustive
# search beats an ANN index on both latency and recall, and it removes a service
# from the deployment.
EMBED_MODEL = os.environ.get("ATLAS_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DIM   = int(os.environ.get("ATLAS_EMBED_DIM", "384"))
EMBED_BATCH = int(os.environ.get("ATLAS_EMBED_BATCH", "128"))
# "auto" takes the GPU only when it has room to spare. Upstream this was a
# conflict — Atlas shared a machine with the harvester's Qwen shards, and a second
# process taking VRAM is how the GPU worker died mid-narrative. Here the local
# engine is a *thread in this process*, so `ModelCache` sees both allocations and
# the conflict is accounted rather than accidental.
EMBED_DEVICE = os.environ.get("ATLAS_EMBED_DEVICE", "auto").lower()

# Weights live under paths.MODEL_DIR, which paths.py already exported into HF_HOME
# before transformers could be imported. Read back from the environment rather than
# recomputed, so an explicit HF_HOME override still wins.
HF_CACHE = os.environ["HF_HOME"]
ST_CACHE = os.environ["SENTENCE_TRANSFORMERS_HOME"]
# bge-v1.5 was trained with an asymmetric instruction on the query side only.
# Dropping it costs a few points of recall, so it is not optional.
EMBED_QUERY_PREFIX = ("Represent this sentence for searching relevant "
                      "passages: ")

# Candidate depth per retriever before fusion. 200 is past the point where deeper
# retrieval changes the top 20, and keeps the fuse itself trivial.
CANDIDATES   = int(os.environ.get("ATLAS_CANDIDATES", "200"))
RRF_K        = 60      # the constant from the original RRF paper
MOMENT_GAP_S = 6.0     # hits closer than this in one video are one moment
QUERY_CACHE  = 256     # LRU entries

# ── Image search ──────────────────────────────────────────────────────────
# The frame vectors the processing plane writes, searched in their own spaces.
# `siglip2` and `clip` are two different geometries; a query is only ever compared
# against the space it was produced in.
#
# The resident matrix is bounded by construction rather than by hope: 62 videos at
# ~900 frames × 1152 dims × 4 B is 257 MB. `VSEARCH_MAX_MB` picks a frame stride so
# the matrix fits and records it in the meta — so growth costs *resolution*, never
# memory, and the number is visible rather than inferred. The stride is only the
# coarse pass: the top videos are re-ranked against their full-rate rows read
# straight from `vec_payload`, so the answer is frame-exact.
VSEARCH_MAX_MB = int(os.environ.get("ATLAS_VSEARCH_MAX_MB", "256"))
VSEARCH_SPACES = ("siglip2", "clip")

# CLIP ViT-L/14 for the query side: 1.7 GB against SigLIP2-so400m's ~4 GB, both
# towers in one checkpoint, and stronger on proper nouns and logos — which is what
# a search box actually receives. Loaded only on the first image-or-text-into-image
# query, so a session that never runs one never pays for it.
VSEARCH_MODEL = os.environ.get("ATLAS_VSEARCH_MODEL",
                               "openai/clip-vit-large-patch14")
# Still CPU by default, and the reason changed. Upstream it was because the
# processing plane owned both cards for the whole session. Here it is RAM: measured
# on this machine, 1.6 GB is available of 12 GB installed, and a 1.7 GB CPU
# checkpoint is most of that. So the honest default is to keep it off the GPU *and*
# load it lazily, and to let ATLAS_VSEARCH_DEVICE=cuda be a deliberate choice made
# when the Engine tab is idle.
VSEARCH_DEVICE     = os.environ.get("ATLAS_VSEARCH_DEVICE", "cpu").lower()
VSEARCH_CANDIDATES = int(os.environ.get("ATLAS_VSEARCH_CANDIDATES", "24"))

# Relative trust in each kind of evidence. Qwen's narrative is a model looking at
# the video and describing it, so it outranks an object list; OCR is exact but
# often noise from a watermark. These are multipliers on the fused score.
SOURCE_WEIGHT = {
    "narrative": 1.30,
    "speech":    1.15,
    "visual":    1.00,
    "ocr":       0.85,
    "caption":   0.95,
    "meta":      0.70,
}

# ── Media ─────────────────────────────────────────────────────────────────
# The Bot API caps getFile at 20 MB, which is under the size of a long reel, so
# downloads go over MTProto and this is only the fallback threshold.
HTTP_DOWNLOAD_LIMIT = 20 * 1024 * 1024

# There is no cache size. `media.resolve()` still answers local/cache/remote, and
# the mirror worker's job is to make the answer "local" for everything; the floor
# that stops it lives in paths.py, and it warns instead of evicting.
FREE_FLOOR_GB = paths.FREE_FLOOR_GB

# Speculative prefetch stays even though the mirror makes it mostly redundant,
# because it is exactly what covers the window *before* the mirror finishes — the
# first hour after a cold start, when the top results of a query are the only
# videos that matter. Playback streams 1 MiB chunks, so warming a result costs two
# chunks (the head where playback starts, the tail where a phone-written mp4 keeps
# its moov atom), not the whole file.
PREFETCH_TOP_N = int(os.environ.get("ATLAS_PREFETCH", "12"))

# ── Proxy encoding — the latency budget, as ffmpeg flags ──────────────────
# These four numbers are the difference between "instant" and "a second and a
# half", and they are paid once per video at ingest instead of on every play.
#
#   faststart      moves the moov atom to the *front*. Instagram's own mp4s often
#                  put it at the end, which forces a reader to fetch the whole file
#                  before it can show frame one. This is the single biggest
#                  playback win available and it costs one extra pass over the file.
#   GOP ≈ 1 s      a keyframe about every second, so a seek lands on a nearby
#                  keyframe instead of decoding forward from a distant one. This is
#                  *the* seek-latency lever.
#   sprite sheet   ~100 frames in one JPEG grid. Scrub preview then moves a CSS
#                  background-position: zero decode, zero requests, so it can hold
#                  a 16 ms budget that no thumbnail endpoint could.
#   CRF 23 / 720w  small enough that a 5,000-reel archive is tens of GB, good
#                  enough that the proxy is what you actually watch.
PROXY_WIDTH      = int(os.environ.get("VIOS_PROXY_WIDTH", "720"))
PROXY_CRF        = int(os.environ.get("VIOS_PROXY_CRF", "23"))
PROXY_GOP_SECS   = float(os.environ.get("VIOS_PROXY_GOP_SECS", "1.0"))
PROXY_PRESET     = os.environ.get("VIOS_PROXY_PRESET", "veryfast")
SPRITE_COLUMNS   = int(os.environ.get("VIOS_SPRITE_COLUMNS", "10"))
SPRITE_ROWS      = int(os.environ.get("VIOS_SPRITE_ROWS", "10"))
SPRITE_TILE_W    = int(os.environ.get("VIOS_SPRITE_TILE_W", "160"))
# Three poster tiers, so the density slider changes *what is fetched*, not just
# CSS. A 12-column grid asking for 720px posters is how a grid drops frames.
POSTER_TIERS     = (160, 360, 720)

# ── Server ────────────────────────────────────────────────────────────────
# A default for running `python -m server` bare. The desktop shell picks a free
# port instead and tells the window about it, so two copies of the app cannot
# collide on 7000.
PORT = int(os.environ.get("ATLAS_PORT", "7000"))
