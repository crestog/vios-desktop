"""
derive — the ffmpeg pass that turns a video into something that plays instantly.

Every latency number in the plan except search is won or lost here, once per
video, on a background thread, and never again at play time:

    faststart     the `moov` atom moved to the *front* of the file. Instagram's
                  own mp4s routinely put it at the end, which forces a reader to
                  fetch the whole file before it can show frame one. This is the
                  single biggest playback win available and it costs one extra
                  pass over the output. `faststart_ok()` below verifies it
                  actually happened, because getting it wrong is invisible.
    GOP ≈ 1 s     a keyframe about every second, so a seek lands on a nearby
                  keyframe instead of decoding forward from a distant one. *The*
                  seek-latency lever.
    sprite sheet  ~100 frames in one JPEG grid. Scrub preview then moves a CSS
                  `background-position` — zero decode, zero requests — which is
                  the only way a 16 ms budget is reachable at all.
    poster tiers  three sizes, so the density slider changes *what is fetched*
                  rather than just how it is stretched. A 12-column grid pulling
                  720 px posters is how a grid drops frames.
    keyframes     scene-change frames at source resolution, with timestamps, for
                  local model passes.

Two passes, not one
───────────────────
The plan said one ffmpeg pass per file. It is two, and the reason is worth the
paragraph:

  * **Pass one reads the source** and produces the proxy *and* the keyframes.
    Both need source-quality pixels — the keyframes because a local OCR pass on a
    720 px CRF-23 transcode is being asked to read text that is no longer there,
    which is exactly the "don't force a model to do what it can't" failure. They
    share the expensive decode via `split`.
  * **Pass two reads the proxy** and produces the sprite sheet and the posters.
    Those are thumbnails; 720 px is more than they need. Decoding a small
    short-GOP H.264 file is several times cheaper than decoding the 1080×1920
    source again, so this is *faster* than folding it into pass one, not slower.

It also splits the failure modes usefully. A failed pass one means nothing plays.
A failed pass two means it plays without a scrub preview — worth knowing apart.

Nothing here is stateful
────────────────────────
`have()` answers "is this done" from the filesystem alone. There is no derivation
table, no in-progress flag to get stuck, and a killed process leaves nothing that
looks finished: every artefact is written to a `.part` and renamed, and the
keyframe directory is only considered complete once its `index.json` exists.
Re-running `derive()` on a finished video is a few `stat` calls. The ledger of
what still needs doing belongs to `mirror.py`, which has to survive restarts;
this module only has to be honest about the present.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time

import paths
from atlas import config
from atlas.media import local_proxy_path as proxy_path, safe_name
from atlas.subproc import BACKGROUND
from logger import vios_log as log

SUB = "MEDIA"

FFMPEG = shutil.which("ffmpeg") or ""
FFPROBE = shutil.which("ffprobe") or ""


class DeriveError(RuntimeError):
    """A derivation that cannot be retried into working — a bad input file."""


# ── Politeness ────────────────────────────────────────────────────────────
# Two at a time, four threads each, at below-normal priority. The numbers come
# off this machine: 10 cores / 16 threads, and the thing being protected is not
# throughput, it is the window. A mirror that transcodes the archive twice as
# fast while the scroll stutters has optimised the wrong number — the archive is
# a week of downloads either way, and nobody watches a progress bar for a week.
#
# Below-normal priority is the load-bearing half. Windows will let a NORMAL
# foreground process preempt these entirely, so the UI thread keeps its
# timeslice even with both encoders running flat out.
_SLOTS = threading.Semaphore(int(os.environ.get("VIOS_DERIVE_JOBS", "2")))
_THREADS = os.environ.get("VIOS_DERIVE_THREADS", "4")

_KEY_LOCKS: dict = {}
_KEY_LOCKS_MUTEX = threading.Lock()


def _get_key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_MUTEX:
        lock = _KEY_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[key] = lock
        return lock


# Kept under this name because `runners/ff.py` imports it from here, and because
# it was the only correct copy of this in the tree — every other spawn site was
# missing it, which is what put console windows on screen during a search.
# Defined in `atlas/subproc.py` now so there is one of it. See that module for
# why the priority is a second flag rather than part of the first.
_CREATIONFLAGS = BACKGROUND


# ── Where things land ─────────────────────────────────────────────────────
# All of it under paths.HOME and addressed by the video's key, never beside the
# original. `library.py` depends on that: a watched folder is read and never
# written, so a user's own video directory must come out of a derivation pass
# byte-identical to how it went in.
#
# `proxy_path` is imported rather than defined — it is `media.local_proxy_path`,
# because `resolve()` has to find the file this module writes and one of them
# knowing a different filename than the other is a bug with no symptom except
# "playback is slow again".


def sprite_path(key: str) -> str:
    return os.path.join(paths.SPRITE_DIR, f"{safe_name(key)}.jpg")


def sprite_meta_path(key: str) -> str:
    return os.path.join(paths.SPRITE_DIR, f"{safe_name(key)}.json")


def poster_path(key: str, tier: int) -> str:
    return os.path.join(paths.POSTER_DIR, f"{safe_name(key)}.t{int(tier)}.jpg")


def keyframe_dir(key: str) -> str:
    return os.path.join(paths.KEYFRAME_DIR, safe_name(key))


def keyframe_index_path(key: str) -> str:
    return os.path.join(keyframe_dir(key), "index.json")


def _nonempty(path: str) -> bool:
    try:
        return os.path.getsize(path) > 0
    except OSError:
        return False


def have(key: str) -> dict:
    """Which artefacts exist for this key, right now, from the disk.

    The keyframe answer is deliberately the *index*, not the directory: a
    directory of JPEGs from a run that was killed at frame 30 of 60 looks
    finished and is not. The index is written last, so its presence is the only
    honest completion signal.
    """
    return {
        "proxy":     _nonempty(proxy_path(key)),
        "sprite":    _nonempty(sprite_path(key)) and _nonempty(sprite_meta_path(key)),
        "posters":   all(_nonempty(poster_path(key, t)) for t in config.POSTER_TIERS),
        "keyframes": _nonempty(keyframe_index_path(key)),
    }


def complete(key: str) -> bool:
    return all(have(key).values())


# ── Probing ───────────────────────────────────────────────────────────────
def probe(path: str) -> dict:
    """What ffprobe knows about a file. `{}` when it cannot read it at all.

    Duration is taken from the container first and the video stream second,
    because a stream-level `duration` is missing often enough on phone-written
    mp4s to matter and a container-level one is missing on fragmented ones. Both
    absent means the sprite sheet cannot be laid out, and the caller has to know
    that rather than divide by zero.
    """
    if not FFPROBE:
        return {}
    cmd = [FFPROBE, "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=60,
                           creationflags=_CREATIONFLAGS)
    except (subprocess.SubprocessError, OSError) as e:
        log(f"ffprobe failed on {os.path.basename(path)} — "
            f"{type(e).__name__}: {e}", SUB, "WARN")
        return {}
    if r.returncode != 0:
        return {}
    try:
        raw = json.loads(r.stdout.decode("utf-8", "replace"))
    except (ValueError, AttributeError):
        return {}

    streams = raw.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fmt = raw.get("format") or {}

    def num(val, default=0.0) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    duration = num(fmt.get("duration")) or num(video.get("duration"))

    # r_frame_rate is a rational string, "30000/1001".
    fps = 0.0
    rate = str(video.get("r_frame_rate") or video.get("avg_frame_rate") or "")
    if "/" in rate:
        top, _, bot = rate.partition("/")
        fps = num(top) / (num(bot) or 1.0)

    width = int(num(video.get("width")))
    height = int(num(video.get("height")))
    # A display-matrix rotation means the stored dimensions are not the ones the
    # player will show. ffmpeg autorotates through the filter graph, so the proxy
    # comes out upright — but a caller reading these numbers to lay out a 9:16
    # grid would place a portrait reel in a landscape cell without this swap.
    if _rotated(video):
        width, height = height, width

    return {
        "path": path,
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": audio is not None,
        "vcodec": str(video.get("codec_name") or ""),
        "acodec": str((audio or {}).get("codec_name") or ""),
        "bytes": int(num(fmt.get("size"))),
    }


def _rotated(video: dict) -> bool:
    """True when the display matrix turns this stream on its side."""
    for side in (video.get("side_data_list") or []):
        try:
            deg = abs(int(float(side.get("rotation", 0)))) % 180
        except (TypeError, ValueError):
            continue
        if deg == 90:
            return True
    tags = video.get("tags") or {}
    try:
        return abs(int(float(tags.get("rotate", 0)))) % 180 == 90
    except (TypeError, ValueError):
        return False


# ── Pass one: source → proxy + keyframes ──────────────────────────────────
_KEYFRAME_CAP = int(os.environ.get("VIOS_KEYFRAME_CAP", "60"))
_SCENE_THRESHOLD = os.environ.get("VIOS_SCENE_THRESHOLD", "0.28")
_KEYFRAME_WIDTH = int(os.environ.get("VIOS_KEYFRAME_WIDTH", "1080"))
_STAMPS = "stamps.txt"


def _run(cmd: list, cwd: str, timeout: float, what: str) -> None:
    """Run ffmpeg, or raise with the tail of what it said.

    ffmpeg's diagnostics are on stderr and the useful line is almost always the
    last one, so the exception carries the tail rather than the whole log — a
    filter-graph error is one line and a 4 MB dump of frame statistics around it
    is what makes it unreadable in a log view.
    """
    try:
        r = subprocess.run(cmd, cwd=cwd or None, capture_output=True,
                           timeout=timeout, creationflags=_CREATIONFLAGS)
    except subprocess.TimeoutExpired:
        raise DeriveError(f"{what} timed out after {timeout:.0f}s") from None
    except OSError as e:
        raise DeriveError(f"{what} could not start — {e}") from e
    if r.returncode != 0:
        tail = (r.stderr or b"").decode("utf-8", "replace").strip()
        tail = " / ".join(tail.splitlines()[-3:]) or f"exit {r.returncode}"
        raise DeriveError(f"{what} failed — {tail}")


def _pass_one(key: str, src: str, facts: dict) -> list:
    """Proxy and keyframes, from one decode of the source. Returns timestamps.

    The filter graph splits the decoded video in two. One branch is scaled and
    encoded; the other is thinned to scene changes and written as JPEGs. Two
    details in it are not obvious:

      * `scale=w=min(iw\\,N)` rather than `scale=N`. Plain `scale` would happily
        *upscale* a 720 p source to 1080 for the keyframes, which invents pixels,
        triples the JPEG and helps no model.
      * `metadata=mode=print:file=stamps.txt` with a **relative** filename, and
        the process `cwd` set to the keyframe directory. Filter options are
        colon-separated, so an absolute Windows path inside a filter argument
        (`C:\\Users\\…`) is parsed as options — the escaping needed to survive
        that is a known source of silent breakage, and not needing it is better
        than getting it right.

    A frame's timestamp is the whole value of a keyframe. Without it a frame
    cannot carry a claim's span, so the JPEGs and the stamps are written by the
    same filter chain and can never be off by one.
    """
    out_dir = keyframe_dir(key)
    os.makedirs(out_dir, exist_ok=True)
    stamps = os.path.join(out_dir, _STAMPS)
    for stale in (stamps,):
        try:
            os.remove(stale)
        except OSError:
            pass
    # A previous run that died mid-way left JPEGs with no index. They are about
    # to be regenerated, and leaving them would let a shorter run inherit the
    # tail of a longer one.
    for name in os.listdir(out_dir):
        if re.fullmatch(r"\d{4}\.jpg", name):
            try:
                os.remove(os.path.join(out_dir, name))
            except OSError:
                pass

    dest = proxy_path(key)
    tmp = f"{dest}.{os.getpid()}_{threading.get_ident()}.part"
    fps = facts.get("fps") or 30.0
    gop = max(12, int(round(fps * config.PROXY_GOP_SECS)))

    graph = (
        f"[0:v]split=2[vp][vk];"
        f"[vp]scale=w={config.PROXY_WIDTH}:h=-2:flags=bicubic[proxy];"
        f"[vk]select='eq(n\\,0)+gt(scene\\,{_SCENE_THRESHOLD})',"
        f"metadata=mode=print:file={_STAMPS},"
        f"scale=w='min(iw\\,{_KEYFRAME_WIDTH})':h=-2[kf]"
    )
    cmd = [
        FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-i", src,
        "-filter_complex", graph,
        # ── the proxy ──
        "-map", "[proxy]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", config.PROXY_PRESET,
        "-crf", str(config.PROXY_CRF),
        "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.0",
        # -g bounds the GOP in frames; -force_key_frames pins it in *seconds*,
        # which is what actually matters and what survives a source whose
        # reported frame rate is wrong. Both, because they fail differently.
        "-g", str(gop), "-keyint_min", str(max(2, gop // 2)),
        "-force_key_frames", f"expr:gte(t,n_forced*{config.PROXY_GOP_SECS})",
        "-c:a", "aac", "-b:a", "128k", "-ac", "2",
        "-movflags", "+faststart",
        "-threads", _THREADS,
        # Explicit, because the destination is `<name>.mp4.part` and ffmpeg picks
        # its muxer from the extension — `.part` is not one, and the failure is
        # `Error initializing the muxer: Invalid argument`, which reads like a bad
        # flag rather than a filename. Writing to a temp and renaming is
        # non-negotiable (a killed transcode must not leave something `resolve()`
        # would serve), so the format has to be stated.
        "-f", "mp4",
        tmp,
        # ── the keyframes ──
        "-map", "[kf]", "-fps_mode", "passthrough",
        "-q:v", "2", "-frames:v", str(_KEYFRAME_CAP),
        "%04d.jpg",
    ]

    duration = float(facts.get("duration") or 0.0)
    timeout = max(180.0, duration * 12.0)
    _run(cmd, out_dir, timeout, f"proxy+keyframes for {key}")

    if not _nonempty(tmp):
        raise DeriveError(f"proxy for {key} came out empty")
    if not faststart_ok(tmp):
        # Not fatal — the file plays. But it plays *slowly*, and the whole reason
        # this pass exists is the flag that just did not take, so it must not
        # pass silently.
        log(f"{key}: proxy has no leading moov atom — faststart did not apply, "
            f"playback will need a full read before frame one", SUB, "WARN")
    os.replace(tmp, dest)

    return _read_stamps(stamps)


def _read_stamps(path: str) -> list:
    """Timestamps out of the metadata filter's print output.

    Format is one `frame:N pts:… pts_time:S` line per frame followed by its
    metadata keys, so only the `frame:` lines are of interest.
    """
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.startswith("frame:"):
                    continue
                hit = re.search(r"pts_time:\s*([0-9.]+)", line)
                if hit:
                    try:
                        out.append(round(float(hit.group(1)), 3))
                    except ValueError:
                        continue
    except OSError:
        return []
    return out


def _write_keyframe_index(key: str, stamps: list, facts: dict) -> int:
    """Pair the JPEGs on disk with their timestamps and seal the directory.

    Written last, and it is what `have()` reads, so a directory without it is a
    directory nobody will trust. `zip` truncates to the shorter of the two
    deliberately: `-frames:v` caps the JPEGs while the metadata filter keeps
    printing, so there are routinely more stamps than files and the files are
    the authority.
    """
    out_dir = keyframe_dir(key)
    try:
        files = sorted(n for n in os.listdir(out_dir)
                       if re.fullmatch(r"\d{4}\.jpg", n))
    except OSError:
        files = []

    frames = [{"i": i + 1, "file": name, "t": stamps[i] if i < len(stamps) else None}
              for i, name in enumerate(files)]
    body = {
        "key": key,
        "count": len(frames),
        "width": int(facts.get("width") or 0),
        "height": int(facts.get("height") or 0),
        "duration": round(float(facts.get("duration") or 0.0), 3),
        "scene_threshold": float(_SCENE_THRESHOLD),
        "capped": len(files) >= _KEYFRAME_CAP,
        "frames": frames,
    }
    _write_json(keyframe_index_path(key), body)
    try:
        os.remove(os.path.join(out_dir, _STAMPS))
    except OSError:
        pass
    return len(frames)


# ── Pass two: proxy → sprite sheet + poster tiers ─────────────────────────
_MIN_CELL_SECS = float(os.environ.get("VIOS_SPRITE_MIN_CELL", "0.2"))


def _pass_two(key: str, facts: dict, stamps: list) -> dict:
    """One decode of the proxy, four images out.

    The poster is not frame zero. A reel's first frame is very often black, or a
    title card that is identical across a creator's whole output, which turns a
    grid of results into a grid of the same rectangle. Pass one already found the
    scene changes, so the cover frame is the first of those at least half a
    second in — a real frame from a real shot, for free.
    """
    src = proxy_path(key)
    duration = float(facts.get("duration") or 0.0)
    if not _nonempty(src):
        raise DeriveError(f"no proxy to derive thumbnails from for {key}")
    if duration <= 0.0:
        raise DeriveError(f"{key} has no readable duration — cannot lay out a "
                          f"sprite sheet")

    cols = max(1, config.SPRITE_COLUMNS)
    # The grid in config is a *ceiling*, not a target. A 6 s clip spread over 100
    # cells is 64 ms per cell — finer than a mouse can be aimed, and it cost
    # 474 KB measured, against 190 KB for the 40 cells that are actually useful.
    # So the cell duration has a floor and the row count follows from it.
    #
    # The other end is not solved here and should not be pretended away: a
    # ten-minute local-library video gets 100 cells of six seconds each, which is
    # coarse. The honest fix is a second, denser sheet per minute, and it is worth
    # doing when there are long videos to test it against — until then `interval`
    # is recorded truthfully so the UI can label the preview rather than imply a
    # precision it does not have.
    want = cols * max(1, config.SPRITE_ROWS)
    want = min(want, max(cols, int(duration / _MIN_CELL_SECS)))
    rows = max(1, -(-want // cols))                 # ceil, so the grid is full
    count = cols * rows
    interval = duration / count
    pos = _cover_time(stamps, duration)

    tiles = [(t, f"{poster_path(key, t)}.{os.getpid()}_{threading.get_ident()}.part")
             for t in config.POSTER_TIERS]
    sheet_tmp = f"{sprite_path(key)}.{os.getpid()}_{threading.get_ident()}.part"

    branches = ["[0:v]split=%d%s" % (1 + len(tiles),
                                     "".join(f"[b{i}]" for i in range(1 + len(tiles))))]
    # Branch 0 is the sheet. `fps` resamples to exactly count/duration frames per
    # second, so `tile` receives about `count` frames over the whole video and
    # emits one image. "About": the filter rounds, so the final cell can be a
    # padded duplicate. That is why the sidecar records `count` and the UI clamps
    # to it — an off-by-one on the last cell of a scrub is not worth an exact
    # frame-selection expression that would be twice as easy to get wrong.
    branches.append(f"[b0]fps={count}/{duration:.6f},"
                    f"scale=w={config.SPRITE_TILE_W}:h=-2,"
                    f"tile={cols}x{rows}[sheet]")
    for i, (tier, _) in enumerate(tiles, start=1):
        branches.append(f"[b{i}]select='gte(t\\,{pos:.3f})',"
                        f"scale=w={tier}:h=-2[p{i}]")

    # `-f image2 -update 1` for the same reason the proxy names its muxer: the
    # destinations end in `.jpg.part`. `-update 1` tells image2 the filename is a
    # literal rather than a pattern, which is what makes a single-image output
    # legal at all.
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
           "-i", src, "-filter_complex", ";".join(branches),
           "-map", "[sheet]", "-frames:v", "1", "-q:v", "5",
           "-f", "image2", "-update", "1", sheet_tmp]
    for i, (tier, tmp) in enumerate(tiles, start=1):
        cmd += ["-map", f"[p{i}]", "-frames:v", "1",
                "-q:v", "3" if tier >= 720 else "4",
                "-f", "image2", "-update", "1", tmp]

    _run(cmd, paths.SCRATCH_DIR, max(120.0, duration * 6.0),
         f"sprite+posters for {key}")

    if not _nonempty(sheet_tmp):
        raise DeriveError(f"sprite sheet for {key} came out empty")
    tile_h = _tile_height(sheet_tmp, rows)
    os.replace(sheet_tmp, sprite_path(key))
    for tier, tmp in tiles:
        if _nonempty(tmp):
            os.replace(tmp, poster_path(key, tier))
        else:
            try:
                os.remove(tmp)
            except OSError:
                pass

    meta = {
        "key": key, "sheet": os.path.basename(sprite_path(key)),
        "cols": cols, "rows": rows, "count": count,
        "tile_w": config.SPRITE_TILE_W, "tile_h": tile_h,
        "interval": round(interval, 4),
        "duration": round(duration, 3),
        "cover_at": round(pos, 3),
        "tiers": list(config.POSTER_TIERS),
    }
    _write_json(sprite_meta_path(key), meta)
    return meta


def _cover_time(stamps: list, duration: float) -> float:
    """When to cut the poster from. First real shot change past 0.5 s."""
    for t in stamps:
        if t is not None and 0.5 <= t < max(0.5, duration * 0.9):
            return float(t)
    return min(1.0, max(0.0, duration * 0.5))


def _tile_height(sheet: str, rows: int) -> int:
    """The sheet's real tile height, so the UI's CSS offset is not a guess.

    `scale=w:h=-2` picks the height from the aspect ratio and rounds it to an
    even number, so it is knowable only after the fact. Measuring it here costs
    one ffprobe per video and removes a class of "the scrub preview is one row
    off on portrait videos" bug that no amount of arithmetic in the frontend
    could fix.
    """
    got = probe(sheet)
    height = int(got.get("height") or 0)
    return height // max(1, rows) if height else 0


def _write_json(path: str, body: dict) -> None:
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, separators=(",", ":"))
    os.replace(tmp, path)


# ── The whole thing ───────────────────────────────────────────────────────
def derive(key: str, src: str, force: bool = False,
           facts: dict = None) -> dict:
    """Everything this video needs to play instantly. Idempotent.

    Returns a report the mirror stores and the UI shows. Raises `DeriveError`
    only for a source that cannot be read at all; a source that produces a proxy
    but no sprite sheet is reported as a partial success, because a video that
    plays without a scrub preview is worth far more than no video.
    """
    key = str(key)
    if not FFMPEG:
        raise DeriveError("ffmpeg is not on PATH — nothing can be derived")
    if not _nonempty(src):
        raise DeriveError(f"source for {key} is missing or empty: {src}")

    started = time.monotonic()
    with _get_key_lock(key):
        state = have(key)
        if not force and all(state.values()):
            return {"key": key, "skipped": True, **state, "seconds": 0.0}

        with _SLOTS:
            facts = facts or probe(src)
            if not facts:
                raise DeriveError(f"ffprobe could not read {os.path.basename(src)} "
                                  f"— not a video this build of ffmpeg decodes")

            stamps: list = []
            frames = 0
            if force or not (state["proxy"] and state["keyframes"]):
                stamps = _pass_one(key, src, facts)
                frames = _write_keyframe_index(key, stamps, facts)
            else:
                stamps = _stamps_from_index(key)
                frames = len(stamps)

            sprite_note = ""
            if force or not (state["sprite"] and state["posters"]):
                try:
                    _pass_two(key, facts, stamps)
                except DeriveError as e:
                    sprite_note = str(e)
                    log(f"{key}: {e}", SUB, "WARN")

        done = have(key)
        report = {
            "key": key, "skipped": False, **done,
            "frames": frames,
            "duration": round(float(facts.get("duration") or 0.0), 3),
            "proxy_bytes": os.path.getsize(proxy_path(key)) if done["proxy"] else 0,
            "seconds": round(time.monotonic() - started, 2),
        }
        if sprite_note:
            report["note"] = sprite_note
        return report


def _stamps_from_index(key: str) -> list:
    try:
        with open(keyframe_index_path(key), "r", encoding="utf-8") as fh:
            body = json.load(fh)
    except (OSError, ValueError):
        return []
    return [f.get("t") for f in (body.get("frames") or []) if f.get("t") is not None]


# ── Verification the plan asks for, as functions rather than assertions ────
def faststart_ok(path: str) -> bool:
    """True when `moov` precedes `mdat` in this mp4's top-level boxes.

    Read directly rather than through ffprobe, because ffprobe reports what it
    *found*, not where. An mp4 is a flat list of length-prefixed boxes at the top
    level, so this is a dozen seeks: read an 8-byte header, note the type, skip
    ahead by the size, stop at whichever of the two appears first.

    `None` is not returned for a malformed file — an unreadable box structure
    means the answer to "will a player see moov first" is no.
    """
    try:
        with open(path, "rb") as fh:
            offset = 0
            end = os.fstat(fh.fileno()).st_size
            while offset + 8 <= end:
                fh.seek(offset)
                head = fh.read(8)
                if len(head) < 8:
                    return False
                size = int.from_bytes(head[:4], "big")
                kind = head[4:8]
                if size == 1:                       # 64-bit extended size
                    ext = fh.read(8)
                    if len(ext) < 8:
                        return False
                    size = int.from_bytes(ext, "big")
                elif size == 0:                     # to end of file
                    size = end - offset
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size < 8:
                    return False
                offset += size
    except OSError:
        return False
    return False


def keyframe_interval(path: str, seconds: float = 20.0) -> dict:
    """Measured keyframe spacing over the first `seconds` of a file.

    Deliberately *not* called during derivation. Counting key frames means
    decoding packet headers for the whole window, which is cheap once and
    pointless five thousand times — the flags either work for every file or none.
    This is what the verification pass runs on a sample.
    """
    if not FFPROBE:
        return {}
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-read_intervals", f"%+{seconds:g}",
           "-show_entries", "packet=pts_time,flags",
           "-print_format", "json", path]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=90,
                           creationflags=_CREATIONFLAGS)
        body = json.loads(r.stdout.decode("utf-8", "replace"))
    except (subprocess.SubprocessError, OSError, ValueError):
        return {}
    stamps = []
    for pkt in body.get("packets") or []:
        if "K" not in str(pkt.get("flags") or ""):
            continue
        try:
            stamps.append(float(pkt["pts_time"]))
        except (KeyError, TypeError, ValueError):
            continue
    if len(stamps) < 2:
        return {"keyframes": len(stamps), "mean": 0.0, "max": 0.0}
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    return {"keyframes": len(stamps),
            "mean": round(sum(gaps) / len(gaps), 3),
            "max": round(max(gaps), 3),
            "window": seconds}
