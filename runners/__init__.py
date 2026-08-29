"""
runners/ — the passes this laptop can actually run, and the seam where the
engine stops reporting work it did not do.

Until now `engine_queue._worker_loop` contained this, between claiming a job and
marking it `completed`:

    # Execute pass (stubbed out / family runner hook)
    # In Phase 5.2 family runners execute here.
    # Once pass finishes, write shard and import
    time.sleep(0.05)  # Simulate execution yield

Fifty milliseconds of sleep, then `completed`. Every pass in the catalogue
reported success, none of them ran, and the lie was *sticky*: `enqueue_job`'s
`ON CONFLICT … DO UPDATE … WHERE state IN ('failed','unrunnable')` means a job
that claimed to succeed can never be re-queued, so re-running the sweep would
not have found the problem either. This package is what goes in that gap.

**Ten of the catalogue's fifty-odd passes have an implementation here.** That is
not most of them and the number is not hidden — `blocked()` returns a reason for
every id that has no runner, the queue writes that reason into the job row, and
the Engine tab shows it. A pass with no runner is `unrunnable` with *"no runner
on this machine"*, which is retryable the moment one exists, and is a completely
different row from a pass that ran and failed. The whole point of the change is
that those two stop looking the same.

**Which ten, and why those.** Everything ffmpeg can measure on a CPU, and
nothing else. `probe`, `artifacts`, `allframes` and `shots-cpu` are in
`structure`; `cuts`, `motion`, `colour`, `loudness`, `caption` and `perframe`
are in `signal`. What is missing is missing for one reason — this machine has
`numpy` and no `cv2`, no `torch`, no `soundfile`, no `faster_whisper` — and
`registry.missing_modules` already says so per component, so the Engine tab can
name the library rather than shrugging.

**The observer id is derived from what actually did the work.** Upstream's
`motion` fits an affine transform with OpenCV; this one takes the mean absolute
frame difference out of ffmpeg. Same component id, same catalogue row, entirely
different method — so they must not hash to the same observer, or whichever
landed first would silently claim the other's rows through `INSERT OR IGNORE`.
Feeding `MODELS[cid]` into the hash is what keeps them two observers with two
sets of claims, which is also what lets a later comparison ask which one was
right. Where the method genuinely is identical — `cuts` is arithmetic over the
shot table on either machine — the ids collide on purpose and the rows dedupe.

**Nothing here writes to the database.** A runner returns an `Emission` and a
dict of extra tables; `to_rows` turns those into shard rows; the queue writes a
shard with `shardwriter.write_shard` and imports it with
`ingest.import_local_shard`. So evidence produced on this laptop travels the
same wire format as evidence produced on Kaggle, through the same replay code,
and a pass that dies three quarters of the way through leaves nothing behind —
the shard was never written. That is the same bargain `sizing/base.py` describes
for the processing plane, kept here rather than reimplemented.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field

import paths
from sizing import registry
from sizing.base import DeferPass, Emission, Job, SkipPass

from . import ff, signal, structure

# ══════════════════════════════════════════════════════════════════════════
# WHAT EXISTS
# ══════════════════════════════════════════════════════════════════════════

HANDLERS = {
    "probe":      structure.probe,
    "artifacts":  structure.artifacts,
    "allframes":  structure.allframes,
    "shots-cpu":  structure.shots_cpu,
    "cuts":       signal.cuts,
    "caption":    signal.caption,
    "colour":     signal.colour,
    "loudness":   signal.loudness,
    "motion":     signal.motion,
    "perframe":   signal.perframe,
}

# What performed the measurement — the string that goes into the observer hash.
# Deliberately concrete: `ffmpeg/ebur128` is a citable implementation of ITU-R
# BS.1770 and `arithmetic` is a promise that nothing was estimated. A component
# whose local method matches upstream's carries upstream's own `model` (empty
# for the pure-arithmetic passes), so the two machines produce one observer and
# their rows merge instead of doubling.
MODELS = {
    "probe":      "ffprobe",
    "artifacts":  "ffmpeg",
    "allframes":  "ffmpeg",
    "shots-cpu":  "ffmpeg/scdet",
    "cuts":       "",                    # arithmetic over the shot table
    "caption":    "",                    # regex over the uploader's own text
    "colour":     "ffmpeg/signalstats",
    "loudness":   "ffmpeg/ebur128",
    "motion":     "ffmpeg/scdet.mafd",
    "perframe":   "ffmpeg/signalstats",
}

# Each runner's *own* prerequisites, which are not the catalogue's. The
# catalogue describes upstream's implementation — `colour` declares `cv2` there
# because upstream runs k-means in CIELAB with OpenCV — and using that list to
# decide what this machine can run would block six passes that are sitting right
# here working. `blocked()` uses this instead, and the divergence is the point:
# the same component id, honestly reached two different ways.
REQUIRES = {
    "probe":      (),
    "artifacts":  (),
    "allframes":  (),
    "shots-cpu":  (),
    "cuts":       (),
    "caption":    (),
    "colour":     (),      # numpy buys the palette; the other four run without
    "loudness":   (),
    "motion":     (),
    "perframe":   (),
}

# The passes that open the video file. Everything else reads the database, and
# `cuts` and `caption` genuinely do run on a machine with no ffmpeg at all.
DECODES = frozenset({"probe", "artifacts", "allframes", "shots-cpu",
                     "colour", "loudness", "motion", "perframe"})

# What each local pass needs to have run *first*, by local component id.
#
# `registry.topo_sort` gets the current ten right, and it gets them right by
# accident. The catalogue says `cuts` needs `shots` and `motion` needs
# `('shots', 'allframes')` — true of the plane those declarations describe, where
# `shots` is TransNetV2 on a GPU. Sorted over those `needs`, the edge into
# `shots-cpu` does not exist, so `cuts` is in the ready set from the first
# iteration and the only thing keeping it behind the shot pass is the tie-break:
# `shots-cpu` is stage 0 and its four dependents are stage 1, so the stage sort
# happens to emit them in a working order. Measured over all 512 subsets that
# contain `shots-cpu`, there is no case where `topo_sort` currently gets it
# wrong.
#
# Which is worth stating precisely, because the accident is one field deep. Give
# `shots-cpu` `stage=1` — the stage its own dependents have, and the value a
# later author would reasonably pick for a pass that runs alongside them — and
# `topo_sort` immediately emits `cuts`, `colour` and `loudness` before it, `cuts`
# skips with *"no shots for this reel yet"* on every reel in the archive, and the
# queue works and produces nothing. Declaring the edges means the order survives
# that edit instead of depending on nobody making it.
#
# Two of these are hard and three are soft, and the soft ones matter more.
# `cuts` raises `SkipPass` without a shot table, so getting its order wrong is
# loud. `motion`, `perframe` and `loudness` all run perfectly happily with no
# shots and are *wrong*: `_across_cuts` returns an empty set, the frames that
# straddle a cut stay in the mean absolute difference series, and `motion_energy`
# comes out 2.4× too high with nothing anywhere saying why. A silent wrong number
# is the failure this file exists to prevent, so the soft edges are declared with
# the hard ones and the ordering is not left to luck.
LOCAL_NEEDS = {
    "probe":      (),
    "artifacts":  (),
    "allframes":  (),
    "shots-cpu":  (),
    "cuts":       ("shots-cpu",),   # hard — SkipPass with no shot table
    "motion":     ("shots-cpu",),   # soft — cut frames poison mafd without it
    "colour":     (),               # per-frame only; reads no shot
    "loudness":   ("shots-cpu",),   # soft — shot_level needs the spans
    "caption":    (),               # the uploader's text; needs no decode
    "perframe":   ("shots-cpu",),   # soft — freeze spans read the mafd series
}


def order(ids) -> list:
    """`ids` in an order that satisfies every prerequisite. Deterministic.

    Uses `LOCAL_NEEDS` for a component with a runner and the catalogue's own
    `needs` for one without, so a queue holding both kinds still comes out in a
    sane order. Ties break on (stage, id) exactly as `registry.topo_sort` breaks
    them — two machines with the same catalogue derive the same plan, which is
    what lets them share a coverage table without ever talking.

    A prerequisite that is not in `ids` is dropped rather than added. Enqueueing
    `cuts` alone is a legitimate thing to ask for — the shots may already be in
    the database from Kaggle — and silently expanding one requested pass into two
    would put a job in the queue nobody asked for. If the shots genuinely are
    missing, `cuts` skips and says so, which is the answer.

    A cycle cannot happen with the table above, and if one is ever introduced the
    remaining ids are appended in sorted order rather than raising: a bad edge in
    a dependency table must not stop the queue from draining.
    """
    want = list(dict.fromkeys(ids))
    wanted = set(want)
    stage = {i: (registry.BY_ID[i].stage if i in registry.BY_ID else 99, i)
             for i in want}
    left = {i: {n for n in _needs(i) if n in wanted and n != i} for i in want}
    out = []
    ready = sorted((i for i, d in left.items() if not d), key=stage.get)
    while ready:
        cur = ready.pop(0)
        out.append(cur)
        left.pop(cur, None)
        fresh = [i for i, d in left.items() if cur in d and len(d) == 1]
        for i in left:
            left[i].discard(cur)
        ready = sorted(ready + fresh, key=stage.get)
    return out + sorted(left, key=stage.get)


def _needs(cid: str) -> tuple:
    if cid in LOCAL_NEEDS:
        return LOCAL_NEEDS[cid]
    c = registry.BY_ID.get(cid)
    return tuple(c.needs) if c else ()


# ══════════════════════════════════════════════════════════════════════════
# THE ONE COMPONENT THIS SIDE ADDS
# ══════════════════════════════════════════════════════════════════════════

SHOTS_CPU = registry.Component(
    id="shots-cpu", title="Shot detection (CPU)", stage=registry.STAGE_STRUCTURE,
    family="ffmpeg", wave=registry.WAVE_SPINE,
    summary="Cut boundaries from mean absolute frame difference, no GPU.",
    detail=(
        "ffmpeg's `scdet` filter compares each frame to the one before it and "
        "reports a boundary when the mean absolute difference crosses a "
        "threshold. It is a weaker detector than the TransNetV2 the `shots` "
        "component declares — it cannot tell a hard cut from a fast whip pan "
        "and it misses a dissolve entirely — and it is the honest ceiling of "
        "what a machine with no CUDA can do. Every row it writes carries "
        "detector='scdet' so the ceiling is recorded in the data rather than "
        "remembered, and the pass refuses to run at all over shots some other "
        "detector already found: shard replay fills NULLs and never overwrites, "
        "so a CPU guess written first would make itself permanent."),
    model="ffmpeg/scdet", device="cpu", seconds=2.0, ram_mb=256,
    needs=(), produces=("shots",), requires=("subprocess",),
    kaggle_ok=False,
    params={"min_shot": structure.MIN_SHOT, "threshold": ff.SCD_THRESHOLD},
    notes="Local to the desktop application. Not part of the shared catalogue.",
)


def install() -> None:
    """Put `shots-cpu` in the catalogue. Idempotent; called on import.

    `sizing/registry.py` is a copy of the processing plane's catalogue and is
    kept comparable with it line for line, so a pass that only exists here is
    registered from the outside rather than added to the list. That keeps the
    local addition next to the local code that justifies it, and keeps the
    diff between the two catalogues meaning what WIRE.md says it means.
    """
    registry.register(SHOTS_CPU)


install()


def have() -> set:
    """Component ids with an implementation in this package."""
    return set(HANDLERS)


def blocked(ids=None, res: dict = None) -> dict:
    """Every id that cannot run here, and why. `{component_id: reason}`.

    Three sources, ordered by which constraint actually binds. The absence of a
    runner comes first for any pass this machine has no code for, because no
    purchase and no `pip install` moves it: `shots` needs a local implementation
    written, and until then "not installed on this machine: torch, scenedetect"
    is a true sentence about the wrong plane. The catalogue's hardware and
    library verdicts survive as a trailing clause on those rows, and stand alone
    for the passes that *do* have a runner. Then ffmpeg, which is one binary
    standing between this machine and eight of the ten passes that work.

    The reason matters more than the boolean. It is what the queue writes into
    the job row and what the Engine tab shows, and *"no runner on this machine
    yet"* is a sentence a user can act on — install nothing, wait — while a
    blank `unrunnable` is not, and a `pip install` that changes nothing is worse
    than either.
    """
    ids = list(ids if ids is not None else registry.all_ids())
    out = dict(registry.unrunnable(ids, res or {}))

    # Re-examine anything `unrunnable` held on grounds that describe upstream's
    # implementation rather than this one. `colour` declares `cv2` and runs
    # k-means in CIELAB on Kaggle; here it is `signalstats` plus a histogram and
    # needs nothing, so leaving the catalogue's verdict in place would block six
    # working passes. Only components with a runner are re-examined, and only
    # against `REQUIRES` — the hardware verdicts are left exactly as they were.
    for cid in list(out):
        if cid not in HANDLERS:
            continue
        gone = [m for m in REQUIRES.get(cid, ()) if not _importable(m)]
        if gone:
            out[cid] = ("not installed on this machine: " + ", ".join(gone)
                        + " — pip install it and re-queue")
        else:
            del out[cid]

    if not ff.available():
        for cid in ids:
            if cid in DECODES:
                out[cid] = "ffmpeg is not on PATH — nothing can be decoded"

    # The absence of a runner outranks whatever the catalogue said, because it is
    # the constraint that actually binds. `shots` came back "not installed on
    # this machine: torch, scenedetect — install them and re-queue", and the tab
    # printed that sentence next to a chip reading *no runner*: two claims about
    # one row that disagree, and the pip advice is the false one. Installing both
    # libraries leaves `shots` with no code here to call — the local shot pass is
    # `shots-cpu` — so a reason that reads as an instruction sends the user to
    # fetch 2 GB of wheels for nothing.
    #
    # The catalogue's verdict is kept as a trailing clause rather than dropped,
    # in the register of a note about the other plane instead of a thing to do.
    # For `describe` that clause is the one that says *don't bother writing the
    # runner either* — a 6,200 MB model was never going to fit on this card — and
    # for `shots` it is the shopping list for whoever writes the local pass.
    mods = registry.missing_modules(ids)
    for cid in ids:
        if cid in HANDLERS:
            continue
        prior = out.get(cid)
        note = ""
        if prior is not None and prior != mods.get(cid):
            # Held on hardware: the loop in `registry.unrunnable` reached a
            # verdict before `missing_modules` was consulted at all.
            note = f"and it would not fit: {prior}"
        else:
            c = registry.BY_ID.get(cid)
            # Only the modules that are actually absent, not the whole `requires`
            # tuple: `tag` declares numpy, numpy is here, and "the GPU-plane
            # build needs numpy" would invent a shortfall to explain a row whose
            # only real shortfall is the missing code.
            need = [m for m in (c.requires or ()) if not _importable(m)] if c else []
            if need:
                note = f"the GPU-plane build needs {', '.join(need)}"
        out[cid] = "no runner on this machine yet" + (f" · {note}" if note else "")
    return out


def _importable(mod: str) -> bool:
    import importlib.util                                        # noqa: PLC0415
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, AttributeError, ValueError):
        return False


# ══════════════════════════════════════════════════════════════════════════
# THE STORE A LOCAL JOB READS THROUGH
# ══════════════════════════════════════════════════════════════════════════

class LocalStore:
    """The read side of `atlas.db`, shaped like the processing plane's Store.

    Runners were written against `Store` and must stay written against it —
    `sizing/base.py` is a copy of upstream's file and every pass in this package
    is meant to be liftable to Kaggle unchanged. So this supplies the three
    methods a local pass actually calls and nothing more, rather than the whole
    interface: `shots`, `detectors` and `claims`.

    Read-only by construction. Nothing in this class writes, because the shard
    is the only way evidence enters the database and a runner that could reach
    around it would be a second writer with different rules.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── shots ───────────────────────────────────────────────────────────
    def shots(self, video_key: str) -> list:
        """`[{idx, t0, t1, score, detector, keyframe}]` in order.

        Tolerant of the column the row is stored under. The canonical shot
        table names the boundary confidence `score`; this laptop's `shot` table
        was created by a test fixture that called it `scene_score`, and the
        first real shard widens the table so both columns exist with only one
        of them filled per row. Probing the schema costs one `PRAGMA` and is
        the difference between rendering an archive and rendering NULLs.
        """
        cols = self._columns("shot")
        if not cols:
            return []
        pick = [c for c in ("idx", "t0", "t1", "score", "scene_score",
                            "detector", "keyframe") if c in cols]
        sql = (f'SELECT {", ".join(chr(34) + c + chr(34) for c in pick)} '
               f'FROM shot WHERE video_key = ? ORDER BY "idx"')
        out = []
        for row in self.conn.execute(sql, (str(video_key),)):
            d = dict(zip(pick, row))
            if d.get("score") is None and d.get("scene_score") is not None:
                d["score"] = d["scene_score"]
            d.pop("scene_score", None)
            d.setdefault("detector", None)
            d.setdefault("keyframe", None)
            out.append(d)
        return out

    def detectors(self, video_key: str, exclude: str = "") -> str:
        """The name of another detector that already wrote this reel's shots.

        Empty string for *"nothing else has"*, which is the answer that lets a
        CPU pass proceed. A row with a NULL detector is not counted as foreign:
        the local `shot` table predates the column entirely, so NULL means
        unknown provenance rather than someone else's work, and treating it as
        foreign would make `shots-cpu` permanently skip a reel it should own.
        """
        if "detector" not in self._columns("shot"):
            return ""
        row = self.conn.execute(
            'SELECT DISTINCT "detector" FROM shot WHERE video_key = ? '
            'AND "detector" IS NOT NULL AND "detector" != "" '
            'AND "detector" != ? LIMIT 1',
            (str(video_key), str(exclude))).fetchone()
        return str(row[0]) if row else ""

    # ── claims ──────────────────────────────────────────────────────────
    def claims(self, video_key: str, channel: str = "", kind: str = "") -> list:
        """Claims already recorded for this reel, filtered. `[]` if none.

        Reads `value` and falls back to `name`, for the same reason `shots`
        probes: the local table was created with `name` and the canonical one
        uses `value`, so after the first real shard both columns exist and a
        query naming only one of them returns NULL for half the archive.
        """
        cols = self._columns("claim")
        if not cols:
            return []
        text = "value" if "value" in cols else ("name" if "name" in cols else "")
        pick = [c for c in ("kind", "channel", "num", "shot_idx", "confidence",
                            "t0", "t1", "observer_id") if c in cols]
        sel = ", ".join(f'"{c}"' for c in pick)
        if text:
            sel += f', "{text}" AS value'
            if text != "value" and "value" in cols:
                sel = sel.replace(f'"{text}" AS value',
                                  f'COALESCE("value", "{text}") AS value')
        sql = f'SELECT {sel} FROM claim WHERE video_key = ?'
        args = [str(video_key)]
        if channel and "channel" in cols:
            sql += ' AND "channel" = ?'
            args.append(channel)
        if kind:
            sql += ' AND "kind" = ?'
            args.append(kind)
        names = pick + (["value"] if text else [])
        return [dict(zip(names, r)) for r in self.conn.execute(sql, args)]

    # ── schema ──────────────────────────────────────────────────────────
    def _columns(self, table: str) -> set:
        try:
            return {r[1] for r in
                    self.conn.execute(f'PRAGMA table_info("{table}")')}
        except sqlite3.Error:
            return set()


# ══════════════════════════════════════════════════════════════════════════
# THE JOB
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class LocalJob(Job):
    """A `Job` that can also hand out the per-frame decode.

    `analysis()` is the addition and it is the reason four passes cost one
    decode instead of four. `shots-cpu`, `motion`, `colour` and `perframe` all
    read `lavfi.scd.mafd` and `lavfi.signalstats.*`, which `ff.analyse` produces
    in a single pass over the file; memoising it on the job means a queue that
    runs all four against one reel decodes it once. On `.check/long.mp4` — 2,280
    frames at 190× real time — that is half a second instead of two.

    The cache is per job, not per process, because a job is exactly one reel and
    holding two thousand frame dicts for reels that are finished would be a leak
    with no upper bound.
    """

    _analysis: list = field(default=None, repr=False)
    _analysis_meta: dict = field(default_factory=dict, repr=False)

    def analysis(self) -> list:
        """Every decoded frame's `scdet` and `signalstats` readings."""
        if self._analysis is None:
            rows, cuts, meta = ff.analyse(self.source)
            self._analysis = rows
            self._analysis_meta = dict(meta, cuts=cuts)
        return self._analysis

    def analysis_meta(self) -> dict:
        """What the decode itself revealed — `rc`, `decoded`, `errors`, `cuts`."""
        self.analysis()
        return self._analysis_meta

    def coverage(self) -> float:
        """Fraction of the reel's frames that actually decoded. 1.0 when whole.

        `media/proxy/loc_d640ec1bfdeed4fc.mp4` is why this exists. It has a
        corrupt bitstream, ffmpeg emits 258 error lines, decodes 132 of ~192
        frames — **and exits 0**. Every pass that reads the frame table then
        computes a perfectly well-formed mean over the first two thirds of the
        reel and presents it as the reel's. The numbers are not wrong for what
        they measured; the claim about what they measured is.

        `shots-cpu` already refuses outright past a 50% shortfall, because shot
        boundaries from a third of a file are wrong rather than partial. A mean
        brightness is not — it degrades gracefully — so the signal passes keep
        their answer and `run()` staples the coverage onto their notes, where it
        travels with the result instead of being knowable only by re-decoding.
        """
        meta = self.analysis_meta()
        if not meta:
            return 1.0
        return round(1.0 - ff.shortfall(meta.get("decoded", 0),
                                        ff.probe(self.source)), 4)


def build_job(component_id: str, video_key: str, conn: sqlite3.Connection,
              log=None, progress=None, cache=None) -> LocalJob:
    """Assemble everything one pass over one reel is allowed to see.

    Raises `SkipPass` when the reel has no file to read, which is a correct
    outcome rather than a failure: a `video_index` row whose `local_path` points
    at a video the user has since deleted is a reel this machine cannot analyse
    and will not be able to until it comes back, and marking that `failed` would
    put a permanent red row in the queue for a missing file.

    `cache` is threaded through unused by all ten current runners — every one of
    them shells out to ffmpeg and holds no weights. It is here rather than hard-
    coded to None because the queue owns exactly one `ModelCache` for the
    worker's lifetime, and the first pass that does load a model must find it
    already in hand: a cache created per job is a cache that reloads the weights
    for every reel, which is the specific failure `sizing/base.ModelCache` was
    written to prevent.
    """
    component = registry.BY_ID.get(component_id)
    if component is None:
        raise KeyError(f"no such component: {component_id!r}")

    video = _video_row(conn, video_key)
    source = _source(conn, video_key, video)
    if component_id in DECODES and not source:
        raise SkipPass("no readable copy of this reel on disk")

    workdir = os.path.join(paths.SCRATCH_DIR, "jobs", _safe(video_key))
    os.makedirs(workdir, exist_ok=True)

    return LocalJob(
        video=video, component=component, store=LocalStore(conn),
        source=source, workdir=workdir, params=dict(component.params or {}),
        resources={}, cache=cache, renew=None, progress=progress, log=log)


def _video_row(conn: sqlite3.Connection, video_key: str) -> dict:
    """The reel's row, from `video` if it exists and `video_index` otherwise.

    Both, merged, with `video` winning — because `video` is written by `probe`
    from ffprobe and `video_index` is assembled by the reader from whatever
    arrived, so where they disagree about duration the measured one is right.
    A reel with neither still gets `{"video_key": …}`, which is enough for
    `probe` to run and produce the row everything else needs.
    """
    out = {"video_key": str(video_key)}
    for table in ("video_index", "video"):
        try:
            cur = conn.execute(f'SELECT * FROM "{table}" WHERE video_key = ?',
                               (str(video_key),))
        except sqlite3.Error:
            continue                            # the table does not exist yet
        row = cur.fetchone()
        if row:
            names = [d[0] for d in cur.description]
            out.update({k: v for k, v in zip(names, row) if v is not None})
    return out


def _source(conn: sqlite3.Connection, video_key: str, video: dict) -> str:
    """The file a pass should measure. The original, not the proxy.

    `atlas.media.resolve` deliberately prefers the locally derived proxy — for
    *playback* a 480p `+faststart` copy is strictly better and that is the whole
    reason it exists. For analysis it is the wrong file: the proxy is 480p, its
    GOP was rewritten for seeking, and measuring brightness or cut boundaries on
    a re-encode means measuring the encoder as much as the reel. So this walks
    the same candidates in the opposite order and only falls back to the proxy
    when there is no original left on this machine — a fallback that is real
    (the mirror can evict an original it has a proxy for) and worth taking,
    because a 480p measurement is worth more than none.
    """
    local = str(video.get("local_path") or "")
    if local and _readable(local):
        return local

    from atlas.media import _cache_path, local_proxy                # noqa: PLC0415
    cached = _cache_path(str(video_key))
    if _readable(cached):
        return cached

    return local_proxy(str(video_key)) or ""


def _readable(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def _safe(key: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(key))


# ══════════════════════════════════════════════════════════════════════════
# RUNNING ONE PASS
# ══════════════════════════════════════════════════════════════════════════

def run(component_id: str, video_key: str, conn: sqlite3.Connection,
        log=None, progress=None, cache=None) -> dict:
    """Execute one pass over one reel. Never raises for an expected outcome.

    Returns `{"state", "reason", "tables", "notes", "seconds", "rows"}` where
    `state` is one of:

      completed   the pass ran and produced evidence
      skipped     the pass does not apply to this reel, with the reason
      deferred    not now, nothing wrong — `retry_after` seconds
      failed      it tried and could not, with the exception text

    Four states rather than two, because collapsing them is exactly the defect
    this package exists to remove. A reel with no audio track and a reel whose
    audio decoder crashed are not the same event, and a coverage matrix reading
    *4,800 done, 200 skipped* is a complete sweep while *4,800 done, 200 failed*
    is an unsolved problem. `sizing/base.py` defines the first three and this
    function is where they finally reach a database.

    `KeyboardInterrupt` and `SystemExit` are re-raised rather than caught. A
    Ctrl-C during a pass must stop the program, not be recorded as that pass
    having failed.
    """
    started = time.time()
    handler = HANDLERS.get(component_id)
    if handler is None:
        return _result("unrunnable", "no runner on this machine yet", started)

    try:
        job = build_job(component_id, video_key, conn, log=log,
                        progress=progress, cache=cache)
    except SkipPass as exc:
        return _result("skipped", str(exc), started)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:                                     # noqa: BLE001
        return _result("failed", f"{type(exc).__name__}: {exc}", started)

    em = Emission()
    try:
        extra = handler(job, em) or {}
    except SkipPass as exc:
        return _result("skipped", str(exc), started)
    except DeferPass as exc:
        out = _result("deferred", str(exc), started)
        out["retry_after"] = float(exc.retry_after)
        return out
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:                                     # noqa: BLE001
        return _result("failed", f"{type(exc).__name__}: {exc}", started)

    tables = to_rows(component_id, job.key, em, extra,
                     coverage=job.coverage() if job._analysis_meta else 1.0)  # noqa: SLF001
    out = _result("completed", "", started)
    out["tables"] = tables
    out["notes"] = _with_coverage(job, dict(em.notes or {}))
    out["rows"] = sum(len(v) for v in tables.values())
    return out


def _with_coverage(job: LocalJob, notes: dict) -> dict:
    """Staple the decode's completeness onto a pass's notes, if it decoded.

    Centrally, rather than in each pass, because this is exactly the fact a pass
    author forgets — the numbers come out clean, the mean is a real mean, and
    nothing in the result hints that a third of the reel never made it past the
    decoder. Doing it here means no runner can omit it and a runner added later
    inherits it without knowing it exists.

    Silent below 2%, because a frame or two against a container's estimated
    frame count is rounding, not damage, and a note on every reel is a note
    nobody reads.
    """
    if not job._analysis_meta:                                   # noqa: SLF001
        return notes                       # DB-only pass, or nothing decoded
    cov = job.coverage()
    if cov < 0.98:
        notes["coverage"] = (f"{cov * 100:.0f}% of the reel decoded "
                             f"({job._analysis_meta.get('errors', 0)} "   # noqa: SLF001
                             f"decoder error(s)) — measured over what was "
                             f"readable")
    return notes


def _result(state: str, reason: str, started: float) -> dict:
    return {"state": state, "reason": reason, "tables": {}, "notes": {},
            "rows": 0, "seconds": round(time.time() - started, 3)}


# ══════════════════════════════════════════════════════════════════════════
# EMISSION → SHARD ROWS
# ══════════════════════════════════════════════════════════════════════════

def to_rows(component_id: str, video_key: str, em: Emission,
            extra: dict = None, coverage: float = 1.0) -> dict:
    """Turn one pass's output into the tables a shard carries.

    Claims get their canonical sixteen-column shape here, including the two
    columns a runner cannot compute for itself:

    `observer_id` — who said it, derived below.

    `uid` — the identity of the claim, hashed from
    `(video_key, observer_id, channel, kind, shot_idx, ordinal)` exactly as
    `vios.process.store` hashes it. That is what makes a shard replayable
    without duplicating: the second import of the same evidence collides on
    `ux_claim_uid` and `INSERT OR IGNORE` drops it. Reimplementing the hash
    with a different field order would break that silently — the rows would
    import fine and simply double.

    `coverage` scales every claim's confidence, and it is how a partly-readable
    reel stops looking like a whole one. A brightness mean over the 69% of
    `loc_d640ec1bfdeed4fc` that decodes is a real measurement of real frames, so
    it is kept — but at 0.69 confidence rather than 1.0, because the claim is
    about a reel and a third of the reel was not seen. The alternative was to
    discard it or to record it as certain, and both throw away the one thing
    worth knowing. Note that this multiplies whatever the pass already set, so a
    pass that lowered its own confidence keeps that judgement.

    Shots come from `em.shots`, which carries `{t0, t1, keyframe, score}` and
    gets `video_key`, `idx` and `detector` added here, because the index is a
    property of the sequence rather than of any one shot and a runner that
    assigned it could disagree with the order it emitted them in.

    `extra` is the tables a pass returns directly — `video` from `probe`,
    `artifact` from `artifacts` and `allframes` — for rows that are not one of
    `Emission`'s shapes. Merged last and never overwritten, so a pass that
    returns both a table and an emission of the same name keeps both.
    """
    obs = observer_id(component_id)
    now = time.time()
    cov = max(0.0, min(1.0, float(coverage)))
    tables: dict = {}

    if em.claims:
        rows = []
        for c in em.claims:
            ch = str(c.get("channel") or "")
            kind = str(c.get("kind") or "")
            si = c.get("shot_idx")
            si = None if si is None else int(si)
            ordinal = int(c.get("ordinal") or 0)
            fi = c.get("frame_idx")
            fhi = c.get("frame_hi")
            val = c.get("value")
            if isinstance(val, (dict, list)):
                val = json.dumps(val, ensure_ascii=False, sort_keys=True)
            # The uid drops the frame terms entirely when there is no frame, so
            # a claim written here hashes identically to the same claim written
            # by the processing plane's v1 writer.
            parts = [video_key, obs, ch, kind, si, ordinal]
            if fi is not None:
                parts += ["f", fi, fhi]
            rows.append({
                "uid": _uid(*parts), "video_key": video_key,
                "shot_idx": si,
                # A frame claim carries its own time from the extractor's
                # manifest, which read it out of the container's presentation
                # timestamps. A shot claim carries none: its time *is* the shot,
                # and duplicating the boundary into every claim about it would
                # be a second copy to keep in agreement with the shot table.
                "t0": _num(c.get("frame_t")), "t1": _num(c.get("frame_t1")),
                "channel": ch, "kind": kind,
                "value": None if val is None else str(val),
                "num": c.get("num"),
                "confidence": round(float(c.get("confidence", 1.0)) * cov, 4),
                "observer_id": obs, "ordinal": ordinal, "created_at": now,
                "frame_idx": None if fi is None else int(fi),
                "frame_hi": None if fhi is None else int(fhi),
            })
        tables["claim"] = rows

    if em.shots:
        detector = MODELS.get(component_id, "").rsplit("/", 1)[-1] or component_id
        tables["shot"] = [
            {"video_key": video_key, "idx": i,
             "t0": round(float(s["t0"]), 3), "t1": round(float(s["t1"]), 3),
             "score": s.get("score"), "detector": detector,
             "keyframe": s.get("keyframe")}
            for i, s in enumerate(em.shots)]

    for table, rows in (extra or {}).items():
        if not rows:
            continue
        tables.setdefault(table, []).extend(rows)
    return tables


def observer_id(component_id: str) -> str:
    """Who said it: `<component>@<12 hex>` over model, revision and params.

    The same derivation `vios.process.store.observer_id_from` uses — model,
    revision and the JSON of the params, hashed, with the component id as a
    prefix rather than as part of the hash. Two components reading one model
    get two ids because the prefix differs; one component whose prompt changed
    gets a new id because the params hash does.

    `MODELS[cid]` rather than the catalogue's `model` is the one deliberate
    difference and it is the load-bearing line of this module. See the module
    docstring: upstream's `motion` is an OpenCV affine fit and this one is
    ffmpeg's frame difference, and letting them hash to one observer would mean
    the two archives' rows silently merged as though one method had produced
    them both.
    """
    c = registry.BY_ID.get(component_id)
    model = MODELS.get(component_id, "")
    if not model and c is not None:
        model = c.model
    revision = str(getattr(c, "revision", "1") if c else "1")
    params = dict(getattr(c, "params", {}) or {}) if c else {}
    blob = json.dumps(params, sort_keys=True, ensure_ascii=False)
    return f"{component_id}@{_uid(model, revision, blob)[:12]}"


def _uid(*parts) -> str:
    """blake2b-128 of the parts, unit-separated. Upstream's `store._uid`."""
    raw = "\x1f".join("" if p is None else str(p) for p in parts)
    return hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


def _num(x):
    """A float, or None. Never raises — a bad time must not lose the claim."""
    if x is None:
        return None
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return None


# ══════════════════════════════════════════════════════════════════════════
# KEYS THE FIRST SHARD MUST NOT GET TO GUESS
# ══════════════════════════════════════════════════════════════════════════

# table → the columns that identify a row in it.
_KEYS = {
    "video":    ("video_key",),
    "shot":     ("video_key", "idx"),
    "claim":    ("uid",),
    "artifact": ("video_key", "kind"),
}


def ensure_keys(conn: sqlite3.Connection) -> list:
    """Declare the evidence tables' unique keys before any shard creates them.

    `ingest._dedup_columns` *measures* a table's key from the rows in the first
    shard that carries it, and `_ensure_shard_table` builds a unique index from
    that measurement — once, at creation, permanently. That is right for a shard
    arriving from a machine this one knows nothing about, and it is a trap for
    the shards this machine writes itself, because it can be handed a sample too
    narrow to be honest.

    The specific trap: `allframes` emits exactly one `artifact` row. If it runs
    before `artifacts` on the very first reel, `video_key` alone is unique in
    that sample, the measured key is `(video_key)`, and the index built from it
    silently limits every reel in the archive to one artefact forever. Nothing
    errors — the later rows are simply IGNOREd.

    So the four tables whose keys are actually known are declared here rather
    than inferred. Creating them empty is enough: `_ensure_shard_table` widens
    an existing table instead of creating one, so the columns still arrive from
    the shard's own header and only the index is pinned. Returns the tables it
    created, which is `[]` on every run after the first.
    """
    made = []
    for table, keys in _KEYS.items():
        row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                           "AND name=?", (table,)).fetchone()
        if row:
            continue
        cols = ", ".join(f'"{k}" {"INTEGER" if k == "idx" else "TEXT"}'
                         for k in keys)
        conn.execute(f'CREATE TABLE "{table}" ({cols})')
        idx = ("ux_" + table + "_" + "_".join(keys))[:60]
        conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{idx}" ON '
                     f'"{table}" ({", ".join(chr(34) + k + chr(34) for k in keys)})')
        made.append(table)
    if made:
        conn.commit()
    return made


__all__ = ["HANDLERS", "MODELS", "REQUIRES", "DECODES", "LOCAL_NEEDS",
           "SHOTS_CPU", "LocalJob", "LocalStore", "blocked", "build_job",
           "ensure_keys", "ff", "have", "install", "observer_id", "order",
           "run", "signal", "structure", "to_rows"]
