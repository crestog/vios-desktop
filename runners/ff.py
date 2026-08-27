"""
runners/ff.py — ffmpeg as a measuring instrument, not a transcoder.

`derive.py` already runs ffmpeg, and this is not a second copy of it. The two
want opposite things from the same binary. `derive._run` **throws its output
away on success** (`subprocess.run(..., capture_output=True)` and then only the
stderr of a failure is read), because what it wants is a file: a proxy, a poster,
a sprite sheet. The exit code is the whole answer.

Here the output *is* the answer. `-f null -` writes no file at all; every fact
this module returns was scraped out of stdout or stderr. So the invocation has
to be a different one, and pretending otherwise would mean either making
`derive._run` return text it has no use for, or calling it and re-running the
decode to see what it said.

**Why ffmpeg's own filters and not a Python library.** The standing instruction
for this repository is *"use mathmatical and proven systems for specific
things"*, and three of these filters are the reference implementation of a
published standard or a textbook statistic:

* `ebur128` implements **EBU Tech 3341/3342 and ITU-R BS.1770** — the loudness
  standard every broadcaster and every streaming platform normalises to. It is
  not an approximation of LUFS, it is LUFS. Getting the same number out of
  Python means `librosa` plus a K-weighting filter plus a gating implementation,
  which is three chances to be subtly wrong about a value that has one correct
  answer.
* `scdet`'s `lavfi.scd.mafd` is **mean absolute frame difference**, the plainest
  possible motion statistic: the mean over all pixels of |this frame − last
  frame|. There is nothing to get wrong and nothing to tune, and because it is
  emitted on every frame it serves as the motion-energy series *and* the cut
  detector from a single decode.
* `signalstats` reports min/low/avg/high/max per plane plus `SATAVG` and
  `HUEAVG`. The previous session cross-checked `YAVG`, `YMIN`, `YMAX` against
  numpy over raw `yuv444p` pixels and they matched exactly; `SATAVG` matched to
  within chroma-subsampling tolerance, confirming it is `hypot(U−128, V−128)`.

`silencedetect` is the one that is a threshold rather than a standard, so its
threshold is a parameter and is recorded in the claim that comes out of it.

**One decode, many answers.** A 95-second reel decodes at ~190× real time on
this laptop — half a second — so the expensive thing is not the arithmetic, it
is starting ffmpeg and reading the file. `scdet=…,signalstats,metadata=print`
is therefore built as *one* chain: shot boundaries, motion energy, brightness,
contrast, saturation and hue all come out of the same pass. Splitting them into
four passes would be four decodes for no benefit.

**Nothing here raises on a damaged file.** `run()` returns the exit code instead
of throwing, and `frames()` returns however many frames were actually decoded.
That is deliberate and it is tested against a real broken file on this disk —
`media/proxy/loc_d640ec1bfdeed4fc.mp4` has 215 `Invalid NAL unit size` errors,
decodes 132 of its ~192 frames, and **exits 0**. A runner that trusted the exit
code would write two thirds of a reel's evidence and mark the pass complete. So
the shortfall is measured and reported, and it is the runner's job to decide
whether two thirds is evidence or is a lie.
"""

from __future__ import annotations

import math
import os
import re
import subprocess

from derive import FFMPEG, FFPROBE, _CREATIONFLAGS

# ══════════════════════════════════════════════════════════════════════════
# INVOCATION
# ══════════════════════════════════════════════════════════════════════════

# Long enough for a full decode of anything a phone shoots, short enough that a
# hung ffmpeg does not hold the single worker thread for the rest of the
# session. Measured: 95 s of 720p costs 0.5 s, so this is ~1000× headroom.
TIMEOUT = 600.0

_BASE = ("-hide_banner", "-nostdin", "-v", "info", "-nostats")


class FFMissing(RuntimeError):
    """No ffmpeg or ffprobe on PATH. Separate from a failed run on purpose:
    one is a machine that cannot do this work at all and the other is one file
    that would not decode, and only the first is worth telling the user to fix."""


def available() -> bool:
    return bool(FFMPEG and FFPROBE)


def run(args: list, timeout: float = TIMEOUT) -> tuple:
    """Run ffmpeg and return `(rc, stdout, stderr)`. Does not raise on failure.

    `text=True` with `errors="replace"`: ffmpeg writes filenames and stream
    metadata into its log, and on this machine a reel captured from Instagram
    routinely carries a title in Devanagari or an emoji in its `handler_name`.
    Under the console's cp1252 default that decode raises `UnicodeDecodeError`
    from inside `subprocess`, which would present as "this video is corrupt"
    for a video that is fine and a caption that is not ASCII.

    A timeout is returned as `rc = -1` with the reason in stderr rather than
    propagated, so one pathological file cannot end the worker loop.
    """
    if not FFMPEG:
        raise FFMissing("ffmpeg is not on PATH")
    cmd = [FFMPEG, *_BASE, *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           errors="replace", timeout=timeout,
                           creationflags=_CREATIONFLAGS)
    except subprocess.TimeoutExpired:
        return -1, "", f"timed out after {timeout:.0f}s"
    return p.returncode, p.stdout or "", p.stderr or ""


def probe(path: str) -> dict:
    """`derive.probe`, re-exported. There is exactly one prober in this program.

    Kept as a name here so a runner never has to decide which module to ask —
    every fact about the container comes from `derive.probe`, which already
    handles rotation (a portrait reel shot by a phone reports 1920×1080 with a
    90° matrix and must be read as 1080×1920) and already normalises `fps` out
    of `r_frame_rate`. Re-implementing either would be two answers to
    "how tall is this video", which is the class of bug that makes a sprite
    sheet not line up with a player.
    """
    from derive import probe as _probe                          # noqa: PLC0415
    return _probe(path)


# ══════════════════════════════════════════════════════════════════════════
# metadata=print — the per-frame table
# ══════════════════════════════════════════════════════════════════════════

_FRAME_RE = re.compile(r"^frame:(\d+)\s+pts:(-?\d+|N/A)\s+pts_time:(\S+)")
_KV_RE = re.compile(r"^(lavfi\.[\w.]+)=(.*)$")


def _kv_frames(text: str) -> list:
    """Parse `metadata=print` / `ametadata=print` stdout into one dict per frame.

    The format is a `frame:N pts:P pts_time:T` header followed by any number of
    `lavfi.<filter>.<key>=<value>` lines, and it is identical for video and
    audio — which is the whole reason this is one function and not two. The
    `signalstats` table and the `r128` curve are the same shape; only the keys
    differ.

    `t` is `pts_time`, the container's presentation timestamp, never
    `idx / fps`. Most of what a phone records is variable-frame-rate and has no
    single frame rate to divide by; over 95 seconds the difference is large
    enough to put a claim in the wrong shot.
    """
    rows, cur = [], None
    for line in text.splitlines():
        m = _FRAME_RE.match(line)
        if m:
            if cur is not None:
                rows.append(cur)
            t = m.group(3)
            cur = {"idx": int(m.group(1)),
                   "t": 0.0 if t in ("N/A", "nan") else float(t)}
            continue
        if cur is None:
            continue
        kv = _KV_RE.match(line)
        if kv:
            cur[kv.group(1)] = _num(kv.group(2))
    if cur is not None:
        rows.append(cur)
    return rows


def frames(src: str, vf: str, timeout: float = TIMEOUT,
           extra: list = None) -> tuple:
    """Decode `src` through `vf` and return `(rows, meta)`.

    `rows` is one dict per decoded frame: `idx`, `t`, and every `lavfi.*` key
    the chain exported, floats where they parse as floats and strings where they
    do not. `meta` carries what the run itself revealed — `rc`, `decoded`,
    `errors` (how many stderr lines look like a decode complaint), and `stderr`
    trimmed to its last few lines.

    `metadata=print:file=-` writes to **stdout**, which is why this can be
    parsed at all: ffmpeg's own progress and warnings go to stderr, so the two
    streams separate the data from the noise with no filtering. Appending
    `:file=-` is the caller's job, not this function's, because the same chain
    is sometimes wanted without it.
    """
    rc, out, err = run(["-i", src, *(extra or []), "-vf", vf, "-f", "null", "-"],
                       timeout)
    rows = _kv_frames(out)
    return rows, {"rc": rc, "decoded": len(rows), "errors": _count_errors(err),
                  "stderr": _tail(err)}


def _num(raw: str):
    """Float when it is one, string when it is not. `nan`/`inf` become None.

    `signalstats` emits `YDIF=nan` for the first frame of some files — there is
    no previous frame to difference against — and `float("nan")` would carry
    that into a mean and turn the whole series into NaN silently. None is
    droppable; NaN is contagious.
    """
    raw = raw.strip()
    try:
        v = float(raw)
    except ValueError:
        return raw
    return None if (math.isnan(v) or math.isinf(v)) else v


_ERR_RE = re.compile(r"(Invalid NAL|error|Error|corrupt|damaged|"
                     r"decode_slice_header|no frame|non-existing PPS)")


def _count_errors(err: str) -> int:
    return sum(1 for ln in err.splitlines() if _ERR_RE.search(ln))


def _tail(err: str, n: int = 4) -> str:
    keep = [ln.strip() for ln in err.splitlines() if ln.strip()]
    return " · ".join(keep[-n:])[:400]


def shortfall(decoded: int, facts: dict) -> float:
    """How much of the file did not decode, as a fraction of what was expected.

    0.0 when everything expected came through — and often slightly negative,
    which is not an error: `duration × fps` is a container-level estimate and a
    real file is routinely one or two frames longer than it. Clamped at 0.

    Returns 0.0 when there is nothing to compare against. A file with no
    duration and no frame rate is a file this cannot judge, and inventing a
    shortfall of 1.0 for it would hold a pass on a video that decoded fine.
    """
    dur = float(facts.get("duration") or 0.0)
    fps = float(facts.get("fps") or 0.0)
    want = dur * fps
    if want < 1.0 or decoded < 0:
        return 0.0
    return max(0.0, 1.0 - decoded / want)


# ══════════════════════════════════════════════════════════════════════════
# scdet — shot boundaries
# ══════════════════════════════════════════════════════════════════════════

_SCD_RE = re.compile(r"lavfi\.scd\.score:\s*([\d.]+),\s*lavfi\.scd\.time:\s*"
                     r"([\d.]+)")

# `scdet`'s threshold is a percentage of maximum possible frame difference.
# 10 is ffmpeg's own default and the value the previous session measured
# against: on `.check/src.mp4` it finds the two real cuts at t=2 and t=4 and
# nothing else, and on a 95-second continuous take it finds nothing, which is
# the correct answer rather than a failure to detect.
SCD_THRESHOLD = 10.0


def cut_times(stderr: str) -> list:
    """`[(t, score)]` for every scene change ffmpeg reported, in time order.

    Read from **stderr**, not from the `metadata=print` table, and the reason is
    worth writing down: `scdet` sets `lavfi.scd.time` only on the frames where a
    cut occurred, so those keys are present on 2 lines out of 192 and absent
    everywhere else. Both sources agree; stderr is simply the one that does not
    require scanning every frame's dictionary for a key that is usually missing.
    """
    out = [(float(m.group(2)), float(m.group(1)))
           for m in _SCD_RE.finditer(stderr)]
    out.sort(key=lambda p: p[0])
    return out


ANALYSE_VF = (f"scdet=threshold={SCD_THRESHOLD:g},signalstats,"
              "metadata=print:file=-")


def analyse(src: str, timeout: float = TIMEOUT) -> tuple:
    """The one decode that answers four passes. `(rows, cuts, meta)`.

    `rows` carries `lavfi.scd.mafd` (motion), `lavfi.signalstats.YAVG` and
    friends (brightness, contrast, saturation, hue) per frame; `cuts` carries
    the shot boundaries. `shots-cpu`, `motion`, `colour` and `perframe` all read
    the same three values rather than each starting their own ffmpeg — which is
    why the result is memoised on the job in `runners/__init__.py`.

    `-an` because none of the four wants audio and decoding it would be pure
    cost. Audio has its own pass with its own chain.

    This repeats nothing: `_kv_frames` is shared with `frames()`. What differs
    is that ffmpeg's **whole** stderr is kept rather than `_tail` of it, because
    the cut lines live in the middle of the log and `frames()` throws that away.
    """
    rc, out, err = run(["-i", src, "-an", "-vf", ANALYSE_VF, "-f", "null", "-"],
                       timeout)
    rows = _kv_frames(out)
    return rows, cut_times(err), {
        "rc": rc, "decoded": len(rows), "errors": _count_errors(err),
        "stderr": _tail(err)}


# ══════════════════════════════════════════════════════════════════════════
# ebur128 — loudness, to the standard
# ══════════════════════════════════════════════════════════════════════════

_SUM_RE = {
    "lufs":       re.compile(r"^\s*I:\s*(-?[\d.]+|-inf)\s*LUFS"),
    "gate":       re.compile(r"^\s*Threshold:\s*(-?[\d.]+|-inf)\s*LUFS"),
    "lra":        re.compile(r"^\s*LRA:\s*(-?[\d.]+)\s*LU"),
    "lra_low":    re.compile(r"^\s*LRA low:\s*(-?[\d.]+|-inf)\s*LUFS"),
    "lra_high":   re.compile(r"^\s*LRA high:\s*(-?[\d.]+|-inf)\s*LUFS"),
    "true_peak":  re.compile(r"^\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS"),
}

# ebur128 reports a window it cannot yet measure as -120.691 LUFS — its floor,
# not a measurement. The first three 100 ms windows of every file read this way
# because the momentary window is 400 ms wide and is not full yet, and a fully
# silent passage reads the same. Averaging the sentinel with real readings would
# drag a loudness curve toward a number no listener ever heard, so it is dropped
# from the curve; the silence map is what reports silence, and it is measured.
SILENCE_FLOOR = -70.0

# silencedetect's threshold, in dBFS. -50 is quiet enough that room tone and a
# noise floor still count as sound, loud enough that a genuinely muted passage
# is found. Recorded in the claim, because unlike LUFS this number is a choice.
SILENCE_DB = -50.0
SILENCE_MIN = 0.3          # seconds; shorter than this is a gap between words


def loudness(src: str, timeout: float = TIMEOUT) -> dict:
    """EBU R128 loudness plus the silence map, from one audio decode.

    Returns `{"lufs", "lra", "lra_low", "lra_high", "true_peak", "gate",
    "curve": [(t, momentary, short_term)], "silences": [(t0, t1)], "meta"}`.
    Anything ffmpeg did not print is `None` rather than 0.0 — 0 LUFS is a *very
    loud* file, and a missing measurement that read as 0 would be the loudest
    reel in the archive and would sort to the top of every query about audio.

    **Two readers of one filter, and why.** The gated integrated loudness, the
    loudness range and the true peak are printed once, as prose, in the
    `Summary:` block on stderr — those are the authoritative values, computed
    over the whole file with BS.1770's two-stage gate applied, and they are
    parsed with the regexes above. The per-window curve is a different problem:
    `ebur128=…:metadata=1` stops logging its running readings and exports them
    as frame metadata instead, which `ametadata=print:file=-` then writes to
    **stdout** as `lavfi.r128.M=…`. So the curve arrives as key/value pairs on a
    stream that carries nothing else, rather than as 64 lines of prose mixed
    into the same log as the decoder's warnings. The previous revision of this
    function set `metadata=1` *and* parsed the log, and got an empty curve every
    time — the flag is what suppresses the lines it was looking for.

    `lavfi.r128.true_peak` is **linear amplitude, not dB**, which is worth
    stating because 0.129 looks like a plausible dBFS reading and is not one:
    it is −17.8 dBFS. The summary's `Peak:` is already in dB and is the one
    reported; the metadata value is converted only for the per-window curve.

    `-vn`, and the caller having already established that there is audio at all.
    A file with no audio stream fails here with *"Output file does not contain
    any stream"* and a nonzero rc, which is a true statement about a perfectly
    normal file — `.check/long.mp4` is one. The `loudness` runner checks
    `has_audio` from the probe and skips before reaching this, so an error here
    means something else and is returned rather than swallowed.
    """
    af = (f"ebur128=peak=true:metadata=1,"
          f"silencedetect=noise={SILENCE_DB:g}dB:d={SILENCE_MIN:g},"
          f"ametadata=print:file=-")
    rc, out, err = run(["-i", src, "-vn", "-af", af, "-f", "null", "-"],
                       timeout)

    got = {}
    for line in err.splitlines():
        for name, rx in _SUM_RE.items():
            if name in got:
                continue
            m = rx.search(line)
            if m:
                got[name] = _inf(m.group(1))

    curve = []
    for r in _kv_frames(out):
        mom, short = r.get("lavfi.r128.M"), r.get("lavfi.r128.S")
        if not isinstance(mom, float) or mom <= SILENCE_FLOOR:
            continue
        curve.append((round(r["t"], 3), round(mom, 2),
                      None if not isinstance(short, float) or short <= SILENCE_FLOOR
                      else round(short, 2)))

    got["curve"] = curve
    got["silences"] = silences(err)
    got["meta"] = {"rc": rc, "errors": _count_errors(err), "stderr": _tail(err)}
    return got


def _inf(raw: str):
    """`-inf` and `-120.7` are both "silent". Only the first is not a number."""
    raw = raw.strip()
    if raw.endswith("inf"):
        return None
    try:
        return float(raw)
    except ValueError:
        return None


_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)\s*\|\s*silence_duration:\s*"
                      r"([\d.]+)")


def silences(err: str) -> list:
    """`[(t0, t1)]` for every detected silent span, in time order.

    A `silence_start` with no matching `silence_end` means the file ended still
    silent — ffmpeg does not close the span it is in when input runs out. That
    is a real span and dropping it would under-report trailing silence, which on
    a reel is exactly the span that matters (a hook that ends in dead air). It
    is closed at `None` and the caller substitutes the duration, which is the
    only place that knows it.
    """
    out, open_at = [], None
    for line in err.splitlines():
        m = _SIL_START.search(line)
        if m:
            open_at = float(m.group(1))
            continue
        m = _SIL_END.search(line)
        if m and open_at is not None:
            out.append((max(0.0, open_at), float(m.group(1))))
            open_at = None
    if open_at is not None:
        out.append((max(0.0, open_at), None))
    return out


# ══════════════════════════════════════════════════════════════════════════
# FRAME EXTRACTION
# ══════════════════════════════════════════════════════════════════════════

def thumbnails(src: str, fps: float = 1.0, size: int = 64,
               timeout: float = TIMEOUT) -> tuple:
    """Decode `src` to raw RGB thumbnails in memory. `(bytes, n, size, meta)`.

    `-f rawvideo -pix_fmt rgb24` to stdout, which means the caller gets actual
    pixels rather than re-encoded JPEGs it would have to decode again. At 64×64
    that is 12,288 bytes per frame: a 95-second reel at 1 fps is 1.1 MB and
    takes 0.7 s, measured. Nothing is written to disk.

    This exists because `signalstats` answers *statistics about* colour —
    average luminance, average saturation — and cannot answer *which colours*.
    A palette needs the pixels. Squashing to 64×64 first is not a compromise for
    speed: a palette is a claim about the large areas of an image, and bilinear
    downscaling is an area average, so the small bright detail that would
    dominate a naive per-pixel histogram is correctly averaged away.

    Returns the raw buffer rather than a numpy array so this module stays
    importable on a machine with no numpy — the runner that needs the array is
    the one that should fail to load, not the ffmpeg layer.
    """
    if not FFMPEG:
        raise FFMissing("ffmpeg is not on PATH")
    cmd = [FFMPEG, *_BASE, "-i", src, "-an",
           "-vf", f"fps={fps:g},scale={size}:{size}:flags=bilinear",
           "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           creationflags=_CREATIONFLAGS)
    except subprocess.TimeoutExpired:
        return b"", 0, size, {"rc": -1, "errors": 1,
                              "stderr": f"timed out after {timeout:.0f}s"}

    raw = p.stdout or b""
    stride = size * size * 3
    n = len(raw) // stride
    err = (p.stderr or b"").decode("utf-8", "replace")
    return raw[:n * stride], n, size, {
        "rc": p.returncode, "errors": _count_errors(err), "stderr": _tail(err)}


def extract_frames(src: str, dest_dir: str, fps: float = 2.0,
                   width: int = 512, timeout: float = TIMEOUT) -> tuple:
    """Write `dest_dir/f%06d.jpg` at `fps`, and return `(paths, times, meta)`.

    Times come from ffmpeg's own `-frame_pts`-free arithmetic being avoided
    entirely: at a forced constant `fps` the *n*th written frame is at
    `n / fps` by construction, because that is what `fps=` does — it duplicates
    and drops source frames to hit exactly that cadence. So the index and the
    time cannot disagree, which is not true of any scheme that reads timestamps
    back out of filenames.

    `width` scales the long edge down with the aspect preserved (`-1` height,
    rounded to even for the encoder). 512 px is enough for OCR and for a
    palette and small enough that 190 frames of a 95-second reel is a few
    megabytes rather than a few hundred.
    """
    os.makedirs(dest_dir, exist_ok=True)
    pattern = os.path.join(dest_dir, "f%06d.jpg")
    rc, _out, err = run(
        ["-i", src, "-an", "-vf", f"fps={fps:g},scale={width}:-2:flags=bicubic",
         "-q:v", "3", "-fps_mode", "passthrough", "-y", pattern], timeout)

    got = sorted(f for f in os.listdir(dest_dir)
                 if f.startswith("f") and f.endswith(".jpg"))
    paths = [os.path.join(dest_dir, f) for f in got]
    times = [round(i / fps, 4) for i in range(len(paths))]
    return paths, times, {"rc": rc, "errors": _count_errors(err),
                          "stderr": _tail(err), "fps": fps, "width": width}
