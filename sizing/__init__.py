"""
sizing — will this model fit on this machine, and if not, why not.

Three files lifted out of the Kaggle program's 17,688-line processing tree,
because they are the only part of it a laptop needs and they encode months of
measurement that would be re-lost if re-derived:

  registry.py   every model declared as *data* — a `Component` row saying what it
                needs, what it costs in VRAM, what it produces and what it depends
                on. `validate()`, `plan_cohorts()` and `unrunnable()` are the
                reason the Engine tab can answer "why can't I run this here"
                before a 6 GB download rather than after.
  resources.py  what this machine actually has, measured: VRAM per card, compute
                capability, whether bf16 and FlashAttention-2 are available.
  base.py       `ModelCache`, which accounts VRAM on every load and drop and
                notices when a model failed to release it.

What is **not** here is everything that exists to survive Kaggle: `store.py`,
`coverage.py` with its leases, `jobs.py`'s broker, `engine.py`'s rotation loop.
Those solve "ten notebooks on ten accounts share work without talking, and each
one dies at twelve hours". This machine has one worker and does not get killed, so
copying them would be importing a distributed system to run a for-loop. See
`engine/queue.py` for what replaces them, and `WIRE.md` for the upstream SHA these
three were taken from.

The two constants below are the ones `registry.py` imports. They are duplicated
from upstream `vios/process/__init__.py` rather than derived, so `WIRE.md` records
them as part of the contract: `CHANNELS` in particular is shared with the
interface — it is the list the UI colours by — so adding a channel means adding a
colour, in both repositories.
"""

from __future__ import annotations

__all__ = ["SCHEMA_VERSION", "CHANNELS"]

# The shard/database schema this application speaks. It must track upstream: a
# shard arriving from Kaggle carries its own `schema` number, and the reader
# compares the two. Higher-than-ours is reported loudly rather than skipped
# quietly — see `atlas/ingest.py` and the banner it feeds.
#
# v2 added per-frame identity (`claim.frame_idx`/`frame_hi`, the packed
# `frame_vector`/`frame_metric` tables). v3 added no columns: it marks that
# `coverage` rows travel *with* the evidence, so a database rebuilt from shards
# knows what has already been done rather than only what was learned.
SCHEMA_VERSION = 3

# The evidence channels. Not a loose vocabulary — the interface colours by exactly
# this list, so it is part of the wire contract and not an implementation detail.
CHANNELS = (
    "speech",     # what was said
    "ocr",        # what was written on screen
    "visual",     # what is physically present in frame
    "audio",      # music, sound design, loudness — the non-speech signal
    "narrative",  # what is happening, and why it holds attention
    "style",      # how it was shot and cut
    "caption",    # what the creator wrote, and what the audience wrote back
    "concept",    # the abstractions: entities, topics, techniques
)
