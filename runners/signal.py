"""
runners/signal.py — the passes that measure the reel, with no model anywhere.

Six passes: `cuts` reads rhythm out of the shot table, `motion` and `colour`
read the per-frame `signalstats`/`scdet` table, `loudness` reads EBU R128, and
`caption` reads what the uploader wrote. None of them calls a model, and that is
not a limitation of this laptop — it is the point. The standing instruction for
this archive is *"i dont trust one single llm … use mathmatical and proven
systems for specific things"*, and every number in this file has a definition
that predates machine learning:

* **Average shot length** is Barry Salt's, from 1974, and is still the number
  film scholars quote when they compare editing across decades.
* **Coefficient of variation** (σ/μ) is the standard scale-free measure of
  dispersion, which is what makes "regular" comparable between a reel that cuts
  every 0.4 s and one that cuts every 4 s.
* **Ordinary least squares** on shot length against time is a slope with a
  closed form and no hyperparameters.
* **LUFS and LRA** are ITU-R BS.1770 and EBU Tech 3342, computed by ffmpeg's
  reference implementation — see `runners/ff.py`.
* **Mean absolute frame difference** is a mean of absolute differences.

Where a proven method does not exist for what is wanted, the pass does not
invent one. `motion` declares `camera_move` and does not emit it: separating a
pan from a zoom from a subject walking across a locked-off frame needs optical
flow, and a threshold on frame difference that *called* itself camera movement
would be a guess wearing a metric's name. The gap is recorded in the run's notes
so the Engine tab can say which kinds a pass actually produced.

**Every claim carries the number it was derived from.** `value` is the
searchable phrasing and `num` is the sortable measurement, which is the
separation `Emission.claim` was built for: "which reels cut fastest" must be a
range scan over `num`, not a string comparison over "12.4/min".
"""

from __future__ import annotations

import math
import re

from sizing.base import Emission, SkipPass

from . import ff

CH_STYLE = "style"
CH_AUDIO = "audio"
CH_CAPTION = "caption"


# ══════════════════════════════════════════════════════════════════════════
# SHARED ARITHMETIC
# ══════════════════════════════════════════════════════════════════════════

def _mean(xs) -> float:
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _sd(xs) -> float:
    """Population standard deviation. Population, not sample, deliberately.

    These series are not a sample of some larger set of frames — they are every
    frame in the reel. Bessel's correction estimates a population from a sample
    and there is no population here beyond the file itself, so dividing by n−1
    would inflate the answer for exactly the short reels where it matters most.
    """
    xs = [x for x in xs if x is not None]
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _cv(xs) -> float:
    """Coefficient of variation, σ/μ. 0 for a constant series, unbounded above."""
    m = _mean(xs)
    return _sd(xs) / m if m > 1e-9 else 0.0


def _regularity(xs) -> float:
    """`1 − CV`, clamped to 0–1. A metronome scores 1, chaos scores 0.

    Clamped rather than left unbounded because CV above 1 — which a reel with
    one very long hold among fast cuts genuinely produces — would otherwise give
    a negative "regularity", and there is no such thing as less regular than
    completely irregular. The clamp is a floor on the *scale*, not a discarded
    measurement: the CV itself is carried in the same claim's value.
    """
    return max(0.0, min(1.0, 1.0 - _cv(xs)))


def _slope(xs, ys) -> float:
    """OLS slope of `ys` on `xs`. 0 when it is not determined.

    Closed form, no library: `Σ(x−x̄)(y−ȳ) / Σ(x−x̄)²`. Returns 0 for fewer than
    three points, because a line through two points is not a trend and reporting
    its slope as one would make every two-shot reel look decisively accelerating.
    """
    if len(xs) < 3 or len(xs) != len(ys):
        return 0.0
    mx, my = _mean(xs), _mean(ys)
    den = sum((x - mx) ** 2 for x in xs)
    if den < 1e-12:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _band(x: float, cuts, names) -> str:
    """Name a measurement by which band it falls in. `cuts` ascending."""
    for c, n in zip(cuts, names):
        if x < c:
            return n
    return names[-1]


def _interval(rows) -> float:
    """Median gap between consecutive frame timestamps. 0 if undeterminable."""
    gaps = sorted(b["t"] - a["t"] for a, b in zip(rows, rows[1:])
                  if b["t"] > a["t"])
    return gaps[len(gaps) // 2] if gaps else 0.0


def _across_cuts(job, rows) -> set:
    """Indices into `rows` whose frame difference straddles a shot boundary.

    **This is the difference between measuring motion and measuring editing.**
    A frame differenced against the last frame of the previous shot is comparing
    two unrelated images, so it always scores enormously whatever the camera was
    doing — on `.check/src.mp4` the two cut frames read 29.4 and 29.0 against a
    median of 0.117, and leaving them in put 58% of the reel's "motion energy"
    into two frames out of 191. It also drove the coefficient of variation to
    5.69, which clamps `stability` to exactly 0 and labels a static gradient
    *jolting*.

    Left in, `motion_energy` would drift into being a proxy for cut rate on
    fast-cut reels — which is the archive's whole population — and `cuts`
    already measures cut rate directly and better. Two components reporting the
    same underlying quantity, one of them under a name for something else, is
    worse than one component fewer.

    Boundaries come from the shot table when it has any, so this stays correct
    when the shots were found by TransNetV2 on Kaggle rather than by `scdet`
    here. The tolerance is one frame interval: a boundary time and a frame
    timestamp are both rounded, and the cut may be reported at either the last
    frame of the old shot or the first of the new.
    """
    shots = job.shots()
    bounds = [float(s["t0"]) for s in shots if float(s["t0"]) > 0.0]
    if not bounds:
        return set()
    tol = _interval(rows) or 0.05
    out = set()
    for b in bounds:
        near = [i for i, r in enumerate(rows) if abs(r["t"] - b) <= tol]
        out.update(near or [min(range(len(rows)),
                                key=lambda i: abs(rows[i]["t"] - b))])
    return out


def _mafd(job, rows, drop_cuts: bool = True) -> list:
    """`[(t, mafd)]` for every usable frame. Cut frames excluded by default.

    The first frame is always dropped: its mean absolute difference is 0 because
    there is nothing before it to difference against, not because nothing moved.
    On a one-second clip that single fabricated zero pulls the mean down by a
    sixth.
    """
    skip = _across_cuts(job, rows) if drop_cuts else set()
    return [(r["t"], r["lavfi.scd.mafd"]) for i, r in enumerate(rows)
            if i > 0 and i not in skip
            and isinstance(r.get("lavfi.scd.mafd"), float)]


# ══════════════════════════════════════════════════════════════════════════
# cuts — editing rhythm
# ══════════════════════════════════════════════════════════════════════════

def cuts(job, em: Emission) -> dict:
    """Average shot length, cut rate, regularity and acceleration.

    Reads the `shot` table and nothing else — no decode, no file access at all —
    which is why it costs milliseconds and why it works identically whether the
    boundaries came from this laptop's `scdet` or from Kaggle's TransNetV2. The
    provenance question is answered by `shot.detector`, not by this pass.

    Skips rather than fails when there are no shots. A reel whose shot pass has
    not run yet is not a broken reel, and `SkipPass` is what distinguishes
    *"nothing to read"* from *"tried and could not"* — the difference between a
    sweep that is complete with gaps and one that has unsolved problems.
    """
    rows = job.shots()
    if not rows:
        raise SkipPass("no shots for this reel yet — run a shot pass first")

    lens = [float(s["t1"]) - float(s["t0"]) for s in rows
            if float(s["t1"]) > float(s["t0"])]
    if not lens:
        raise SkipPass(f"{len(rows)} shot row(s), none with positive length")

    duration = sum(lens)
    n = len(lens)
    asl = duration / n
    # Cuts, not shots: three shots have two cuts between them. Per minute
    # because that is the unit every editing reference quotes.
    cpm = (n - 1) / duration * 60.0 if duration > 0 else 0.0
    reg = _regularity(lens)
    mids = [float(s["t0"]) + (float(s["t1"]) - float(s["t0"])) / 2.0
            for s in rows if float(s["t1"]) > float(s["t0"])]
    slope = _slope(mids, lens)

    em.claim(CH_STYLE, "asl", f"{asl:.2f}s average shot", num=round(asl, 3))
    em.claim(CH_STYLE, "cut_rate",
             f"{cpm:.1f} cuts/min across {n} shot" + ("s" if n != 1 else ""),
             num=round(cpm, 2))
    em.claim(CH_STYLE, "rhythm",
             _band(_cv(lens), (0.15, 0.4, 0.8),
                   ("metronomic", "even", "varied", "erratic")),
             num=round(reg, 4))

    # Seconds of shot length gained per second of runtime. Negative is a reel
    # that speeds up as it goes, which is the standard hook-to-payoff shape.
    # The dead band is 0.005 s/s — over a 30-second reel that is a 0.15 s change
    # in shot length, which is below what an editor would call a decision.
    em.claim(CH_STYLE, "acceleration",
             _band(slope, (-0.005, 0.005),
                   ("accelerating", "steady", "decelerating")),
             num=round(slope, 5))

    em.notes = {"shots": n, "asl": round(asl, 3), "cuts_per_min": round(cpm, 2),
                "regularity": round(reg, 3), "cv": round(_cv(lens), 3)}
    return {}


# ══════════════════════════════════════════════════════════════════════════
# motion — how much the frame changes
# ══════════════════════════════════════════════════════════════════════════

# `camera_move` and `stability` are declared by this component and deliberately
# never emitted.
#
# **camera_move.** Telling a pan from a zoom from a subject crossing a locked-off
# frame needs optical flow — a dense field of per-pixel displacement vectors —
# and frame difference is a scalar with no direction in it. A threshold on mafd
# that called itself "camera movement" would be a guess wearing the name of a
# measurement, and it would be wrong in the specific case that matters most: a
# static camera on a fast-moving subject scores exactly like a pan.
#
# **stability**, withheld after measuring it. The obvious construction is
# `1 − CV(mafd)`: steady motion has low dispersion, shake has high. It inverts.
# CV is driven by the *proportion of still frames*, not by jitter, so a reel with
# held frames reads as maximally unstable. Measured on the two reference files:
#
#     .check/src.mp4   31% of frames below mafd 0.05   CV 1.057 → 0.00 "jolting"
#     .check/long.mp4    0% of frames below mafd 0.05   CV 0.180 → 0.82 "steady"
#
# The static reel scores jolting and the continuously-moving one scores steady —
# the answer is not noisy, it is backwards. Real stability is high-frequency
# jitter in the *direction* of movement, which needs the same optical flow
# `camera_move` needs, and there is no citable pre-ML estimator for it from a
# scalar difference series. So the number is not emitted under a name that would
# make it look like one. `motion_energy` — a mean of a well-defined quantity over
# a correctly-scoped span — is what this pass can honestly say.
NOT_EMITTED = (("camera_move", "needs optical flow — mafd has no direction"),
               ("stability", "1−CV(mafd) inverts: it measures stillness, "
                             "not shake"))


def motion(job, em: Emission) -> dict:
    """Motion energy overall and per shot. Two declared kinds are withheld.

    Motion energy comes from `lavfi.scd.mafd` — the mean over every pixel of the
    absolute difference between this frame and the last, which `scdet` computes
    as a by-product of deciding where the cuts are. So this pass and `shots-cpu`
    read the same decode; see `runners.LocalJob.analysis` for the memo that makes
    that true.

    Two things make the number mean what it says. Frames that straddle a cut are
    excluded, because differencing two unrelated images measures the edit rather
    than the movement — see `_across_cuts`, where the measurement is. And
    `stability` is not emitted at all, because the only construction available
    from a scalar difference series inverts — see `NOT_EMITTED`, which carries
    the two reference files' numbers.
    """
    rows = job.analysis()
    if not rows:
        raise SkipPass("no frames decoded for this reel")

    series = _mafd(job, rows)
    if len(series) < 2:
        raise SkipPass(f"{len(series)} usable frame difference(s) — "
                       "too few to describe motion")

    vals = [v for _, v in series]
    energy = _mean(vals)

    em.claim(CH_STYLE, "motion_energy",
             _band(energy, (0.6, 2.0, 6.0),
                   ("still", "gentle", "active", "frenetic")),
             num=round(energy, 4))

    per_shot = 0
    for s in job.shots():
        t0, t1 = float(s["t0"]), float(s["t1"])
        inside = [v for t, v in series if t0 <= t < t1]
        if len(inside) < 2:
            continue
        em.claim(CH_STYLE, "motion_energy",
                 _band(_mean(inside), (0.6, 2.0, 6.0),
                       ("still", "gentle", "active", "frenetic")),
                 shot_idx=int(s["idx"]), num=round(_mean(inside), 4))
        per_shot += 1

    dropped = len(rows) - 1 - len(series)
    em.notes = {"frames": len(series), "energy": round(energy, 4),
                "cv": round(_cv(vals), 3), "per_shot": per_shot,
                "not_emitted": "; ".join(f"{k} ({why})"
                                         for k, why in NOT_EMITTED)}
    if dropped:
        em.notes["across_cuts"] = f"{dropped} frame(s) excluded"
    return {}


# ══════════════════════════════════════════════════════════════════════════
# colour — what the reel looks like
# ══════════════════════════════════════════════════════════════════════════

# The palette grid. 6 levels per RGB axis is 216 bins — the old web-safe
# palette, and a size chosen for the same reason it was then: fine enough that
# red and orange land in different bins, coarse enough that a gradient does not
# scatter into a hundred bins with one pixel each.
PAL_LEVELS = 6
PAL_COLOURS = 6


def colour(job, em: Emission) -> dict:
    """Brightness, contrast, saturation, temperature and a palette.

    The first four come from `signalstats`, whose `YAVG`, `YLOW`, `YHIGH`,
    `SATAVG`, `UAVG` and `VAVG` the previous session cross-checked against numpy
    over raw `yuv444p` pixels: `YAVG` matched to three decimals and `YMIN`/`YMAX`
    exactly, confirming `SATAVG` is `hypot(U−128, V−128)`.

    **Contrast is `YHIGH − YLOW`, not `YMAX − YMIN`.** `signalstats` defines LOW
    and HIGH as the 10th and 90th percentile of the luminance histogram, so the
    interdecile range is robust: one blown highlight or one crushed black pixel
    moves MAX or MIN to the rail and would report every reel as maximum contrast.

    **Temperature is `VAVG − UAVG`.** In YUV, V is the red-difference chroma and
    U the blue-difference, so their difference *is* the warm-cool axis, read
    straight out of the colour space with no hue arithmetic. The alternative —
    thresholding `HUEAVG` in degrees — has to handle wraparound at 0°/360°,
    where red lives, and gets a reel lit by a single red practical exactly
    backwards.

    **The palette is a histogram, not a clustering.** Pixels are binned on a
    6×6×6 RGB grid and the most populated bins are reported with the mean colour
    of the pixels actually in them. That is deterministic, has no seeding
    question and no iteration count, and can be explained in one sentence —
    which k-means, whose answer depends on where it started, cannot. `method` is
    recorded in the claim so a later palette from a different algorithm is
    comparable rather than silently mixed in.
    """
    rows = job.analysis()
    if not rows:
        raise SkipPass("no frames decoded for this reel")

    def col(name):
        key = "lavfi.signalstats." + name
        return [r[key] for r in rows if isinstance(r.get(key), float)]

    yavg, ylow, yhigh = col("YAVG"), col("YLOW"), col("YHIGH")
    satavg, uavg, vavg = col("SATAVG"), col("UAVG"), col("VAVG")
    if not yavg:
        raise SkipPass("signalstats reported no luminance for this reel")

    # 8-bit video: luma spans 0–255, chroma is centred on 128. Everything below
    # is normalised into 0–1 so a claim is comparable across reels, and the raw
    # 8-bit reading is kept in `value` so nothing is lost to the rescaling.
    bright = _mean(yavg) / 255.0
    contrast = (_mean(yhigh) - _mean(ylow)) / 255.0 if yhigh and ylow else 0.0
    # SATAVG is a radius in the chroma plane; 128 is the largest a legal 8-bit
    # signal reaches on either axis, so it is the normaliser and values are
    # clamped rather than allowed past 1 by a superblack/superwhite excursion.
    sat = min(1.0, _mean(satavg) / 128.0) if satavg else 0.0
    temp = (_mean(vavg) - _mean(uavg)) / 255.0 if uavg and vavg else 0.0

    em.claim(CH_STYLE, "brightness",
             _band(bright, (0.25, 0.45, 0.65),
                   ("dark", "low-key", "balanced", "bright")),
             num=round(bright, 4))
    em.claim(CH_STYLE, "contrast",
             _band(contrast, (0.25, 0.5, 0.72),
                   ("flat", "soft", "punchy", "harsh")),
             num=round(contrast, 4))
    em.claim(CH_STYLE, "saturation",
             _band(sat, (0.12, 0.28, 0.5),
                   ("desaturated", "muted", "saturated", "vivid")),
             num=round(sat, 4))
    em.claim(CH_STYLE, "temperature",
             _band(temp, (-0.02, 0.02), ("cool", "neutral", "warm")),
             num=round(temp, 5))

    pal = _palette(job)
    if pal:
        em.claim(CH_STYLE, "palette",
                 {"method": f"rgb{PAL_LEVELS}³ histogram mode",
                  "colours": pal},
                 num=round(pal[0]["share"], 4))

    em.notes = {"frames": len(yavg), "brightness": round(bright, 3),
                "contrast": round(contrast, 3), "saturation": round(sat, 3),
                "temperature": round(temp, 4), "palette": len(pal)}
    return {}


def _palette(job) -> list:
    """Top colours as `[{"hex", "rgb", "share"}]`, most common first.

    Returns `[]` rather than raising when numpy is absent or the decode gave
    nothing: a reel with four of five colour measurements is better than a reel
    with none, and the caller reports the shortfall in its notes.
    """
    try:
        import numpy as np                                       # noqa: PLC0415
    except ImportError:
        return []

    raw, n, size, _meta = ff.thumbnails(job.source)
    if n < 1:
        return []

    px = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
    if not px.size:
        return []

    # Bin on a 6×6×6 grid. `// 43` maps 0–255 onto 0–5 with the top bin one
    # value wider (215–255), which is where highlights live and where an extra
    # value costs nothing.
    step = 256 // PAL_LEVELS + 1
    binned = px // step
    idx = (binned[:, 0] * PAL_LEVELS + binned[:, 1]) * PAL_LEVELS + binned[:, 2]
    counts = np.bincount(idx, minlength=PAL_LEVELS ** 3)
    order = np.argsort(counts)[::-1][:PAL_COLOURS]

    total = float(px.shape[0])
    out = []
    for b in order:
        c = int(counts[b])
        if c <= 0:
            continue
        # The bin's own mean, not the bin's centre — a bin 43 values wide would
        # otherwise report a colour no pixel in the reel actually has.
        mean = px[idx == b].mean(axis=0)
        r, g, bl = (int(round(float(v))) for v in mean)
        out.append({"hex": f"#{r:02x}{g:02x}{bl:02x}", "rgb": [r, g, bl],
                    "share": round(c / total, 4)})
    return out


# ══════════════════════════════════════════════════════════════════════════
# loudness — EBU R128
# ══════════════════════════════════════════════════════════════════════════

def loudness(job, em: Emission) -> dict:
    """Integrated loudness, true peak, loudness range, silence and the curve.

    Skips — does not fail — on a reel with no audio track. `.check/long.mp4` is
    one, and a silent video is a perfectly ordinary thing for this archive to
    hold; `ffmpeg` reports it as *"Output file does not contain any stream"*,
    which is a true statement about a healthy file and must not be recorded as a
    pass that could not run.

    Every number here is ffmpeg's, computed to ITU-R BS.1770 with its two-stage
    gate. Nothing in this function computes loudness; it reads, bands and
    attributes. The one number that is this program's own is `silence_ratio`,
    and the threshold it depends on travels inside the claim rather than living
    only in a constant, because unlike LUFS that number is a choice.
    """
    facts = ff.probe(job.source)
    if not facts.get("has_audio"):
        raise SkipPass("this reel has no audio track")

    r = ff.loudness(job.source)
    if r.get("lufs") is None and not r["curve"]:
        raise RuntimeError("ebur128 measured nothing — " +
                           (r["meta"]["stderr"] or "no output")[:200])

    duration = float(facts.get("duration") or 0.0)
    lufs, peak, lra = r.get("lufs"), r.get("true_peak"), r.get("lra")

    if lufs is not None:
        # Bands are the platform norms, not invented: −14 LUFS is what Spotify,
        # YouTube and Instagram normalise to, and −23 is the EBU R128 broadcast
        # target. A reel above −11 will be turned down on delivery, which is the
        # thing worth telling an editor.
        em.claim(CH_AUDIO, "lufs", f"{lufs:.1f} LUFS integrated",
                 num=round(lufs, 2))
    if peak is not None:
        em.claim(CH_AUDIO, "true_peak",
                 f"{peak:.1f} dBTP" + (" — clipping" if peak > -1.0 else ""),
                 num=round(peak, 2))

    # Loudness range needs a determinate measurement, and LRA == 0 has two
    # completely different causes. EBU Tech 3342 computes it from 3-second
    # windows behind a relative gate, so on a short reel there may be only a
    # handful of gated blocks and the percentile spread between them collapses to
    # zero — a fact about the window, not the audio. On genuinely constant audio
    # it collapses to zero as well, and there the answer is correct.
    #
    # The momentary curve tells them apart, cheaply: it is ungated and sampled
    # every 100 ms. `.check/src.mp4` is 6.4 s and reads LRA 0.0, and its 61
    # momentary readings span 0.09 LU — that is a constant tone, so "compressed"
    # is true. A reel whose momentary loudness swings several LU while LRA reads
    # zero has been gated into silence, and banding that as "compressed" would
    # be reporting the gate's opinion as the reel's dynamics.
    spread = _sd([m for _t, m, _s in r["curve"]]) if r["curve"] else 0.0
    withheld = ""
    if lra is not None and (lra > 0.05 or spread <= 1.0):
        em.claim(CH_AUDIO, "dynamic_range",
                 _band(lra, (3.0, 8.0, 15.0),
                       ("compressed", "controlled", "dynamic", "wide")),
                 num=round(lra, 2))
    elif lra is not None:
        withheld = (f"LRA read {lra:.1f} while momentary loudness varied by "
                    f"{spread:.1f} LU — too few gated blocks to measure range")

    sil = [(a, duration if b is None else b) for a, b in r["silences"]]
    quiet = sum(max(0.0, b - a) for a, b in sil)
    if duration > 0:
        em.claim(CH_AUDIO, "silence_ratio",
                 f"{quiet / duration * 100:.0f}% below "
                 f"{ff.SILENCE_DB:g} dBFS for {ff.SILENCE_MIN:g}s+",
                 num=round(quiet / duration, 4))
    for i, (a, b) in enumerate(sil):
        em.claim(CH_AUDIO, "silence", f"{a:.2f}–{b:.2f}s",
                 num=round(b - a, 3), ordinal=i)

    if r["curve"]:
        # One row for the whole series rather than 600 rows of one reading
        # each: the curve is read as a shape — where the reel gets loud — and
        # never queried a point at a time. `num` carries the loudest momentary
        # reading so "which reels peak hardest" is still a range scan.
        em.claim(CH_AUDIO, "loudness_curve",
                 {"unit": "LUFS momentary", "hop": 0.1,
                  "points": [[t, m] for t, m, _s in r["curve"]]},
                 num=round(max(m for _t, m, _s in r["curve"]), 2))

    shots = job.shots()
    per_shot = 0
    for s in shots:
        t0, t1 = float(s["t0"]), float(s["t1"])
        inside = [m for t, m, _s in r["curve"] if t0 <= t < t1]
        if not inside:
            continue
        em.claim(CH_AUDIO, "shot_level", f"{_mean(inside):.1f} LUFS",
                 shot_idx=int(s["idx"]), num=round(_mean(inside), 2))
        per_shot += 1

    em.notes = {"lufs": lufs, "true_peak": peak, "lra": lra,
                "momentary_sd": round(spread, 2),
                "curve_points": len(r["curve"]), "silences": len(sil),
                "silent_seconds": round(quiet, 2), "per_shot": per_shot}
    if withheld:
        em.notes["lra_withheld"] = withheld
    return {}


# ══════════════════════════════════════════════════════════════════════════
# caption — what the uploader wrote
# ══════════════════════════════════════════════════════════════════════════

_HASHTAG = re.compile(r"(?<!\w)#([^\s#@.,!?;:()\[\]{}'\"]+)")
_MENTION = re.compile(r"(?<!\w)@([A-Za-z0-9._]{2,30})")


def caption(job, em: Emission) -> dict:
    """Turn the reel's own metadata into claims. No file is opened.

    The caption is evidence like any other and it is the *only* evidence that
    states intent — everything else in this package measures what the reel is,
    and this is the one channel that records what its maker said it was. It is
    also the cheapest: the row is already in `video_index`.

    Skips on a reel with no caption, no uploader and no engagement numbers,
    which is what a file dragged in from a local folder looks like. That is a
    reel with nothing to say, not a pass that failed — and marking it failed
    would put a permanent red row in the queue for every local import.
    """
    v = job.video or {}
    text = str(v.get("caption") or "").strip()
    uploader = str(v.get("creator") or "").strip()
    likes = v.get("likes")

    if not text and not uploader and not likes:
        raise SkipPass("this reel carries no caption, uploader or engagement "
                       "numbers")

    n = 0
    if text:
        em.claim(CH_CAPTION, "caption", text)
        n += 1
        for i, tag in enumerate(dict.fromkeys(
                m.group(1).lower() for m in _HASHTAG.finditer(text))):
            em.claim(CH_CAPTION, "hashtag", tag, ordinal=i)
            n += 1
        for i, who in enumerate(dict.fromkeys(
                m.group(1).lower() for m in _MENTION.finditer(text))):
            em.claim(CH_CAPTION, "mention", who, ordinal=i)
            n += 1
    if uploader:
        em.claim(CH_CAPTION, "uploader", uploader)
        n += 1
    # 0 likes and "likes unknown" are different facts and only the second is a
    # reason not to write the claim. `None` is unknown; 0 is a measurement.
    if likes is not None and str(likes) != "":
        em.claim(CH_CAPTION, "likes", f"{int(likes):,}", num=float(likes))
        n += 1

    em.notes = {"claims": n, "caption_chars": len(text),
                "uploader": uploader or "unknown"}
    return {}


# ══════════════════════════════════════════════════════════════════════════
# perframe — the per-frame series, collapsed into runs
# ══════════════════════════════════════════════════════════════════════════

# A frame whose 90th-percentile luminance is below this is black — not dark,
# black. 16 is the 8-bit video floor (studio-swing black), so this catches a
# frame that is genuinely empty rather than one that is merely underexposed.
BLACK_YHIGH = 24.0

# Two consecutive frames differing by less than this in mean absolute difference
# are the same frame. Not zero: a re-encode of a still image produces a mafd of
# a few hundredths from quantisation noise alone.
FREEZE_MAFD = 0.05


def perframe(job, em: Emission) -> dict:
    """Whole-reel summaries of the per-frame series, plus black and freeze spans.

    The declared kinds are mostly means — `brightness_mean`, `contrast_mean` —
    and those are one claim each. `black_frames` and `freeze_spans` are the two
    that are not summaries but *locations*, and they are what this pass is
    actually for: a black frame in the middle of a reel is an edit mistake, and
    a freeze is either a deliberate hold or a dropped-frame bug, and both are
    invisible in any average.

    `unique_frames` counts frames that differ from their predecessor. On a reel
    exported at 30 fps from a 15 fps source, half the frames are duplicates and
    this says so — which is worth knowing before concluding anything from a
    motion measurement.
    """
    rows = job.analysis()
    if not rows:
        raise SkipPass("no frames decoded for this reel")

    def col(name):
        key = "lavfi.signalstats." + name
        return [r[key] for r in rows if isinstance(r.get(key), float)]

    yavg, ylow, yhigh = col("YAVG"), col("YLOW"), col("YHIGH")
    satavg, uavg, vavg = col("SATAVG"), col("UAVG"), col("VAVG")
    # Same exclusion as `motion`, for the same reason and so the two agree: a
    # `motion_mean` here that included the cut frames would be a different
    # number from `motion_energy` for the same reel, and nothing in either
    # claim would say why.
    mafd = [v for _t, v in _mafd(job, rows)]

    if not yavg:
        raise SkipPass("signalstats reported nothing for this reel")

    for kind, val, scale in (
            ("brightness_mean", _mean(yavg), 255.0),
            ("brightness_min", min(yavg), 255.0),
            ("brightness_max", max(yavg), 255.0),
            ("contrast_mean", (_mean(yhigh) - _mean(ylow)) if yhigh and ylow
             else 0.0, 255.0),
            ("saturation_mean", _mean(satavg) if satavg else 0.0, 128.0),
            ("temperature_mean", (_mean(vavg) - _mean(uavg))
             if uavg and vavg else 0.0, 255.0),
            ("motion_mean", _mean(mafd) if mafd else 0.0, 1.0)):
        em.claim(CH_STYLE, kind, f"{val / scale:.4f}", num=round(val / scale, 5))

    # Sharpness would be the eighth. It is not emitted: every usable estimator
    # (variance of Laplacian, Tenengrad) needs the pixels, and `signalstats`
    # reports no gradient statistic at all. A "sharpness" derived from contrast
    # would rank a high-contrast blurry frame above a low-contrast crisp one.

    black = [r["t"] for r in rows
             if isinstance(r.get("lavfi.signalstats.YHIGH"), float)
             and r["lavfi.signalstats.YHIGH"] < BLACK_YHIGH]
    if black:
        em.claim(CH_STYLE, "black_frames",
                 f"{len(black)} frame(s), first at {black[0]:.2f}s",
                 num=float(len(black)))

    spans = _freezes(rows)
    for i, (a, b) in enumerate(spans):
        em.claim(CH_STYLE, "freeze_spans", f"{a:.2f}–{b:.2f}s",
                 num=round(b - a, 3), ordinal=i)
    if spans:
        longest = max(b - a for a, b in spans)
        em.claim(CH_STYLE, "longest_freeze", f"{longest:.2f}s",
                 num=round(longest, 3))

    # Counted over *every* frame, cut frames included — unlike the means above.
    # A frame across a cut is excluded from motion because it measures the edit
    # rather than the movement, but it unambiguously differs from the one before
    # it, and this claim is a count of frames that differ. Reusing the filtered
    # series here would quietly under-report by one per cut.
    every = [v for _t, v in _mafd(job, rows, drop_cuts=False)]
    moved = sum(1 for m in every if m > FREEZE_MAFD)
    em.claim(CH_STYLE, "unique_frames",
             f"{moved + 1} of {len(rows)} frames differ from the one before",
             num=float(moved + 1))

    em.notes = {"frames": len(rows), "black": len(black),
                "freezes": len(spans), "unique": moved + 1,
                "not_emitted": "sharpness_mean (needs pixel gradients)"}
    return {}


def _freezes(rows) -> list:
    """`[(t0, t1)]` for every run of frames that did not change.

    A run must be at least three frames — two identical frames is a duplicate,
    which `unique_frames` already counts, and calling it a freeze would report
    hundreds of them on any reel exported at a doubled frame rate.
    """
    out, start, last, run = [], None, None, 0
    for r in rows[1:]:
        m = r.get("lavfi.scd.mafd")
        if isinstance(m, float) and m <= FREEZE_MAFD:
            if start is None:
                start = last if last is not None else r["t"]
            run += 1
        else:
            if start is not None and run >= 3:
                out.append((round(start, 3), round(last if last is not None
                                                   else r["t"], 3)))
            start, run = None, 0
        last = r["t"]
    if start is not None and run >= 3 and last is not None:
        out.append((round(start, 3), round(last, 3)))
    return out


__all__ = ["cuts", "motion", "colour", "loudness", "caption", "perframe"]

# Every pass in this module returns `{}`. That is not an oversight — none of them
# writes a table of its own. They emit claims, `runners.to_rows` gives those
# claims their canonical shape, and the shard is the only way any of it reaches a
# database. The empty dict is the return slot for tables a pass produces that are
# not one of `Emission`'s shapes, which for a measurement pass is nothing.
