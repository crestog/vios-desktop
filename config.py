"""
config — the small surface the lifted modules expect, pointed at this machine.

`tg_transport.py`, `db_restore.py`, `db_export.py` and `capture/engine.py` were
written against the Kaggle program's 412-line `config.py`. They use ten names out
of it. Rather than copy a file whose every interesting line is a negotiation with
a filesystem that does not exist here — a 19.5 GB working quota, a scratch disk
that is wiped every twelve hours, `_pick_scratch()` choosing between them — this
provides those ten names and delegates the rest to `paths.py`.

What is deliberately absent, and why:

  REDIS_*         there is no broker here. The Kaggle plane needs one because ten
                  notebooks on ten accounts must share work without talking; one
                  laptop with one worker thread does not.
  OMNI_*          the Omniscient subsystem and its Postgres are not part of this
                  application. `db_export`/`db_restore` name four OMNI_PG_*
                  constants, so they are defined and empty, and the Postgres dump
                  step self-skips — which is the behaviour that was already
                  correct for a machine without Postgres.
  _pick_scratch   one disk, so there is nothing to pick.

**Credentials are read per access and have no fallback literal.** That rule comes
out of a live bot token having once been committed to a public repository, and it
does not relax because this repo is private — a private repo is a second layer,
not a replacement. The mechanism is PEP 562 `__getattr__`, so `config.BOT_TOKEN`
is a lookup evaluated *now*: a credential typed into the Admin form is live on the
next read, with no restart, and a missing one can become present without the
process having frozen "absent" into a global.

`from config import BOT_TOKEN` would defeat that by binding the value at import.
Every reader in this repository uses `config.NAME` for exactly that reason.
"""

from __future__ import annotations

import os
import time

import creds
import paths

# ── Disks ─────────────────────────────────────────────────────────────────
# Names the lifted modules import. All of them resolve into the one home.
BASE_DIR    = paths.HOME
LAKE_DIR    = os.path.join(paths.HOME, "lake")
DB_PATH     = os.path.join(LAKE_DIR, "lake.db")
SCRATCH_DIR = paths.SCRATCH_DIR

os.makedirs(LAKE_DIR, exist_ok=True)

# 30 seconds, and every `sqlite3.connect` in this repository must pass it. The
# default is 5, which is shorter than a WAL checkpoint under a mirror worker that
# is writing while the UI reads — and the symptom of getting this wrong is
# "database is locked" surfacing in a search box.
SQLITE_TIMEOUT = 30

# ── Postgres, absent ──────────────────────────────────────────────────────
# Defined so `from config import OMNI_PG_*` resolves in the lifted export code,
# empty so its Postgres branch skips itself rather than trying localhost:5432 and
# waiting out a connection timeout on every bundle build.
OMNI_PG_DB       = os.environ.get("VIOS_PG_DB", "")
OMNI_PG_USER     = os.environ.get("VIOS_PG_USER", "")
OMNI_PG_PASSWORD = os.environ.get("VIOS_PG_PASSWORD", "")
OMNI_PG_HOST     = os.environ.get("VIOS_PG_HOST", "")
OMNI_ENABLED     = False

# ── Telegram ──────────────────────────────────────────────────────────────
# The four Telegram credentials, by the attribute name callers use. The env
# names, the aliases and the on-disk store are all `creds.py`'s business: it
# already knows that a token may be stored as VIOS_BOT_TOKEN,
# VIOS_TELEGRAM_BOT_TOKEN, TELEGRAM_BOT_TOKEN or ATLAS_BOT_TOKEN, and a second
# alias list here is precisely the "two halves disagreed about spelling" failure
# its own comments document. So there is no alias list in this file.
_FIELDS = {
    "API_ID":     "api_id",
    "API_HASH":   "api_hash",
    "BOT_TOKEN":  "bot_token",
    "CHANNEL_ID": "channel_id",
    "HF_TOKEN":   "hf_token",
    "IG_COOKIES": "ig_cookies",
}
_INT = ("API_ID", "CHANNEL_ID")

# The credential file is re-read rather than cached at import, because "typed
# into the form thirty seconds ago" has to work. Re-reading it on *every* access
# would put a disk hit inside `tg_transport`'s per-request path, so it is memoised
# for a second — long enough that a burst of reads costs one stat, short enough
# that no human notices the delay.
_FILE_TTL = 1.0
_file_at = 0.0
_file_val: dict = {}


def _from_file() -> dict:
    global _file_at, _file_val
    now = time.monotonic()
    if now - _file_at < _FILE_TTL:
        return _file_val
    try:
        _file_val = creds._from_file()  # noqa: SLF001 — one owner, this is it
    except Exception:
        _file_val = {}
    _file_at = now
    return _file_val


def _credential(field: str) -> str:
    """One credential, resolved now: environment first, then the local file.

    Environment wins so an explicit `set VIOS_BOT_TOKEN=...` still overrides a
    stored value for one session without deleting it — the same precedence
    `creds.resolve()` applies, kept identical on purpose.
    """
    for label in creds.labels(field):
        val = os.environ.get(label, "").strip()
        if val:
            return val
    return str(_from_file().get(field, "") or "").strip()


def __getattr__(name: str):
    if name in _FIELDS:
        raw = _credential(_FIELDS[name])
        if name in _INT:
            try:
                return int(raw) if raw else 0
            except ValueError:
                return 0
        return raw
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list:
    return sorted(set(globals()) | set(_FIELDS))


def missing_telegram_secrets() -> list:
    """Which Telegram secrets are absent, right now, by the name to set.

    Recomputed per call. A caller that finds them missing can ask again a minute
    later and get a different — and true — answer, which is the whole reason
    nothing here is a module global.
    """
    return [creds.FIELDS[_FIELDS[attr]][0]
            for attr in ("API_ID", "API_HASH", "BOT_TOKEN", "CHANNEL_ID")
            if not __getattr__(attr)]


def telegram_ready() -> bool:
    return not missing_telegram_secrets()


# ── The one setting that must never be set ────────────────────────────────
# PYTORCH_CUDA_ALLOC_CONF is absent from this file, and that is deliberate rather
# than an oversight. `expandable_segments:True` lets the allocator unmap pages
# that bitsandbytes still holds raw device pointers into, and the result is a
# sticky `illegal memory access` that survives every later CUDA call in the
# process — so the failure appears in a pass that is not the one that caused it.
# Do not "optimise" it here. The upstream file documents the measured session at
# length; the reasoning survives the copy.
if os.environ.get("PYTORCH_CUDA_ALLOC_CONF"):
    os.environ.pop("PYTORCH_CUDA_ALLOC_CONF", None)
