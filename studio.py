"""
studio.py — the archive read as craft.

Graph asks what is connected to what. Roadmap asks what order to watch things
in. This module asks the third question of the same tables: **how was this
made, and what do the ones that work have in common** — so that the next one
can be built from measurement instead of memory.

Three views, and not one of them calls a model.

* `deconstruct(key)` takes one reel apart. Cuts come from `shot`, evidence from
  `moments`, and the structure between them is found by change-point
  segmentation over the channel mix — the point where a reel stops being
  speech-over-footage and becomes text-on-screen is a real, locatable event,
  and it is found by minimising within-segment variance rather than by asking
  anything what it thinks the sections are.
* `patterns(scope)` reads many reels the same way and reports the
  distributions: how long, how fast, how much of it is talking, and which
  phrases carry the opening. Phrase weight is a log-odds ratio with an
  informative Dirichlet prior — the method Monroe, Colaresi and Quinn
  published for exactly this problem, because raw frequency ranks "the" first
  and a plain ratio ranks whatever was said once.
* `script_draft(scope)` turns that into a beat sheet. Every number in it is a
  median of real reels and every line of it cites the reel and timecode it was
  measured from. It writes no prose, because prose is the one part of this that
  a measurement cannot honestly supply.

**Nothing here is stored.** Everything is derived on read and cached in memory
against a fingerprint of the tables it read, so a scan or a re-index silently
invalidates it and no answer can outlive the data it came from. The two derived
tables Roadmap and Graph maintain are not touched at all.

Two honest limits, stated here rather than discovered later:

1. **The five slot names in `SLOTS` are a convention, not a finding.** Hook,
   Setup, Turn, Payoff, Close at fixed proportions of the runtime is the
   editorial vocabulary the beat sheet is written in. What is *measured* is
   what really occupies those proportions across the scope. A reel that opens
   with its payoff will still be read as having a hook, and the numbers will
   say so — the leading channel and the phrases will simply not look like a
   hook's.
2. **Lift is computed inside the scope**, hook against the rest of the same
   reels, not against the whole archive. That answers "what does the opening
   say that the body does not" in one pass over data already loaded. The
   archive-wide comparison — "what do these reels say that all the others do
   not" — needs a full token count of every reel and is deliberately left out
   rather than approximated.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import unicodedata

from atlas import search
from logger import vios_log as log

SUB = "STUDIO"

# ── The numbers this module is allowed to have opinions about ────────────────
# Each one is a reading decision, not a tuning knob, so each one gets a reason.

HOOK_S = 3.0          # A reel's opening. Three seconds is where the platforms
                      # themselves cut retention curves, and it is long enough
                      # to contain a sentence and short enough to exclude the
                      # second one.
SCOPE_CAP = 400       # Reels loaded per scope. Above this the distributions
                      # stop moving and the query starts being felt.
BINS = 96             # Timeline resolution. 96 bins over a 40 s reel is ~0.4 s
                      # per bin — finer than a cut, coarser than a frame.
MIN_SEG_BINS = 4      # No "section" shorter than this. Without a floor,
                      # segmentation finds every single-bin outlier and reports
                      # a reel as having eleven acts.
MAX_SECTIONS = 6      # Ceiling on discovered sections.
SPLIT_FLOOR = 0.06    # A split must remove at least 6% of the remaining cost
                      # to be worth naming. This is the elbow rule, applied to
                      # the same statistic being minimised rather than to a
                      # separate score.
GAP_S = 1.5           # Silence shorter than this is breathing, not a gap.
PHRASES = 18          # Phrases reported per comparison.
PRIOR_STRENGTH = 0.30 # α₀ for the Dirichlet prior in the log-odds test, as a
                      # fraction of the observed corpus size. Monroe et al.
                      # recommend an informative prior drawn from the corpus
                      # itself; this is that, scaled so the prior never
                      # outweighs the evidence.
MIN_TERM_N = 3        # A term must appear three times somewhere in the scope
                      # before it can be ranked. Below that, the z-score is
                      # measuring one person's verbal tic.
EXAMPLES = 3          # Real cited moments per beat.
NGRAM_MAX = 3         # Unigrams through trigrams. "how to" and "here's the
                      # thing" are hooks; "how" and "thing" are not.

# Positional slots. Read the module docstring: this tuple is the editorial
# vocabulary, and the only thing measured is what actually occupies these
# proportions of the runtime across the scope.
SLOTS: tuple[tuple[str, float, float], ...] = (
    ("Hook",   0.00, 0.12),
    ("Setup",  0.12, 0.35),
    ("Turn",   0.35, 0.60),
    ("Payoff", 0.60, 0.85),
    ("Close",  0.85, 1.00),
)

# The nine channels, in the order a reel is usually built rather than
# alphabetically, so a mix reads left to right as authored → captured → styled.
CHANNELS = ("narrative", "speech", "caption", "ocr",
            "visual", "concept", "style", "audio", "meta")

# Function words, removed before ranking. This list is English plus the
# romanised Hindi that turns up in the same sentence as it ("ye wala", "aur
# phir"), because that is how the archive's captions are actually written.
# Devanagari and South Indian scripts are deliberately *not* filtered: a
# half-finished stoplist in a script the list's author cannot read removes
# content words and leaves function words, which is worse than filtering
# nothing. The log-odds prior handles unfiltered function words correctly on
# its own — it ranks them near zero because they are equally common in both
# sides of the comparison. The stoplist is a readability shortcut, not the
# mechanism.
STOP = frozenset("""
a an the and or but if then than so because as of to in on at by for with from
into over under about after before while during is are was were be been being
am do does did doing done have has had having will would can could should may
might must shall this that these those there here it its it's i i'm i've you
your you're we we're they he she him her his hers them their our us me my mine
what which who whom whose when where why how all any both each few more most
other some such no nor not only own same too very just also even still yet
one two three four five get got go goes going make makes made really actually
basically literally like okay ok yeah yes right now new use used using thing
things lot lots kind sort want wants need needs know knows think thinks say
says said see sees look looks come comes take takes give gives put puts
hai hain tha thi the ho hota hoti hote hu hun ka ki ke ko se me mein par
aur ya to bhi hi na nahi nahin toh kya kyu kyun kaise kaisa jab tab ab
yeh ye woh wo iska uska mera tera apna hum tum aap unka jo koi kuch kuchh
bahut zyada thoda phir fir agar lekin matlab bas sab sabhi wala wali waise
""".split())

def _word_re() -> "re.Pattern":
    """A word pattern that does not shred Hindi.

    `[^\\W\\d_]+` looks script-agnostic and is not. Python's `\\w` is "alnum or
    underscore", and a Devanagari vowel sign — the ी in वीडियो — is a *combining
    mark*, which is not alnum. So the obvious pattern treats every matra as a
    word boundary and turns वीडियो into व, ड, य: six one-letter tokens where
    there was one word. Tamil, Telugu, Kannada and Malayalam all break the same
    way, and every one of them is in this archive.

    So a token here is one letter followed by any run of letters and combining
    marks. The mark set is collected from `unicodedata` over the blocks that can
    appear in this data — Latin diacritics, the Indic scripts, Arabic and Thai
    vowel signs, and the combining-mark blocks — rather than hardcoded, so it is
    right by construction instead of right by my transcription of a chart. It is
    ~4,000 codepoint lookups, once, at import."""
    spans = ((0x0300, 0x036F), (0x0483, 0x0489), (0x0591, 0x05BD), (0x0610, 0x061A),
             (0x064B, 0x0670), (0x0900, 0x0DFF), (0x0E00, 0x0F8F),
             (0x1AB0, 0x1AFF), (0x1DC0, 0x1DFF), (0x20D0, 0x20F0), (0xFE20, 0xFE2F))
    marks = "".join(chr(cp) for a, b in spans for cp in range(a, b + 1)
                    if unicodedata.category(chr(cp))[0] == "M")
    letter = r"[^\W\d_]"
    return re.compile(f"{letter}(?:{letter}|[{re.escape(marks)}])*", re.UNICODE)


_WORD = _word_re()


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace. Nothing else — no stemming, because a
    stemmer for one of these languages is a mangler for the other two."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _words(text: str) -> list[str]:
    """Word tokens, script-agnostic: any run of letters. `\\w` would keep digits
    and underscores, and OCR text is full of both."""
    return _WORD.findall(_norm(text))


def _wordcount(text: str) -> int:
    return len(_words(text))


def _grams(text: str, nmax: int = NGRAM_MAX) -> list[str]:
    """Unigrams..n-grams, with n-grams built from the *unfiltered* word
    sequence and then dropped only if every one of their words is a stopword.
    Filtering first would fuse "how to edit" into "edit", inventing a bigram
    nobody said."""
    ws = _words(text)
    out: list[str] = []
    for n in range(1, nmax + 1):
        for i in range(len(ws) - n + 1):
            g = ws[i:i + n]
            if all(w in STOP for w in g):
                continue
            if n == 1 and (len(g[0]) < 3 or g[0] in STOP):
                continue
            out.append(" ".join(g))
    return out


# ── Descriptive statistics ───────────────────────────────────────────────────
# Medians and percentiles rather than means wherever a number is going to be
# shown to a person, because one 90-minute file in a folder of 30-second reels
# moves a mean and does not move a median.

def _n(count: int, one: str, many: str = "") -> str:
    """`_n(1, "reel")` → "1 reel". The count and its noun, formatted together.

    `reel(s)` is unremarkable in a log line and wrong on screen, and every
    string in this module that used it is a `note` or a `why` that the interface
    renders verbatim — so "1 reel(s) matching “hook”" was visible in the
    product, twelve times over. Five of those also needed the verb, which no
    helper can do for you: "1 reel matches", not "1 reel match".
    """
    return f"{count} {one if count == 1 else many or one + 's'}"


def _pct(xs: list[float], p: float) -> float:
    """Linear-interpolated percentile on a sorted copy. `p` in 0..1.

    This is numpy's default (`linear`) and R's type 7, chosen because it is the
    one everybody's spreadsheet agrees with."""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return float(s[0])
    i = p * (len(s) - 1)
    lo = int(math.floor(i))
    hi = min(lo + 1, len(s) - 1)
    frac = i - lo
    return float(s[lo] * (1.0 - frac) + s[hi] * frac)


def _stats(xs: list[float]) -> dict:
    """The five-number summary plus dispersion.

    `cv` is the coefficient of variation (σ/μ), which is the honest way to
    compare spread between quantities of different size — a 2-second standard
    deviation means something different for shot length than for runtime. It is
    `None` when the mean is zero, because σ/0 is not a large number, it is not
    a number."""
    n = len(xs)
    if not n:
        return {"n": 0, "mean": None, "median": None, "p10": None, "p90": None,
                "min": None, "max": None, "sd": None, "cv": None}
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / n
    sd = math.sqrt(var)
    return {
        "n": n,
        "mean": round(mean, 3),
        "median": round(_pct(xs, 0.5), 3),
        "p10": round(_pct(xs, 0.10), 3),
        "p90": round(_pct(xs, 0.90), 3),
        "min": round(min(xs), 3),
        "max": round(max(xs), 3),
        "sd": round(sd, 3),
        "cv": round(sd / mean, 3) if mean else None,
    }


def _union(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Merge overlapping intervals. Needed because "how much of this reel is
    speech" is a question about covered seconds, and two overlapping speech
    moments do not cover twice the time."""
    if not spans:
        return []
    out: list[list[float]] = []
    for a, b in sorted(spans):
        if b <= a:
            b = a
        if out and a <= out[-1][1]:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _covered(spans: list[tuple[float, float]]) -> float:
    return round(sum(b - a for a, b in _union(spans)), 3)


def _gaps(spans: list[tuple[float, float]], duration: float,
          floor: float = GAP_S) -> list[dict]:
    """Stretches of the runtime with no evidence of the given kind at all.

    Reported because they are the most actionable thing in a deconstruction: a
    3-second hole with nothing on any channel is either a deliberate beat or a
    place where the pipeline failed, and the two look identical in a summary
    that only reports totals."""
    merged = _union([(a, b) for a, b in spans if b > a])
    out: list[dict] = []
    cur = 0.0
    for a, b in merged:
        if a - cur >= floor:
            out.append({"t0": round(cur, 2), "t1": round(a, 2),
                        "len": round(a - cur, 2)})
        cur = max(cur, b)
    if duration - cur >= floor:
        out.append({"t0": round(cur, 2), "t1": round(duration, 2),
                    "len": round(duration - cur, 2)})
    return out


# ── Which phrases belong to a group ──────────────────────────────────────────

def _lift(inside: dict[str, int], outside: dict[str, int],
          top: int = PHRASES, min_n: int = MIN_TERM_N) -> list[dict]:
    """Rank terms by how much more they belong to `inside` than to `outside`.

    Log-odds ratio with an informative Dirichlet prior, and its z-score, from
    Monroe, Colaresi & Quinn, *Fightin' Words: Lexical Feature Selection and
    Evaluation for Identifying the Content of Political Conflict* (Political
    Analysis 16:4, 2008). The three obvious alternatives all fail on this data,
    which is why the harder one is here:

    * **Raw frequency** ranks the stoplist. Even after filtering, it ranks
      whatever the longest reel talked about.
    * **A plain ratio** `p_in / p_out` ranks every term said once inside and
      never outside at infinity, so the top of the list is always typos and
      proper nouns.
    * **Log-likelihood / χ²** is better but has no variance term, so it cannot
      distinguish a term seen 4 times from one seen 400 times with the same
      ratio.

    The prior here is the pooled count of each term across both sides, scaled by
    `PRIOR_STRENGTH`. Monroe et al. draw the prior from a large background
    corpus; the pooled corpus is the standard substitute when the background
    *is* the data, and it has exactly the intended effect — a rare term's odds
    are shrunk toward the pooled average, so it must be both distinctive and
    repeated to rank.

    Returns `z` (the ranked statistic), the raw counts on both sides, and the
    per-thousand rates, so a reader can check the ranking against the evidence
    instead of trusting it."""
    n_in = sum(inside.values())
    n_out = sum(outside.values())
    if not n_in or not n_out:
        return []

    vocab = set(inside) | set(outside)
    pooled = {w: inside.get(w, 0) + outside.get(w, 0) for w in vocab}
    total = sum(pooled.values()) or 1
    a0 = PRIOR_STRENGTH * total
    rows: list[dict] = []
    for w in vocab:
        y_in, y_out = inside.get(w, 0), outside.get(w, 0)
        if y_in + y_out < min_n or not y_in:
            continue
        aw = PRIOR_STRENGTH * pooled[w]
        num_in = y_in + aw
        den_in = n_in + a0 - num_in
        num_out = y_out + aw
        den_out = n_out + a0 - num_out
        if den_in <= 0 or den_out <= 0:
            continue
        delta = math.log(num_in / den_in) - math.log(num_out / den_out)
        var = (1.0 / num_in) + (1.0 / num_out)
        z = delta / math.sqrt(var) if var > 0 else 0.0
        rows.append({
            "term": w,
            "z": round(z, 3),
            "log_odds": round(delta, 3),
            "n_in": y_in,
            "n_out": y_out,
            "per_k_in": round(1000.0 * y_in / n_in, 2),
            "per_k_out": round(1000.0 * y_out / n_out, 2),
        })
    rows.sort(key=lambda r: -r["z"])
    return rows[:top]


# ── Where the reel changes what it is doing ──────────────────────────────────

def _segment(mat: list[list[float]], max_k: int = MAX_SECTIONS,
             min_len: int = MIN_SEG_BINS,
             floor: float = SPLIT_FLOOR) -> list[tuple[int, int]]:
    """Find the change points in a sequence of channel-mix vectors.

    Binary segmentation (Scott & Knott, 1974) minimising within-segment sum of
    squared deviations from the segment mean — the same L2 criterion `ruptures`
    and every other change-point library uses by default. Each split is *exact*
    (every legal split point is evaluated); the sequence of splits is greedy,
    which is the standard trade and is more than adequate at 96 bins.

    Why not just threshold, or cluster? A threshold on "is this bin mostly
    speech" produces a new section every time one caption lands mid-sentence.
    Clustering (k-means over bins) ignores time entirely and will happily
    report the opening and the closing as one section because they look alike.
    Segmentation is the only one of the three that answers the question asked,
    which is *when* the reel changed.

    Splitting stops when the best remaining split removes less than `floor` of
    the original cost, so a reel that never changes channel returns exactly one
    section instead of being cut into `max_k` arbitrary pieces. Returns
    `[(start, stop), …]` as half-open bin indices."""
    n = len(mat)
    if n == 0:
        return []
    dim = len(mat[0])
    if n < 2 * min_len or dim == 0:
        return [(0, n)]

    # Prefix sums per channel, so any segment's cost is O(dim) instead of O(n).
    s1 = [[0.0] * (n + 1) for _ in range(dim)]
    s2 = [[0.0] * (n + 1) for _ in range(dim)]
    for i, row in enumerate(mat):
        for c in range(dim):
            v = row[c]
            s1[c][i + 1] = s1[c][i] + v
            s2[c][i + 1] = s2[c][i] + v * v

    def cost(i0: int, i1: int) -> float:
        m = i1 - i0
        if m <= 0:
            return 0.0
        tot = 0.0
        for c in range(dim):
            a = s1[c][i1] - s1[c][i0]
            b = s2[c][i1] - s2[c][i0]
            tot += b - (a * a) / m
        return max(0.0, tot)

    def best_split(i0: int, i1: int) -> tuple[float, int]:
        base = cost(i0, i1)
        gain, at = 0.0, -1
        for k in range(i0 + min_len, i1 - min_len + 1):
            g = base - (cost(i0, k) + cost(k, i1))
            if g > gain:
                gain, at = g, k
        return gain, at

    total = cost(0, n)
    segs = [(0, n)]
    if total <= 1e-9:
        return segs
    cands = {0: best_split(0, n)}
    while len(segs) < max_k:
        pick, best = -1, (0.0, -1)
        for idx, seg in enumerate(segs):
            g = cands.get(idx)
            if g is None:
                g = cands[idx] = best_split(*seg)
            if g[1] >= 0 and g[0] > best[0]:
                pick, best = idx, g
        if pick < 0 or best[0] < floor * total:
            break
        i0, i1 = segs[pick]
        segs[pick:pick + 1] = [(i0, best[1]), (best[1], i1)]
        cands = {}  # indices shifted; recompute lazily
    return segs


# ══════════════════════════════════════════════════════════════════════════
# CACHE
# ══════════════════════════════════════════════════════════════════════════
# Same shape as `roadmap`: an RLock, a dict, a fingerprint of the tables read,
# and a hard clear rather than an eviction policy — the entries are cheap to
# rebuild and a wrong one is expensive to notice.

_LOCK = threading.RLock()
_CACHE: dict = {}
CACHE_MAX = 24


def invalidate() -> None:
    with _LOCK:
        _CACHE.clear()


def _fingerprint(conn: sqlite3.Connection) -> str:
    """Cheap proof that nothing this module reads has moved.

    Counts on the three source tables. A re-scan adds rows, a re-index rewrites
    `moments`, and either changes this string — so a stale answer cannot be
    served. Row counts (not a checksum) because this runs on every request and
    the failure it guards against is bulk import, not in-place edits."""
    try:
        v = conn.execute("SELECT COUNT(*) FROM video_index").fetchone()[0]
        m = conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0]
        s = conn.execute("SELECT COUNT(*) FROM shot").fetchone()[0]
    except sqlite3.Error:
        return ""
    return f"{v}:{m}:{s}"


def _cached(key: tuple, build):
    with _LOCK:
        hit = _CACHE.get(key)
    if hit is not None:
        return hit
    out = build()
    with _LOCK:
        if len(_CACHE) >= CACHE_MAX:
            _CACHE.clear()
        _CACHE[key] = out
    return out


# ══════════════════════════════════════════════════════════════════════════
# READERS
# ══════════════════════════════════════════════════════════════════════════
# Every one of these is wrapped, because `shot` and `claim` arrive from evidence
# shards and a laptop that has imported bundles but no shards has the tables
# missing rather than empty. A missing table is a normal state here, not an
# error, and it must read as "no cuts recorded" rather than as a 500.

def _rows(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> list:
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        log(f"studio: {type(e).__name__} — {e} — on: {_norm(sql)[:70]}", "DEBUG")
        return []


def _sources(raw) -> dict[str, int]:
    """`video_index.sources` is a JSON object — `{"speech": 41, "ocr": 12}`.

    It reads like a comma-separated list and is not one, which is worth a named
    function: splitting it on commas yields the single string `{"meta": 1}` and
    the UI then prints that as though the archive had a channel by that name.
    `atlas/server.py:826` parses the same column the same way.
    """
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(got, dict):
        return {}
    return {str(k): int(v or 0) for k, v in got.items() if k}


def _video(conn: sqlite3.Connection, key: str) -> dict | None:
    r = _rows(conn, "SELECT video_key, title, caption, creator, category, "
                    "duration, width, height, fps, size_mb, likes, created_at, "
                    "poster, moment_count, sources, has_speech, has_narrative, "
                    "text_len FROM video_index WHERE video_key = ?", (key,))
    if not r:
        return None
    c = r[0]
    return {
        "video_key": c[0], "title": c[1] or c[0], "caption": c[2] or "",
        "creator": c[3] or "", "category": c[4] or "",
        "duration": float(c[5] or 0.0), "width": c[6], "height": c[7],
        "fps": float(c[8] or 0.0), "size_mb": float(c[9] or 0.0),
        "likes": c[10], "created_at": c[11], "poster": c[12] or "",
        "moment_count": int(c[13] or 0),
        "sources": _sources(c[14]),
        "has_speech": bool(c[15]), "has_narrative": bool(c[16]),
        "text_len": int(c[17] or 0),
    }


def _columns(conn: sqlite3.Connection, table: str) -> set:
    """The column names a table actually has, or an empty set if it has none.

    Studio is a pure reader over tables it does not create, and two of them are
    named differently depending on what created them. A database built by shard
    replay — the only path that exists on a fresh machine — names the shot
    boundary confidence `score` and the claim's text `value`, and carries no
    `t0`/`t1` on `claim` at all; the copy on this laptop was created by an early
    fixture that called them `scene_score` and `name` and did have times.
    `ensure_schema` widens but never renames, so both shapes persist and a query
    written against either one silently reads nothing from the other: `_rows`
    logs the `OperationalError` at DEBUG and returns `[]`, which the payload then
    reports as *this reel has no shots and no entities* over a table holding
    three shots and thirty-seven claims. Probing costs one PRAGMA per call and is
    the difference between a deconstruction and a blank one.

    Not cached: the first shard to reach a table widens it mid-process, so a set
    memoised at import would be the pre-widening answer for the life of the app.
    """
    try:
        return {r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except sqlite3.Error:
        return set()


def _shots(conn: sqlite3.Connection, key: str) -> list[tuple[float, float, float]]:
    """`(t0, t1, score)` in cut order, with the zero-length and inverted
    rows that a detector occasionally emits dropped rather than carried into a
    mean shot length. The confidence column is whichever of `score` /
    `scene_score` this database has — see `_columns`."""
    cols = _columns(conn, "shot")
    sc_col = "score" if "score" in cols else ("scene_score" if "scene_score" in cols else None)
    if not cols:
        return []
    pick = f'"{sc_col}"' if sc_col else "NULL"
    out = []
    for t0, t1, sc in _rows(conn, f"SELECT t0, t1, {pick} FROM shot "
                                  "WHERE video_key = ? ORDER BY idx", (key,)):
        a, b = float(t0 or 0.0), float(t1 or 0.0)
        if b > a:
            out.append((a, b, float(sc or 0.0)))
    return out


def _moments(conn: sqlite3.Connection, key: str) -> list[dict]:
    """Every piece of evidence for one reel, in time order.

    `t_end` is coalesced to `t_start` because `meta` and `concept` moments
    describe the whole file and carry no span; treating their absent end as 0
    would give them negative length and silently subtract from coverage.

    `timed` is the distinction the rest of this file rests on, and it is here
    because a NULL `t_start` and a `t_start` of zero are not the same claim. A
    caption belongs to the whole reel; a cut at 0.0s happens at the start. Read
    as the same number, an untimed channel reports `first_at: 0.0` — evidence at
    the first frame, which nothing measured — and, worse, `_timeline` names it as
    a present channel, so a reel whose occupancy matrix is entirely zero still
    comes back with four channels and one section spanning all of it. That was
    two of the three reels on this machine: a fabricated section, no gap
    reported, and `notes` silent, because the note that says *evidence exists but
    carries no usable timing* was guarded on the channel list being empty and the
    channel list was built from every source, timed or not.
    """
    out = []
    for t0, t1, src, tbl, w, txt in _rows(
            conn, "SELECT t_start, COALESCE(t_end, t_start), source, src_table,"
                  " weight, text FROM moments WHERE video_key = ? "
                  "ORDER BY t_start", (key,)):
        a = float(t0 or 0.0)
        b = max(a, float(t1 or a))
        out.append({"t_start": round(a, 3), "t_end": round(b, 3),
                    "timed": t0 is not None,
                    "source": src or "meta", "src_table": tbl or "",
                    "weight": float(w or 0.0), "text": txt or ""})
    return out


def _claims(conn: sqlite3.Connection, key: str) -> list[dict]:
    """Named entities the pipeline was willing to assert.

    The text column is `value` on a database built by shard replay and `name` on
    the early fixture's. Times are three-way: a claim may carry its own `t0`/`t1`,
    or be scoped to a shot by `shot_idx` with the seconds living on `shot`, or
    carry neither because it is about the whole reel. All three arrive in one
    table, so the resolution is per row and not per schema — `COALESCE` over a
    LEFT JOIN, exactly what `reflect.time_link` does for the search index.

    Choosing the branch off the *columns* was a bug the moment the shard header
    began declaring `t0`/`t1` on every `claim` table it creates: the table has the
    columns and most rows leave them NULL, so *"it has t0, use t0"* read a per-shot
    claim as untimed and dropped the join that was the whole point of `shot_idx`.
    That join is what keeps a claim clickable on the timeline; without a time the
    button below it would seek to zero for everything. A claim with no times and no
    shot is returned with `t0`/`t1` as null, which the interface renders as
    *whole reel* rather than as second zero.

    The end is borrowed only when the start was, so a point claim that happens to
    fall inside a shot is not stretched across it.

    Note for anyone who profiles this: there is no index on `claim(video_key)`
    (only `claim(uid)` is unique), so this is a scan. It is one scan of a small
    table for one reel, which is why it has not been worth adding an index for;
    it would be worth it if this were ever called in a loop over the archive."""
    cols = _columns(conn, "claim")
    txt = "value" if "value" in cols else ("name" if "name" in cols else None)
    if txt is None:
        return []
    own = "t0" in cols and "t1" in cols
    link = "shot_idx" in cols and "idx" in _columns(conn, "shot")
    if own and link:
        sql = (f'SELECT c.kind, c."{txt}", c.confidence, '
               "  COALESCE(c.t0, s.t0), "
               "  CASE WHEN c.t0 IS NULL THEN s.t1 ELSE c.t1 END "
               "FROM claim c LEFT JOIN shot s "
               "  ON s.video_key = c.video_key AND s.idx = c.shot_idx "
               "WHERE c.video_key = ? "
               "ORDER BY COALESCE(c.t0, s.t0, 0), c.ordinal")
    elif own:
        sql = (f'SELECT kind, "{txt}", confidence, t0, t1 FROM claim '
               "WHERE video_key = ? ORDER BY COALESCE(t0, 0)")
    elif link:
        sql = (f'SELECT c.kind, c."{txt}", c.confidence, s.t0, s.t1 '
               "FROM claim c LEFT JOIN shot s "
               "  ON s.video_key = c.video_key AND s.idx = c.shot_idx "
               "WHERE c.video_key = ? ORDER BY COALESCE(s.t0, 0), c.ordinal")
    else:
        sql = (f'SELECT kind, "{txt}", confidence, NULL, NULL FROM claim '
               "WHERE video_key = ? ORDER BY ordinal")
    out = []
    for kind, name, cf, t0, t1 in _rows(conn, sql, (key,)):
        out.append({"kind": kind or "", "name": name or "",
                    "confidence": round(float(cf or 0.0), 3),
                    "t0": None if t0 is None else round(float(t0), 2),
                    "t1": None if t1 is None else round(float(t1), 2)})
    return out


# ══════════════════════════════════════════════════════════════════════════
# ONE REEL
# ══════════════════════════════════════════════════════════════════════════

def _pacing(shots: list[tuple[float, float, float]], duration: float) -> dict:
    """Cut rhythm.

    `cuts_per_min` is the number everyone quotes, but on its own it says
    nothing about *feel*: a reel that alternates a 4-second hold with eight
    half-second cuts has the same cut rate as one that cuts evenly every
    second, and the two are not the same edit. `regularity` separates them. It
    is `1 − CV` of shot length, clamped to 0–1 — a metronome scores 1, and a
    reel built out of one long hold and a burst scores near 0.

    `longest_hold` is reported with its timecode because it is almost always
    the most important second in the reel, and `p90` because the top decile of
    shot length is what a person means by "it breathes".

    `coverage` — detected shot seconds over runtime — can land slightly above 1
    when a detector's last boundary runs past the recorded duration. It is left
    unclamped for the same reason channel `share` is: a coverage of 1.05 is a
    fact about the shot table worth seeing, and clamping it to 1.00 would hide
    the only evidence that the two disagree."""
    if not shots:
        return {"shots": 0, "cuts": 0, "cuts_per_min": None, "regularity": None,
                "shot_len": _stats([]), "longest_hold": None,
                "covered_s": 0.0, "coverage": None}
    lens = [b - a for a, b, _ in shots]
    st = _stats(lens)
    cuts = max(0, len(shots) - 1)
    span = duration if duration > 0 else max(b for _, b, _ in shots)
    longest = max(shots, key=lambda s: s[1] - s[0])
    cv = st["cv"]
    return {
        "shots": len(shots),
        "cuts": cuts,
        "cuts_per_min": round(cuts / (span / 60.0), 2) if span > 0 else None,
        "regularity": round(max(0.0, min(1.0, 1.0 - cv)), 3) if cv is not None else None,
        "shot_len": st,
        "longest_hold": {"t0": round(longest[0], 2), "t1": round(longest[1], 2),
                         "len": round(longest[1] - longest[0], 2)},
        "covered_s": _covered([(a, b) for a, b, _ in shots]),
        "coverage": round(_covered([(a, b) for a, b, _ in shots]) / span, 3) if span > 0 else None,
    }


def _channels(moments: list[dict], duration: float) -> list[dict]:
    """Per-channel presence, in `CHANNELS` order.

    `share` is covered seconds over runtime, computed on merged intervals — two
    overlapping speech moments cover the time once. It can still exceed 1 for
    `meta` and `concept`, whose moments describe the whole file; that is not a
    bug to clamp away, it is the reason `covered_s` is printed next to it.

    `first_at`/`last_at` are null for a channel none of whose moments are placed.
    A caption is about the reel, not about its first frame, and `0.0` there reads
    as a measurement that was never made."""
    by: dict[str, list[dict]] = {}
    for m in moments:
        by.setdefault(m["source"], []).append(m)
    order = list(CHANNELS) + sorted(set(by) - set(CHANNELS))
    out = []
    for src in order:
        ms = by.get(src)
        if not ms:
            continue
        placed = [m for m in ms if m["timed"]]
        spans = [(m["t_start"], m["t_end"]) for m in placed]
        cov = _covered(spans)
        words = sum(_wordcount(m["text"]) for m in ms)
        chars = sum(len(m["text"]) for m in ms)
        out.append({
            "source": src,
            "moments": len(ms),
            "covered_s": cov,
            "share": round(cov / duration, 3) if duration > 0 else None,
            "first_at": round(min(m["t_start"] for m in placed), 2) if placed else None,
            "last_at": round(max(m["t_end"] for m in placed), 2) if placed else None,
            "words": words,
            "chars": chars,
            "weight": round(sum(m["weight"] for m in ms), 2),
        })
    return out


def _timeline(moments: list[dict], duration: float,
              bins: int = BINS) -> tuple[list[str], list[list[float]], float]:
    """Bin the reel into a channels × bins occupancy matrix.

    A bin's value for a channel is the fraction of that bin covered by moments
    of that channel, so it is 0–1 and comparable across channels and across
    reels of different length. Intervals are merged per channel first, for the
    same reason `share` merges them.

    Returns the channel order used, the matrix as `[bin][channel]` (the shape
    `_segment` wants), and the bin width in seconds."""
    if duration <= 0 or bins <= 0:
        return [], [], 0.0
    w = duration / bins
    # Only channels with a placed moment. A channel that is present in the
    # evidence but nowhere on the clock has no row to draw: every bin of it would
    # be zero, and a zero row reads as *this channel was measured and found
    # absent here* rather than as *this channel was never placed*.
    timed = [m for m in moments if m["timed"]]
    present = [c for c in CHANNELS if any(m["source"] == c for m in timed)]
    present += sorted({m["source"] for m in timed} - set(CHANNELS))
    if not present:
        return [], [[] for _ in range(bins)], w
    mat = [[0.0] * len(present) for _ in range(bins)]
    for ci, src in enumerate(present):
        spans = _union([(m["t_start"], m["t_end"])
                        for m in timed if m["source"] == src])
        for a, b in spans:
            i0 = max(0, min(bins - 1, int(a / w)))
            i1 = max(0, min(bins - 1, int(math.ceil(b / w)) - 1))
            for i in range(i0, i1 + 1):
                lo, hi = i * w, (i + 1) * w
                ov = min(b, hi) - max(a, lo)
                if ov > 0:
                    mat[i][ci] = min(1.0, mat[i][ci] + ov / w)
    return present, mat, w


def _label(mix: dict[str, float]) -> str:
    """Name a section by what leads it, and say so in the name.

    Deliberately not a semantic label. "speech-led" is a measurement; "the
    explanation" would be a guess dressed as one. Where two channels are within
    a fifth of each other both are named, because a talking head with burned-in
    captions is genuinely two things at once."""
    live = sorted(((v, k) for k, v in mix.items() if v > 0.02), reverse=True)
    if not live:
        return "silent"
    top = live[0]
    close = [k for v, k in live[1:3] if v >= top[0] * 0.8]
    return " + ".join([top[1]] + close) + "-led"


def _sections(chans: list[str], mat: list[list[float]], bin_s: float) -> list[dict]:
    """Run the segmentation and describe each piece in the reel's own terms."""
    if not mat or not chans:
        return []
    segs = _segment(mat)
    out = []
    for n, (i0, i1) in enumerate(segs):
        m = max(1, i1 - i0)
        mix = {c: round(sum(mat[i][ci] for i in range(i0, i1)) / m, 3)
               for ci, c in enumerate(chans)}
        mix = {k: v for k, v in mix.items() if v > 0}
        out.append({
            "n": n + 1,
            "bin0": i0, "bin1": i1,
            "t0": round(i0 * bin_s, 2), "t1": round(i1 * bin_s, 2),
            "len": round((i1 - i0) * bin_s, 2),
            "share": round((i1 - i0) / len(mat), 3),
            "mix": mix,
            "label": _label(mix),
            "lead": (max(mix, key=mix.get) if mix else "silent"),
        })
    return out


def _hook(moments: list[dict], shots: list[tuple[float, float, float]],
          duration: float) -> dict:
    """The first `HOOK_S` seconds, described.

    A moment counts as being in the hook if it *overlaps* the window, not if it
    starts inside it — a caption that begins at 2.8s and runs to 6s is part of
    the opening, and requiring `t_start < 3` would drop exactly the moments that
    carry an opening across into the body.

    `first_frame_channels` is separate and stricter: what is on screen at t=0.
    That is the question a person actually asks about a hook, and it is not the
    same as what happens in the first three seconds.

    `silent_open` is null, not false, when no moment on this reel is placed on the
    clock. "This reel opens on nothing" is a finding; drawing it from a timeline
    that was never populated is the same sentence with none of the measurement
    behind it, and the interface prints *unknown* rather than *no*."""
    win = min(HOOK_S, duration) if duration > 0 else HOOK_S
    placed = [m for m in moments if m["timed"]]
    inside = [m for m in placed if m["t_start"] < win and m["t_end"] > 0]
    at_zero = sorted({m["source"] for m in placed
                      if m["t_start"] <= 0.25 and m["t_end"] > 0.0})
    cuts = sum(1 for a, _, _ in shots if 0 < a < win)
    texts = [m for m in inside if m["text"].strip()
             and m["source"] in ("speech", "caption", "ocr", "narrative")]
    words = sum(_wordcount(m["text"]) for m in texts)
    first_speech = next((m["t_start"] for m in moments
                         if m["source"] == "speech" and m["timed"]), None)
    return {
        "window_s": round(win, 2),
        "moments": len(inside),
        "channels": sorted({m["source"] for m in inside}),
        "first_frame_channels": at_zero,
        "cuts": cuts,
        "words": words,
        "words_per_s": round(words / win, 2) if win > 0 else None,
        "first_speech_at": (round(float(first_speech), 2)
                            if first_speech is not None else None),
        "silent_open": (not at_zero) if placed else None,
        "text": [{"source": m["source"], "t": round(m["t_start"], 2),
                  "text": m["text"][:240]} for m in texts[:6]],
    }


def deconstruct(conn: sqlite3.Connection, key: str) -> dict:
    """Take one reel apart.

    Returns `{"ok": False, "error": …}` for a key that is not indexed, and a
    complete answer with empty parts for one that is indexed but has no
    evidence yet — those are different states and the UI needs to tell them
    apart. `notes` carries, in plain sentences, every reason a section of the
    answer is thin, so that "no cuts" reads as "shots were never detected for
    this reel" instead of as "this reel has no cuts"."""
    key = (key or "").strip()
    if not key:
        return {"ok": False, "error": "No video key given."}
    vid = _video(conn, key)
    if not vid:
        return {"ok": False, "error": f"“{key}” is not in the index."}

    shots = _shots(conn, key)
    moments = _moments(conn, key)
    claims = _claims(conn, key)

    # The recorded duration is authoritative, but a reel imported from a shard
    # can carry evidence past a duration of 0 — in that case the evidence is the
    # only measurement there is, so it sets the runtime and says so.
    duration = vid["duration"]
    dur_from = "index"
    if duration <= 0:
        ends = [b for _, b, _ in shots] + [m["t_end"] for m in moments]
        duration = max(ends) if ends else 0.0
        dur_from = "evidence" if duration > 0 else "unknown"

    chans, mat, bin_s = _timeline(moments, duration)
    notes: list[str] = []
    if not shots:
        notes.append("No shot boundaries have been detected for this reel, so "
                     "pacing is blank. Shots arrive with an evidence shard.")
    if not moments:
        notes.append("No moments are indexed for this reel, so there is nothing "
                     "to segment. Run the engine over it first.")
    if dur_from == "evidence":
        notes.append(f"The index records no duration; {duration:.1f}s is the "
                     f"furthest point any evidence reaches.")
    if dur_from == "unknown":
        notes.append("Neither a duration nor any timed evidence exists for this "
                     "reel, so every time-based number is blank.")
    if len(moments) and not chans:
        notes.append(f"{_n(len(moments), 'moment')} exist for this reel but not "
                     "one of them is placed on the clock, so the timeline, the "
                     "sections and the hook are all blank. Evidence is timed by "
                     "the frame it was read from or the shot it belongs to; when "
                     "no pass has written either, the text is searchable but not "
                     "seekable.")

    speech = [m for m in moments if m["source"] == "speech"]
    ocr = [m for m in moments if m["source"] == "ocr"]
    words = sum(_wordcount(m["text"]) for m in speech)
    return {
        "ok": True,
        "video": vid,
        "duration": round(duration, 2),
        "duration_from": dur_from,
        "pacing": _pacing(shots, duration),
        "channels": _channels(moments, duration),
        "timeline": {"channels": chans, "bins": len(mat), "bin_s": round(bin_s, 3),
                     "matrix": mat},
        "sections": _sections(chans, mat, bin_s),
        "hook": _hook(moments, shots, duration),
        "gaps": _gaps([(m["t_start"], m["t_end"])
                       for m in moments if m["timed"]], duration),
        "claims": claims,
        "density": {
            "words": words,
            "words_per_s": round(words / duration, 2) if duration > 0 else None,
            "ocr_chars_per_s": (round(sum(len(m["text"]) for m in ocr) / duration, 2)
                                if duration > 0 else None),
            "moments_per_min": (round(len(moments) / (duration / 60.0), 1)
                                if duration > 0 else None),
            "channels_used": len({m["source"] for m in moments}),
        },
        "moments": len(moments),
        "notes": notes,
    }


# ══════════════════════════════════════════════════════════════════════════
# SCOPE
# ══════════════════════════════════════════════════════════════════════════

def _chunks(xs: list, n: int = 300):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _archive_count(conn: sqlite3.Connection) -> int:
    r = _rows(conn, "SELECT COUNT(*) FROM video_index")
    return int(r[0][0]) if r else 0


def _scope(conn: sqlite3.Connection, goal: str = "", creator: str = "",
           category: str = "") -> dict:
    """Which reels the patterns are drawn from, resolved to actual keys.

    Follows `roadmap._scope` exactly, including its fallback: a goal matching
    fewer than three reels plans over the whole archive and says so, because a
    distribution over two reels is two numbers with a line through them.

    Unlike Roadmap, this also accepts a creator or a category on their own,
    since "how do *my* reels open" is the most common thing to ask this module
    and it is not a search query. When a goal and a filter are both given the
    filter is passed into the search rather than applied afterwards, so the
    scores stay the search's own."""
    goal = (goal or "").strip()
    creator = (creator or "").strip()
    category = (category or "").strip()
    total = _archive_count(conn)
    weights: dict[str, float] = {}
    keys: list[str] = []
    mode, note = "archive", ""

    if goal:
        try:
            found = search.search(conn, goal, limit=SCOPE_CAP, candidates=3000,
                                  creator=creator or None,
                                  category=category or None)
            for r in (found.get("results") or []):
                k = str(r.get("video_key") or "")
                if k:
                    keys.append(k)
                    weights[k] = float(r.get("score") or 0.0) or 1.0
            if len(keys) >= 3:
                more = int(found.get("total") or 0) > len(keys)
                mode = "goal"
                note = (f"{_n(len(keys), 'reel')} matching “{goal}”"
                        + (f" — the strongest {SCOPE_CAP} of more" if more else ""))
            else:
                note = (f"only {_n(len(keys), 'reel')} "
                        f"{'matches' if len(keys) == 1 else 'match'} “{goal}” — "
                        f"reading the whole archive instead, because a "
                        f"distribution over {_n(len(keys), 'reel')} would not "
                        f"mean anything")
                keys, weights = [], {}
        except Exception as e:                                    # noqa: BLE001
            log(f"studio: scope search failed — {type(e).__name__}: {e}", "WARN")
            note = (f"the search for “{goal}” failed ({type(e).__name__}) — "
                    f"reading the whole archive instead")

    if not keys:
        where, args = [], []
        if creator:
            where.append("creator = ?")
            args.append(creator)
        if category:
            where.append("category = ?")
            args.append(category)
        sql = ("SELECT video_key FROM video_index "
               + (f"WHERE {' AND '.join(where)} " if where else "")
               + "ORDER BY moment_count DESC, duration DESC LIMIT ?")
        keys = [r[0] for r in _rows(conn, sql, tuple(args) + (SCOPE_CAP,))]
        if not note:
            if creator or category:
                mode = "filter"
                which = " and ".join(filter(None, [f"creator “{creator}”" if creator else "",
                                                   f"category “{category}”" if category else ""]))
                note = f"{_n(len(keys), 'reel')} with {which}"
            else:
                note = (f"all {_n(len(keys), 'indexed reel')}" if len(keys) >= total
                        else f"the {len(keys)} most-indexed of "
                             f"{_n(total, 'reel')}")

    return {"mode": mode, "goal": goal, "creator": creator,
            "category": category, "keys": keys, "weights": weights,
            "note": note, "archive": total}


def _load(conn: sqlite3.Connection, keys: list[str]) -> dict[str, dict]:
    """Load every reel in the scope, once, in three queries per chunk.

    One pass, shared by `patterns` and `script_draft`, because both need the
    same three things (runtime, cuts, timed text) and loading them twice for one
    screen would be the slowest thing in the module. Text is loaded in full: a
    `substr()` in the query would truncate mid-word and quietly corrupt every
    word count and every phrase built from it."""
    out: dict[str, dict] = {}
    if not keys:
        return out
    for chunk in _chunks(keys):
        qs = ",".join("?" * len(chunk))
        for k, title, creator, cat, dur, mc, hs, tl, likes in _rows(
                conn, f"SELECT video_key, title, creator, category, duration, "
                      f"moment_count, has_speech, text_len, likes "
                      f"FROM video_index WHERE video_key IN ({qs})", tuple(chunk)):
            out[k] = {"video_key": k, "title": title or k, "creator": creator or "",
                      "category": cat or "", "duration": float(dur or 0.0),
                      "moment_count": int(mc or 0), "has_speech": bool(hs),
                      "text_len": int(tl or 0), "likes": likes,
                      "shots": [], "moments": []}
        for k, t0, t1 in _rows(
                conn, f"SELECT video_key, t0, t1 FROM shot "
                      f"WHERE video_key IN ({qs}) ORDER BY video_key, idx",
                tuple(chunk)):
            v = out.get(k)
            if v is not None and float(t1 or 0) > float(t0 or 0):
                v["shots"].append((float(t0), float(t1)))
        for k, a, b, src, w, txt in _rows(
                conn, f"SELECT video_key, t_start, COALESCE(t_end, t_start), "
                      f"source, weight, text FROM moments "
                      f"WHERE video_key IN ({qs}) ORDER BY video_key, t_start",
                tuple(chunk)):
            v = out.get(k)
            if v is None:
                continue
            # `timed` for the same reason `_moments` carries it: an untimed
            # moment is about the whole reel, and the hook aggregate below splits
            # text into hook and rest by comparing `t_start` to the window. Read
            # as zero, every caption in the archive counts as hook language.
            placed = a is not None
            a = float(a or 0.0)
            v["moments"].append({"t_start": a, "t_end": max(a, float(b or a)),
                                 "timed": placed, "source": src or "meta",
                                 "weight": float(w or 0.0), "text": txt or ""})
    # A reel with evidence but no recorded duration would divide by zero in every
    # rate below, so it gets the same evidence-derived runtime `deconstruct` uses.
    for v in out.values():
        if v["duration"] <= 0:
            ends = [b for _, b in v["shots"]] + [m["t_end"] for m in v["moments"]]
            v["duration"] = max(ends) if ends else 0.0
    return out


# ══════════════════════════════════════════════════════════════════════════
# MANY REELS
# ══════════════════════════════════════════════════════════════════════════

def _row_features(v: dict) -> dict:
    """One reel reduced to the numbers a distribution can be built from.

    Rates are `None` rather than 0 when their denominator is missing, and every
    aggregate below drops `None`. A reel with no detected shots must not be
    counted as a reel with a cut rate of zero — that is the difference between
    "this scope averages 22 cuts/min" and "this scope averages 9 because a third
    of it was never shot-detected"."""
    dur = v["duration"]
    shots, ms = v["shots"], v["moments"]
    lens = [b - a for a, b in shots]
    speech = [m for m in ms if m["source"] == "speech"]
    words = sum(_wordcount(m["text"]) for m in speech)
    per_src = {}
    for m in ms:
        per_src.setdefault(m["source"], []).append((m["t_start"], m["t_end"]))
    shares = {s: (round(_covered(sp) / dur, 3) if dur > 0 else None)
              for s, sp in per_src.items()}
    return {
        "video_key": v["video_key"], "title": v["title"],
        "creator": v["creator"], "category": v["category"],
        "duration": dur if dur > 0 else None,
        "shots": len(shots),
        "cuts_per_min": (round(max(0, len(shots) - 1) / (dur / 60.0), 2)
                         if shots and dur > 0 else None),
        "shot_len": round(sum(lens) / len(lens), 3) if lens else None,
        "regularity": (lambda cv: round(max(0.0, min(1.0, 1.0 - cv)), 3)
                       if cv is not None else None)(_stats(lens)["cv"]),
        "moments": len(ms),
        "moments_per_min": (round(len(ms) / (dur / 60.0), 1)
                            if ms and dur > 0 else None),
        "words": words,
        "words_per_s": round(words / dur, 2) if dur > 0 else None,
        "speech_share": shares.get("speech"),
        "shares": shares,
        "channels": sorted(per_src),
    }


def _dist(rows: list[dict], field: str) -> dict:
    return _stats([r[field] for r in rows if r.get(field) is not None])


def _tertile_bands(xs: list[float], names: tuple[str, str, str]) -> dict:
    """Cut a measured quantity into three named bands at its own 33rd and 67th
    percentiles.

    Tertiles of the scope, not fixed thresholds: "fast" means fast *for this
    archive*, which is the only definition available without a reference corpus
    of somebody else's reels. When the scope is too small or too uniform to have
    distinct cut points the bands collapse and the caller is told, rather than
    being handed three buckets two of which are empty."""
    if len(xs) < 6:
        return {"ok": False, "why": f"only {_n(len(xs), 'reel')} "
                                   f"{'carries' if len(xs) == 1 else 'carry'} this "
                                   f"measure — six is the floor for thirds",
                "names": list(names), "edges": []}
    lo, hi = _pct(xs, 1 / 3), _pct(xs, 2 / 3)
    if not (lo < hi):
        return {"ok": False, "why": "this measure is too uniform across the scope "
                                   "to cut into thirds",
                "names": list(names), "edges": [round(lo, 2), round(hi, 2)]}
    return {"ok": True, "names": list(names), "edges": [round(lo, 2), round(hi, 2)],
            "why": ""}


def _band_of(x: float | None, band: dict) -> str | None:
    if x is None or not band.get("ok"):
        return None
    lo, hi = band["edges"]
    return band["names"][0] if x < lo else (band["names"][1] if x < hi else band["names"][2])


def _hook_agg(loaded: dict[str, dict]) -> dict:
    """What the openings of a scope have in common.

    Rates are over the reels that could answer, and the denominator is reported
    with every one of them. "62% open on a caption" means 62% of the reels that
    have any evidence at t=0, and stating the denominator is the difference
    between that and a claim about the whole scope.

    Every number here is a statement about a timeline, so a reel with no moment
    placed on one is not in the denominator. Counting it made the two untimed
    reels on this machine read as *67% of this scope opens silent*, which is a
    finding about the openings of reels whose openings were never looked at. The
    count that dropped out is returned as `untimed` so the caller can say so."""
    opens: dict[str, int] = {}
    leads: dict[str, int] = {}
    words, cuts, first_speech = [], [], []
    silent, answerable, untimed = 0, 0, 0
    hook_terms: dict[str, int] = {}
    rest_terms: dict[str, int] = {}
    for v in loaded.values():
        dur = v["duration"]
        if dur <= 0 or not v["moments"]:
            continue
        if not any(m["timed"] for m in v["moments"]):
            untimed += 1
            continue
        answerable += 1
        win = min(HOOK_S, dur)
        at_zero = sorted({m["source"] for m in v["moments"]
                          if m["t_start"] <= 0.25 and m["t_end"] > 0.0})
        if at_zero:
            for s in at_zero:
                opens[s] = opens.get(s, 0) + 1
        else:
            silent += 1
        cov: dict[str, float] = {}
        for m in v["moments"]:
            if m["t_start"] < win:
                ov = min(m["t_end"], win) - m["t_start"]
                if ov > 0:
                    cov[m["source"]] = cov.get(m["source"], 0.0) + ov
        if cov:
            lead = max(cov, key=cov.get)
            leads[lead] = leads.get(lead, 0) + 1
        w = 0
        for m in v["moments"]:
            if not m["text"].strip():
                continue
            # An untimed moment is on neither side of the window. Its text
            # describes the whole reel, so calling it hook language overstates the
            # opening and calling it body language understates it; the lift below
            # is a comparison between two halves of a timeline, and a row with no
            # place on that timeline is not evidence about either half.
            if not m["timed"]:
                continue
            grams = _grams(m["text"])
            if m["t_start"] < win:
                w += _wordcount(m["text"])
                for g in grams:
                    hook_terms[g] = hook_terms.get(g, 0) + 1
            else:
                for g in grams:
                    rest_terms[g] = rest_terms.get(g, 0) + 1
        words.append(w)
        cuts.append(sum(1 for a, _ in v["shots"] if 0 < a < win))
        fs = next((m["t_start"] for m in v["moments"]
                   if m["source"] == "speech" and m["timed"]), None)
        if fs is not None:
            first_speech.append(float(fs))
    return {
        "reels": answerable,
        "untimed": untimed,
        "opens_with": [{"source": s, "n": n,
                        "rate": round(n / answerable, 3) if answerable else None}
                       for s, n in sorted(opens.items(), key=lambda kv: -kv[1])],
        "leads_with": [{"source": s, "n": n,
                        "rate": round(n / answerable, 3) if answerable else None}
                       for s, n in sorted(leads.items(), key=lambda kv: -kv[1])],
        "silent_open": {"n": silent,
                        "rate": round(silent / answerable, 3) if answerable else None},
        "words": _stats([float(x) for x in words]),
        "cuts": _stats([float(x) for x in cuts]),
        "first_speech_at": _stats(first_speech),
        "phrases": _lift(hook_terms, rest_terms),
        "phrase_basis": {"hook_terms": sum(hook_terms.values()),
                         "rest_terms": sum(rest_terms.values())},
    }


def patterns(conn: sqlite3.Connection, goal: str = "", creator: str = "",
             category: str = "") -> dict:
    """What the reels in a scope have in common, as distributions.

    Medians and percentiles, never a single average, and every measure carries
    the `n` it was computed from — a p90 shot length over four reels is a fact
    about four reels and the screen has to be able to say so."""
    scope = _scope(conn, goal, creator, category)
    fp = _fingerprint(conn)
    ck = ("patterns", scope["mode"], goal, creator, category, fp)

    def build() -> dict:
        loaded = _load(conn, scope["keys"])
        rows = [_row_features(v) for v in loaded.values()]
        rows.sort(key=lambda r: -(r["moments"] or 0))
        notes: list[str] = []
        if not rows:
            notes.append("Nothing is indexed in this scope yet.")
        no_shots = sum(1 for r in rows if not r["shots"])
        if rows and no_shots:
            notes.append(f"{no_shots} of {_n(len(rows), 'reel')} "
                         f"{'has' if no_shots == 1 else 'have'} no detected shots, "
                         f"so {'it is' if no_shots == 1 else 'they are'} absent from "
                         f"every pacing number rather than counted as having "
                         f"none.")
        no_ms = sum(1 for r in rows if not r["moments"])
        if rows and no_ms:
            notes.append(f"{no_ms} of {_n(len(rows), 'reel')} "
                         f"{'carries' if no_ms == 1 else 'carry'} no evidence "
                         f"yet.")
        hook = _hook_agg(loaded)
        if hook["untimed"]:
            u = hook["untimed"]
            notes.append(f"{u} of {_n(len(rows), 'reel')} "
                         f"{'has' if u == 1 else 'have'} evidence that is not "
                         f"placed on a timeline, so {'it is' if u == 1 else 'they are'} "
                         f"absent from every opening number. The openings are not "
                         f"silent; they were never looked at.")

        # Channel presence: how often a channel appears at all, and how much of
        # the runtime it holds when it does. Both, because `style` is present in
        # nearly everything and occupies almost none of it.
        chans = []
        for c in list(CHANNELS) + sorted({s for r in rows for s in r["channels"]} - set(CHANNELS)):
            have = [r for r in rows if c in r["channels"]]
            if not have:
                continue
            sh = [r["shares"][c] for r in have if r["shares"].get(c) is not None]
            chans.append({"source": c, "n": len(have),
                          "rate": round(len(have) / len(rows), 3) if rows else None,
                          "share": _stats(sh)})

        cuts_x = [r["cuts_per_min"] for r in rows if r["cuts_per_min"] is not None]
        talk_x = [r["speech_share"] for r in rows if r["speech_share"] is not None]
        pace_band = _tertile_bands(cuts_x, ("slow", "steady", "fast"))
        talk_band = _tertile_bands(talk_x, ("quiet", "mixed", "talky"))

        grid: dict[str, dict] = {}
        for r in rows:
            p = _band_of(r["cuts_per_min"], pace_band)
            t = _band_of(r["speech_share"], talk_band)
            if not p or not t:
                continue
            cell = grid.setdefault(f"{p}/{t}", {"pace": p, "talk": t, "n": 0,
                                                "examples": []})
            cell["n"] += 1
            if len(cell["examples"]) < EXAMPLES:
                cell["examples"].append({"video_key": r["video_key"],
                                         "title": r["title"],
                                         "cuts_per_min": r["cuts_per_min"],
                                         "speech_share": r["speech_share"]})
        archetypes = sorted(grid.values(), key=lambda c: -c["n"])
        if not archetypes and rows:
            notes.append("The scope is too small or too uniform to split into "
                         "pace × talkativeness bands, so no archetypes are shown.")

        return {
            "ok": True,
            "scope": {k: scope[k] for k in ("mode", "goal", "creator",
                                            "category", "note", "archive")},
            "reels": len(rows),
            "measures": {
                "duration": _dist(rows, "duration"),
                "cuts_per_min": _dist(rows, "cuts_per_min"),
                "shot_len": _dist(rows, "shot_len"),
                "regularity": _dist(rows, "regularity"),
                "moments_per_min": _dist(rows, "moments_per_min"),
                "words_per_s": _dist(rows, "words_per_s"),
                "speech_share": _dist(rows, "speech_share"),
            },
            "channels": chans,
            "hook": hook,
            "bands": {"pace": pace_band, "talk": talk_band},
            "archetypes": archetypes,
            "reel_rows": rows[:60],
            "method": {
                "phrases": "log-odds ratio with an informative Dirichlet prior "
                           "(Monroe, Colaresi & Quinn 2008), ranked by z",
                "bands": "tertiles of this scope, not fixed thresholds",
                "hook_window": HOOK_S,
                "compared": "the first %.0fs of each reel against the rest of "
                            "the same reels" % HOOK_S,
            },
            "notes": notes,
        }

    return _cached(ck, build)


# ══════════════════════════════════════════════════════════════════════════
# A BEAT SHEET
# ══════════════════════════════════════════════════════════════════════════

def _slot_of(t_mid: float, duration: float) -> str | None:
    """Which named slot a moment's midpoint falls in, by proportion of runtime.

    Proportion, not absolute seconds, so a 12-second reel and a 90-second one
    contribute to the same beat — which is the whole point of normalising, and
    also the reason the slot names are a convention rather than a finding."""
    if duration <= 0:
        return None
    p = min(0.999999, max(0.0, t_mid / duration))
    for name, a, b in SLOTS:
        if a <= p < b:
            return name
    return SLOTS[-1][0]


def _fmt_s(x: float) -> str:
    return f"{x:.1f}s" if x < 10 else f"{x:.0f}s"


def _outline(beats: list[dict], target: float, reels: int) -> dict:
    """The beat sheet as text, for reading and for copying out.

    Structured `beats` is what the screen renders; this is the same thing in the
    form a person pastes into their notes. Nothing is here that is not in
    `beats` — it is a rendering, not a second calculation, so the two can never
    disagree."""
    lines: list[dict] = []
    for b in beats:
        bits = [f"{_fmt_s(b['t0'])}–{_fmt_s(b['t1'])}"]
        if b["lead"]:
            rate = f" in {round(100 * b['lead_rate'])}% of {b['voters']}" \
                   if b["lead_rate"] is not None else ""
            bits.append(f"{b['lead']}-led{rate}")
        if b["cuts"]["median"] is not None:
            n = b["cuts"]["median"]
            bits.append(f"{n:.0f} cut" + ("" if abs(n - 1.0) < 0.5 else "s"))
        if b["words"]["median"] is not None:
            bits.append(f"~{b['words']['median']:.0f} words")
        points: list[str] = []
        if b["phrases"]:
            points.append("phrases that belong to this beat: "
                          + ", ".join(f"“{p['term']}”" for p in b["phrases"][:5]))
        for e in b["examples"]:
            points.append(f"{e['title']} @ {_fmt_s(e['t'])} ({e['source']}) — "
                          f"{_norm(e['text'])[:120]}")
        if not points:
            points.append("no text evidence lands in this beat across the scope")
        lines.append({"name": b["name"], "headline": " · ".join(bits),
                      "points": points})
    head = (f"Measured from {_n(reels, 'reel')}; target runtime {_fmt_s(target)}."
            if reels else "Nothing measurable in this scope.")
    text = head + "\n\n" + "\n".join(
        f"{ln['name']} — {ln['headline']}\n" + "\n".join(f"  · {p}" for p in ln["points"])
        for ln in lines)
    return {"head": head, "lines": lines, "text": text}


def script_draft(conn: sqlite3.Connection, goal: str = "", creator: str = "",
                 category: str = "") -> dict:
    """A beat sheet measured from the scope, with citations and no prose.

    Every number is a median of real reels; every phrase is ranked by the same
    log-odds test `patterns` uses, here comparing one slot against the other
    four; every example is a real moment with its reel key and timecode, so the
    reader can open it and check. What this deliberately does **not** produce is
    written lines — the archive can say that 71% of these reels open on a
    caption of about nine words, and it cannot say what yours should be."""
    scope = _scope(conn, goal, creator, category)
    fp = _fingerprint(conn)
    ck = ("script", scope["mode"], goal, creator, category, fp)

    def build() -> dict:
        loaded = _load(conn, scope["keys"])
        usable = [v for v in loaded.values() if v["duration"] > 0 and v["moments"]]
        durs = [v["duration"] for v in usable]
        target = _pct(durs, 0.5) if durs else 0.0

        names = [s[0] for s in SLOTS]
        cov: dict[str, dict[str, float]] = {n: {} for n in names}      # slot → channel → seconds
        lead_votes: dict[str, dict[str, int]] = {n: {} for n in names}
        words: dict[str, list[float]] = {n: [] for n in names}
        cuts: dict[str, list[float]] = {n: [] for n in names}
        terms: dict[str, dict[str, int]] = {n: {} for n in names}
        cites: dict[str, list[dict]] = {n: [] for n in names}

        for v in usable:
            dur = v["duration"]
            per_slot_cov: dict[str, dict[str, float]] = {n: {} for n in names}
            w: dict[str, int] = {n: 0 for n in names}
            for m in v["moments"]:
                slot = _slot_of((m["t_start"] + m["t_end"]) / 2.0, dur)
                if slot is None:
                    continue
                span = max(0.0, m["t_end"] - m["t_start"])
                per_slot_cov[slot][m["source"]] = \
                    per_slot_cov[slot].get(m["source"], 0.0) + span
                if m["text"].strip():
                    w[slot] += _wordcount(m["text"])
                    for g in _grams(m["text"]):
                        terms[slot][g] = terms[slot].get(g, 0) + 1
                    cites[slot].append({"video_key": v["video_key"],
                                        "title": v["title"],
                                        "source": m["source"],
                                        "t": round(m["t_start"], 2),
                                        "weight": m["weight"],
                                        "text": m["text"][:200]})
            for name, a, b in SLOTS:
                lo, hi = a * dur, b * dur
                cuts[name].append(float(sum(1 for s0, _ in v["shots"] if lo < s0 < hi)))
                words[name].append(float(w[name]))
                for src, sec in per_slot_cov[name].items():
                    cov[name][src] = cov[name].get(src, 0.0) + sec
                if per_slot_cov[name]:
                    top = max(per_slot_cov[name], key=per_slot_cov[name].get)
                    lead_votes[name][top] = lead_votes[name].get(top, 0) + 1

        beats = []
        for name, a, b in SLOTS:
            votes = lead_votes[name]
            voters = sum(votes.values())
            ranked = sorted(votes.items(), key=lambda kv: -kv[1])
            others: dict[str, int] = {}
            for o in names:
                if o == name:
                    continue
                for g, n in terms[o].items():
                    others[g] = others.get(g, 0) + n
            ex = sorted(cites[name], key=lambda c: -c["weight"])
            seen, picked = set(), []
            for c in ex:                       # distinct reels, so three examples
                if c["video_key"] in seen:     # are three reels and not one reel
                    continue                   # quoted three times
                seen.add(c["video_key"])
                picked.append(c)
                if len(picked) >= EXAMPLES:
                    break
            beats.append({
                "name": name,
                "p0": a, "p1": b,
                "t0": round(a * target, 2), "t1": round(b * target, 2),
                "len": round((b - a) * target, 2),
                "lead": ranked[0][0] if ranked else None,
                "lead_rate": round(ranked[0][1] / voters, 3) if voters else None,
                "leads": [{"source": s, "n": n,
                           "rate": round(n / voters, 3) if voters else None}
                          for s, n in ranked[:4]],
                "voters": voters,
                "cuts": _stats(cuts[name]),
                "words": _stats(words[name]),
                "phrases": _lift(terms[name], others, top=8),
                "examples": picked,
            })

        outline = _outline(beats, target, len(usable))
        notes: list[str] = []
        if not usable:
            notes.append("No reel in this scope has both a runtime and any "
                         "evidence, so there is nothing to draft from.")
        elif len(usable) < 8:
            notes.append(f"Only {_n(len(usable), 'reel')} "
                         f"{'backs' if len(usable) == 1 else 'back'} this draft. "
                         f"The medians are real but they are medians of "
                         f"{len(usable)} — read them as a sketch.")
        skipped = len(loaded) - len(usable)
        if skipped > 0:
            notes.append(f"{_n(skipped, 'reel')} in the scope "
                         f"{'was' if skipped == 1 else 'were'} skipped for having "
                         f"no runtime or no evidence.")

        return {
            "ok": True,
            "scope": {k: scope[k] for k in ("mode", "goal", "creator",
                                            "category", "note", "archive")},
            "reels": len(usable),
            "target_s": round(target, 2),
            "duration": _stats(durs),
            "beats": beats,
            "outline": outline,
            "method": {
                "slots": "positional convention — fixed proportions of runtime, "
                         "named in the editorial vocabulary; what is measured is "
                         "what occupies them",
                "numbers": "medians across the scope, not averages",
                "phrases": "log-odds with an informative Dirichlet prior, this "
                           "slot against the other four",
                "prose": "none is generated — every line cites a real reel",
            },
            "notes": notes,
        }

    return _cached(ck, build)
