"""
runners/structure.py — the passes that establish what the file *is*.

Four passes, none of which needs a model, a GPU, or a network: `probe` measures
the container, `artifacts` derives the playback set, `allframes` writes the frame
strip everything downstream reads, and `shots-cpu` finds the cuts.

**Why these four are the ones that exist first.** Nothing else can be trusted
without them. A claim at `t=7.3` means nothing until something has established
that the file is 95 seconds long and not 9.5; a shot-scoped claim needs a shot
table to scope to; and the reason a reel plays instantly in this app is that
`artifacts` ran, not that anything was clever at click time. They are also the
four this laptop can do *completely* — there is no sampling, no "first 200
frames", no model whose weights might not be resident. `long.mp4` decodes at
190× real time, so total coverage of a 95-second reel costs half a second and
the honest answer and the cheap answer are the same answer.

**`shots-cpu` is a new component, not a re-pointing of `shots`.** `shots`
declares TransNetV2 on a GPU and that declaration is true of the Kaggle side;
quietly making it mean `scdet` here would put the string "shots ran" in two
databases with two different meanings behind it. So the CPU detector has its own
id, its own row in the catalogue (registered from `runners.SHOTS_CPU` rather than
added to the shared list), and writes `detector='scdet'` into every shot it
produces — and it refuses to run at all over shots some *other* detector already
found. That last part is not politeness. `atlas.ingest._enrich` fills NULL
columns and **never overwrites**, so if this pass wrote boundaries first and
Kaggle later published TransNetV2's, the better answer would be silently
discarded and the guess would be permanent.
"""

from __future__ import annotations

import json
import os
import time

import derive
from sizing.base import Emission, SkipPass

from . import ff

# A shot shorter than this is a flash, a flicker, or a compression artefact
# rather than an edit. 0.25 s at 30 fps is 8 frames, which is the value upstream
# uses (`min_shot_frames`) and is short enough to keep the fast cuts that make a
# reel a reel — a 4-cuts-per-second montage survives this.
MIN_SHOT = 0.25

# Frames per second for the frame strip. 2 is enough to catch a caption that
# holds for half a second and cheap enough that a 95-second reel is 190 JPEGs.
# Not the source frame rate: 2,280 frames of a 95-second reel would be ~180 MB
# of JPEG to answer questions that 190 frames answer identically.
FRAME_FPS = 2.0
FRAME_WIDTH = 512

# How much of a reel may fail to decode before its shot boundaries are refused.
#
# Deliberately far stricter than the tolerance the measurement passes get, and
# the asymmetry is the point. A mean brightness over 69% of a reel degrades
# gracefully — it is a real measurement of real frames, and `runners.to_rows`
# records it at 0.69 confidence. Shot boundaries do not degrade: every "when" in
# this database is a shot index, so a cut hidden in the third of the file that
# never decoded silently merges two shots into one, and every claim scoped to
# that shot is then about two different images with nothing anywhere saying so.
#
# 10% is one shot's worth of a fast-cut reel. Below that the tail is short enough
# that a missed cut would have to fall inside a single shot's span; above it, the
# honest answer is that this machine does not know where the shots are, and a
# repaired file can be re-queued.
SPINE_SHORTFALL = 0.10


# ══════════════════════════════════════════════════════════════════════════
# probe — the container, measured
# ══════════════════════════════════════════════════════════════════════════

def probe(job, em: Emission) -> dict:
    """Measure the file and write the `video` row. Returns extra tables.

    This is the pass with no claims, and it is the one everything else depends
    on. `atlas.index` reflects over whatever tables exist and absorbs `video`
    into `video_index` — so a row written here is what gives a reel its duration
    in the Library, its aspect in the player, and its `has_audio` flag, which is
    the flag `loudness` reads to decide whether to skip.

    `derive.probe` is the only prober in this program, deliberately. It already
    resolves rotation — a portrait reel from a phone reports 1920×1080 with a 90°
    display matrix and must be read as 1080×1920 — and already normalises `fps`
    out of `r_frame_rate`. A second implementation here would be a second answer
    to "how tall is this video", which is the class of divergence that makes a
    sprite sheet not line up with the player it was built for.

    The decode is *not* re-run to check integrity. `ffprobe` reads the container
    and the first packets; whether every frame decodes is a different question
    and a more expensive one, and `shots-cpu` and `motion` both answer it as a
    side effect of work they were doing anyway. Recording a shortfall here would
    mean a second full decode for a number two later passes report for free.
    """
    facts = ff.probe(job.source)
    if not facts.get("duration"):
        raise RuntimeError(
            f"ffprobe read no duration from {os.path.basename(job.source)} — "
            "the file is truncated, empty, or not a video")

    meta = {k: facts.get(k) for k in ("vcodec", "acodec") if facts.get(k)}
    try:
        meta["faststart"] = bool(derive.faststart_ok(job.source))
    except Exception:                                            # noqa: BLE001
        pass                       # a nicety for the player, never a failure

    row = {
        "video_key": job.key,
        "duration": round(float(facts["duration"]), 3),
        "width": int(facts.get("width") or 0),
        "height": int(facts.get("height") or 0),
        "fps": round(float(facts.get("fps") or 0.0), 4),
        "has_audio": 1 if facts.get("has_audio") else 0,
        "bytes": int(facts.get("bytes") or 0),
        "added_at": time.time(),
        "meta": json.dumps(meta, sort_keys=True),
    }
    em.notes = {"duration": row["duration"],
                "size": f'{row["width"]}x{row["height"]}',
                "fps": row["fps"], "audio": bool(row["has_audio"])}
    return {"video": [row]}


# ══════════════════════════════════════════════════════════════════════════
# artifacts — the playback set
# ══════════════════════════════════════════════════════════════════════════

# (artifact kind, the flag `derive.have()` reports it under). The two names are
# not always the same word — `have()` says `posters` because there are three
# tiers of them, while the artefact row names the one kind of thing they are.
_KINDS = (("proxy", "proxy"), ("poster", "posters"), ("sprite", "sprite"),
          ("keyframes", "keyframes"))


def artifacts(job, em: Emission) -> dict:
    """Derive proxy, posters, sprite sheet and keyframe index. Emits `artifact`.

    All of the work is `derive.derive`, which was written before this package
    existed and is already what the mirror worker calls — this pass is the queue
    being able to ask for the same thing per reel instead of waiting for the
    mirror's sweep to reach it. It is idempotent: `derive` checks `have()` first
    and re-derives nothing, so queueing this against a reel that already plays
    costs four `stat` calls.

    **The `artifact` table gains a `path` column here, and that is a real
    divergence worth naming.** Upstream's schema is
    `(video_key, kind, msg_id, file_id, bytes, meta, created_at)` — no path,
    because on Kaggle a derived artefact's only durable home is a Telegram
    message and `file_id` is how you get it back. On this laptop the artefact is
    a file on a disk that is never wiped, so the path is the whole point and
    `msg_id`/`file_id` are the columns that are always NULL. The shard header is
    self-describing and `_ensure_shard_table` adds unseen columns, so this is
    exactly the additive drift the wire format was built to absorb — but it is
    additive drift *originating on this side*, which had not happened before, so
    WIRE.md records it.
    """
    if not derive.FFMPEG:
        raise RuntimeError("ffmpeg is not on PATH — nothing can be derived")

    res = derive.derive(job.key, job.source)
    have = derive.have(job.key)

    rows, made = [], []
    for kind, flag in _KINDS:
        if not have.get(flag):
            continue
        path, meta = _artifact_path(job.key, kind)
        rows.append({"video_key": job.key, "kind": kind, "path": path,
                     "bytes": _size(path), "meta": json.dumps(meta,
                                                              sort_keys=True),
                     "created_at": time.time()})
        made.append(kind)
        em.artifact(kind, path, meta)

    if not rows:
        raise RuntimeError(
            "derive produced nothing — " +
            ", ".join(f"{k}={v}" for k, v in sorted(have.items())) +
            (f"; {res['note']}" if res.get("note") else ""))

    em.notes = {"derived": ", ".join(made), "complete": derive.complete(job.key)}
    if res.get("note"):
        em.notes["note"] = res["note"]
    return {"artifact": rows}


def _artifact_path(key: str, kind: str) -> tuple:
    """Where this artefact lives, and what is worth recording about it.

    A directory for `keyframes` rather than a file, and the count instead of a
    byte size, because "the keyframes" is a set — a caller that wants one asks
    the index for the frame nearest a time, and a caller that wants to know
    whether the pass ran wants to know how many there are.
    """
    if kind == "proxy":
        from atlas.media import local_proxy_path                 # noqa: PLC0415
        return local_proxy_path(key), {}
    if kind == "poster":
        # Three tiers are derived; the 360 is the one every card loads.
        return derive.poster_path(key, 360), {
            "tiers": [t for t in (180, 360, 720)
                      if _size(derive.poster_path(key, t))]}
    if kind == "sprite":
        meta = {}
        try:
            with open(derive.sprite_meta_path(key), encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            pass
        return derive.sprite_path(key), meta
    d = derive.keyframe_dir(key)
    try:
        n = sum(1 for f in os.listdir(d) if f.lower().endswith(".jpg"))
    except OSError:
        n = 0
    return d, {"count": n}


def _size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ══════════════════════════════════════════════════════════════════════════
# allframes — the frame strip every vision pass reads
# ══════════════════════════════════════════════════════════════════════════

def allframes(job, em: Emission) -> dict:
    """Write a 2 fps JPEG strip with a manifest, and emit it as an artefact.

    Every declared vision pass — `ocr`, `detect`, `faces`, `visual-embed`,
    `depth` — declares `needs=('allframes',)`, and none of them can run on this
    laptop today. Writing the strip anyway is not speculative work: it is the
    expensive half, it is the half that does not need a GPU, and it means the day
    a model *is* installed the pass reads JPEGs off a local disk instead of
    re-decoding every reel in the archive.

    The manifest pairs index to time explicitly rather than letting a reader
    recompute `i / fps`. They agree today — `fps=2` forces exactly that cadence
    by duplicating and dropping source frames — but a reader that recomputes is
    a reader that will be wrong the first time this is called with
    `fps='source'`, and the failure would be silent claims at wrong timestamps.
    """
    # Under `derive.keyframe_dir` rather than beside it, and via that function
    # rather than a local sanitiser, so the strip and the shot keyframes agree
    # on how a key becomes a directory name. Two spellings of `safe_name` would
    # put one reel's frames in two directories with no error anywhere.
    dest = os.path.join(derive.keyframe_dir(job.key), "strip")
    fps = float(job.params.get("frame_fps", FRAME_FPS))
    width = int(job.params.get("frame_width", FRAME_WIDTH))

    paths_, times, meta = ff.extract_frames(job.source, dest, fps, width)
    if not paths_:
        raise RuntimeError("ffmpeg wrote no frames — " + meta["stderr"][:200])

    manifest = os.path.join(dest, "index.json")
    body = {"video_key": job.key, "fps": fps, "width": width,
            "count": len(paths_), "at": time.time(),
            "frames": [{"idx": i, "t": t, "file": os.path.basename(p)}
                       for i, (t, p) in enumerate(zip(times, paths_))]}
    _write_json(manifest, body)

    art = {"fps": fps, "width": width, "count": len(paths_),
           "manifest": manifest, "span": [times[0], times[-1]]}
    em.artifact("allframes", dest, art)
    em.notes = {"frames": len(paths_), "fps": fps,
                "span": f"{times[0]:.2f}–{times[-1]:.2f}s"}
    return {"artifact": [{"video_key": job.key, "kind": "allframes",
                          "path": dest, "bytes": sum(_size(p) for p in paths_),
                          "meta": json.dumps(art, sort_keys=True),
                          "created_at": time.time()}]}


def _write_json(path: str, body: dict) -> None:
    """Write via a temp file and `os.replace`, matching `derive._write_json`.

    A manifest half-written by a process that died is worse than no manifest: it
    parses as far as the tear and then reports a frame count that does not match
    the directory, so a reader trusts a strip that is missing its tail.
    """
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, separators=(",", ":"))
    os.replace(tmp, path)


# ══════════════════════════════════════════════════════════════════════════
# shots-cpu — cuts, from mean absolute frame difference
# ══════════════════════════════════════════════════════════════════════════

def shots_cpu(job, em: Emission) -> dict:
    """Find shot boundaries with `scdet` and write the `shot` table.

    `scdet` compares each frame to the one before it and reports a scene change
    when the mean absolute difference crosses a percentage threshold. That is a
    weaker detector than TransNetV2 — it cannot tell a hard cut from a fast whip
    pan, and it misses a dissolve entirely — and it is the honest ceiling of what
    this machine can do with no GPU. Every row it writes carries
    `detector='scdet'` so that ceiling is recorded in the data rather than
    remembered.

    **Refuses to run over another detector's shots.** `atlas.ingest._enrich`
    fills NULLs and never overwrites, so a `shot` row this pass wrote would
    survive a later import of TransNetV2's better boundaries for the same
    `(video_key, idx)`. Skipping is therefore not deference, it is the only way
    the better answer can ever land. Re-running over its *own* previous output is
    fine and is what a re-queue means.

    A reel with no detected cut gets one shot spanning the whole file, which is
    the correct answer for a 95-second continuous take and is what
    `.check/long.mp4` measures as. Zero shots would be a different claim — that
    nothing was looked at — and would make every downstream pass skip.
    """
    facts = ff.probe(job.source)
    duration = float(facts.get("duration") or 0.0)
    if duration <= 0.0:
        raise RuntimeError("no duration — run probe first")

    prior = _foreign_detector(job)
    if prior:
        raise SkipPass(f"{prior} already found this reel's shots — a CPU "
                       f"detector must not overwrite a better one")

    rows, cuts, meta = ff.analyse(job.source)
    if not rows:
        raise RuntimeError("no frame decoded — " + meta["stderr"][:200])

    short = ff.shortfall(meta["decoded"], facts)
    if short > SPINE_SHORTFALL:
        raise RuntimeError(
            f"only {meta['decoded']} of ~{int(duration * (facts.get('fps') or 0))} "
            f"frame(s) decoded, {short * 100:.0f}% of the reel missing "
            f"({meta['errors']} decoder error(s)) — a cut hidden in the part "
            f"that would not decode merges two shots into one, and every claim "
            f"scoped to that shot would be about two different images. Repair "
            f"or re-download the file and re-queue")

    edges = _merge_cuts([t for t, _ in cuts], duration,
                        float(job.params.get("min_shot", MIN_SHOT)))
    score = {round(t, 3): s for t, s in cuts}

    shots = [{"t0": edges[i], "t1": edges[i + 1],
              "keyframe": round((edges[i] + edges[i + 1]) / 2.0, 3),
              "score": score.get(edges[i])}
             for i in range(len(edges) - 1)]
    em.shots = shots
    em.notes = {"shots": len(shots), "detector": "scdet",
                "cuts_found": len(cuts),
                "asl": round(duration / max(len(shots), 1), 3),
                "threshold": ff.SCD_THRESHOLD}
    if short > 0.02:
        em.notes["decoded"] = (f"{meta['decoded']} frame(s), "
                               f"{short * 100:.0f}% short")

    # No table returned. `runners.to_rows` builds the `shot` rows from
    # `em.shots` — it adds `video_key`, the index, and the detector name derived
    # from `MODELS`, because an index is a property of the sequence rather than
    # of any one shot. Returning the rows here as well would write every shot
    # into the shard twice; the unique index would dedupe them on import, so the
    # only visible symptom would be a row count that is quietly double.
    return {}


def _foreign_detector(job) -> str:
    """Which *other* detector already wrote shots for this reel, if any."""
    try:
        return job.store.detectors(job.key, exclude="scdet")
    except Exception:                                            # noqa: BLE001
        return ""


def _merge_cuts(cuts, duration: float, min_shot: float) -> list:
    """Cut times → shot edges, with flashes folded into their neighbour.

    Always starts at 0 and ends at `duration`, so the shots tile the file with
    no gap: a claim at any timestamp in the reel falls inside exactly one shot.
    A boundary closer than `min_shot` to the previous one is dropped rather than
    kept as a shot of its own — an 8-frame "shot" is a flash frame or a
    compression artefact, and letting it through would halve the average shot
    length of any reel with a strobe in it.

    The trailing check mirrors that at the other end: if the last real cut lands
    within `min_shot` of the file's end, the tail is merged backwards instead of
    becoming a 3-frame final shot.
    """
    out = [0.0]
    for c in sorted(cuts):
        c = round(float(c), 3)
        if c <= 0.0 or c >= duration:
            continue
        if c - out[-1] >= min_shot:
            out.append(c)
    if len(out) > 1 and duration - out[-1] < min_shot:
        out.pop()
    out.append(round(duration, 3))
    return out
