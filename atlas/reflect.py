"""
Schema reflection.

The requirement this file exists for: *"even if I change things, add or subtract
things from the database, it should still work, or require very very minimum
changes in code."*

The usual way to read a database is to write its column names into the program.
That works exactly until the schema moves. Add `frame_notes.pose_summary` and
the search index silently ignores it. Rename `chunks.description` and search
returns nothing, with no error anywhere. Drop a table and the UI throws.

So Atlas never names a column outside this module. It asks three questions at
runtime and derives everything else:

  1. What tables are here?              → `tables()`
  2. What does each column mean?        → role inference below
  3. Has any of that changed?           → `fingerprint()`

Role inference is the interesting part. Rather than matching a fixed list, each
column is scored against a small vocabulary of *roles* — the key that identifies
a video, the timestamps that place a row on the timeline, and the free text a
human would search. A new TEXT column is picked up as searchable content
automatically, because the rule is "text that is not an id, a path, or an
enum", not "one of these seven names". A renamed column is found as long as the
new name still reads like what it is.

Two rules here are stricter than they first look, and both are stricter on
purpose:

  A key must be *named* like a video key. The tempting fallback — "any single
  numeric primary key is the key" — attaches `categories(id=3, name='fitness')`
  to video 3. That is not a crash, it is worse: search quietly returns the wrong
  video. When the identity of a row is unknown, the row is not indexed.

  A blocked column suffix must be a whole token. Matching raw string suffixes
  drops `objects` for ending in "ts" and `format` for ending in "at". Names are
  split on separators and case changes, and only the final token is judged.

Nothing here raises on an unfamiliar schema. An unrecognised table becomes a
browsable table with no moments in it, which is the correct degradation: you can
still see the data, you just cannot search text Atlas could not identify as text.
"""

import hashlib
import re
import sqlite3

# ── Role vocabularies ─────────────────────────────────────────────────────
# Ordered by confidence: when a table offers several candidates, the earlier
# name wins. Matched against the *normalised* column name (lowercase,
# non-alphanumerics stripped), so `video_uuid`, `videoUUID` and `video uuid`
# are the same string by the time they get here.
_KEY_NAMES = ("videouuid", "videokey", "mediakey", "msgid", "videoid",
              "messageid", "postid", "reelid", "clipid", "itemid",
              "video", "uuid", "mediaid")
_START_NAMES = ("startt", "startsec", "starttime", "tstart", "start",
                "tssec", "timestamp", "ts", "time", "sec", "seconds",
                "offset", "position", "t0", "framet")
_END_NAMES = ("endt", "endsec", "endtime", "tend", "end", "stop", "until",
              "t1", "framet1")
# `frame_t` / `frame_t1` are here for the shape that carries a frame claim's own
# stamp under its own name. A pass stamps a frame observation with the frame it
# starts on *and* the presentation timestamp that frame was decoded at — see
# `sizing/base.py:frame_claim` — and both writers currently rename those to
# `t0`/`t1` on the way to storage (`runners/__init__.py:780`, and upstream
# `vios/process/store.py:832`), so no table on either side declares a `frame_t`
# column today. Measured, not assumed: the four live `claim` shapes resolve to
# `('','')`, `('t0','t1')`, `('t0','t1')` and — for a table built straight from
# `frame_claim` payloads — `('frame_t','frame_t1')`. These two names are what
# make that fourth case placeable instead of untimed, and the renaming is one
# line in one writer, so recognising the raw name costs nothing and removes a
# whole class of silent regression.
#
# The shape that actually had no time is the first one: a `claim` table built by
# shard replay with no `t0`/`t1` columns at all. No name can fix that, and
# `time_link` below is what does — it borrows the span from the shot the row
# points at.
#
# Last rather than first so a table carrying both a plain span and a frame stamp
# keeps the span: `t0` is what the row is about, `frame_t` is where it was
# sampled. `frame_idx` and `frame_hi` are deliberately not here — they are frame
# numbers, and a moment at t=142s because it was frame 142 is worse than one
# with no time at all.

# Wall-clock columns. A row's insert time is not a position in a video, and
# treating it as one puts every moment at t=1.75 billion seconds.
_NOT_TIMELINE = {"createdat", "updatedat", "insertedat", "modifiedat",
                 "date", "datetime", "epoch", "fetchedat", "importedat"}

# Final-token blocklist for content columns: identifiers, filesystem paths,
# enums and formatted duplicates of a numeric column. Indexing these makes
# search worse — a query for "kitchen" should not match because a file happens
# to live in /kaggle/temp/kitchen/.
_NOT_CONTENT_TOKEN = {
    "id", "ids", "path", "paths", "uuid", "url", "uri", "link", "href",
    "at", "on", "ts", "time", "date", "status", "state", "mode", "kind",
    "type", "hash", "sha256", "md5", "checksum", "sig", "signature",
    "ext", "mime", "mimetype", "filename", "dir", "folder", "thumb",
    "flag", "version", "rev", "idx", "index", "seq", "num", "count",
}
# Whole names that are not content regardless of how they tokenise.
_NOT_CONTENT_EXACT = {"durationstr", "firstframe", "folderid", "thumb",
                      "localvideopath", "abspath", "videopath", "filepath",
                      # The evidence schema. `uid` is a content hash, `channel`
                      # and `space` are enums, `detector` and `observerid` name
                      # the model that spoke — all of them identifiers rather
                      # than things a person searches for. Left in, a query for
                      # "speech" matches every transcript claim ever written.
                      "uid", "channel", "space", "detector", "observerid",
                      "videokey", "component"}

# What kind of evidence a column carries. Keyed by (table, column) with both
# sides normalised, because the same column name means different things in
# different tables: `chunks.description` is a vision model narrating a
# five-second window; `frame_notes.description` describes one still frame.
_SOURCE_MAP = {
    ("chunks", "description"):      "narrative",
    ("transcripts", "text"):        "speech",
    ("framenotes", "description"):  "visual",
    ("framenotes", "objects"):      "visual",
    ("framenotes", "ocrtext"):      "ocr",
    ("posts", "caption"):           "caption",
    ("videos", "title"):            "meta",
    ("creators", "username"):       "meta",
    ("categories", "name"):         "meta",
}

# Fallback for a column nobody has met before. Checked in order, first hit wins.
_SOURCE_HINTS = (
    ("ocr", "ocr"), ("subtitle", "speech"), ("transcript", "speech"),
    ("speech", "speech"), ("audio", "speech"), ("dialog", "speech"),
    ("caption", "caption"), ("narrat", "narrative"), ("summar", "narrative"),
    ("descri", "narrative"), ("story", "narrative"),
    ("object", "visual"), ("label", "visual"), ("tag", "visual"),
    ("scene", "visual"), ("pose", "visual"), ("action", "visual"),
    ("title", "meta"), ("name", "meta"), ("author", "meta"),
)

# Atlas's own tables. They are derived from the others, so indexing them would
# feed search its own output back to it. `parts` belongs here for the same reason
# `video_index` does — it is written by `index.ensure_schema`, and its `name`
# column is a clip filename, timestamped and keyed by video, so left in it puts
# one `1234-c0007.mp4` passage into search per clip of every video.
#
# `vec_payload` is here for a different reason: it holds raw float buffers as
# BLOBs, which no reflection can describe and no reader would want to browse. It
# is written by `ingest`'s payload lane and read only by the image search that
# builds its index from it. `coverage` is here because it is the processing
# plane's work table — it says what ran, which means nothing to a search index.
#
# `scan_seen` is the incremental scan's memory: one row per Telegram message id
# with the verdict that settled it. Its `verdict` column is text, and reflection
# duly volunteered it as a search source on the first run of the new repo —
# `indexing 2 text source(s): map_point.source, scan_seen.verdict` — which would
# have put one "absent" or "no-document" passage into search per message in the
# channel. Thousands of them, all meaningless. Caught by reading the boot log,
# which is the argument for having one.
#
# `map_point` came out of that same log line, and it is an older bug: it is the
# UMAP projection `maps.py` writes, and its `source` column is a channel label
# ("speech", "ocr") rather than prose. Left in, it contributes one one-word
# passage per projected point — up to 180k of them, per `maps.py:114` — every one
# a duplicate of a label search already has as a facet. It was invisible because
# it only appears once a map has been built, and nothing read the log that said so.
_ATLAS_OWN = {"moments", "moments_fts", "bundles", "atlas_meta", "ingest_log",
              "video_index", "graph_nodes", "graph_edges", "parts",
              "vec_payload", "coverage", "scan_seen", "map_point"}

# Every reason above is a reason not to *index* a table. None of them is a reason
# not to let a person read it, and for two of these it is the opposite: `moments`
# is the evidence itself and `video_index` is one row per reel, which makes them
# the two tables someone opens the raw browser to see. So `_ATLAS_OWN` gates
# `tables()` (what search reads) and this gates `browsable()` (what a person can
# open), and the only name in both is the one the paragraph above rules out on its
# own terms — `vec_payload` is float buffers in a BLOB, unreadable as text.
_BROWSE_HIDE = {"vec_payload"}

_FTS_SHADOW = re.compile(r"_(data|idx|content|docsize|config)$")
_TOKEN_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _tokens(name: str) -> list:
    """Split a column name into words, on separators and on camelCase."""
    return [t.lower() for t in _TOKEN_SPLIT.split(name or "") if t]


# ══════════════════════════════════════════════════════════════════════════
# RAW INTROSPECTION
# ══════════════════════════════════════════════════════════════════════════
def _real_tables(conn: sqlite3.Connection) -> list:
    """Every table and view that physically holds rows, in a stable order.

    Virtual tables are excluded by reading `sql`, not by name: sqlite_master
    reports an fts5 index with type='table', so `posts_search` looks exactly
    like a real table until you notice its DDL says CREATE VIRTUAL TABLE. The
    four shadow tables fts5 keeps per index are excluded by suffix, which is the
    only thing that marks them.
    """
    try:
        rows = conn.execute(
            "SELECT name, type, COALESCE(sql,'') FROM sqlite_master "
            "WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name").fetchall()
    except sqlite3.Error:
        return []
    out = []
    for name, _kind, sql in rows:
        if "CREATE VIRTUAL TABLE" in (sql or "").upper():
            continue
        if _FTS_SHADOW.search(name.lower()):
            continue
        out.append(name)
    return out


def tables(conn: sqlite3.Connection) -> list:
    """Every table search should read, in a stable order.

    This is the indexer's, the graph's and `text_sources`' view of the file:
    Atlas's own output is filtered out, because feeding search its own moments
    back to it is how one transcript line becomes two hits.
    """
    return [t for t in _real_tables(conn) if t.lower() not in _ATLAS_OWN]


def browsable(conn: sqlite3.Connection) -> list:
    """Every table a person can open in the raw browser, in a stable order.

    `tables()` and this differ by `_ATLAS_OWN`, and the difference is the whole
    point: one question is "what should search read", the other is "what can be
    read". Answering both from one list is why the Data tab shipped listing four
    of this file's nineteen tables — `claim`, `shot`, and two empty map tables —
    with `moments` and `video_index` missing, under a heading that promises every
    table Atlas writes.
    """
    return [t for t in _real_tables(conn) if t.lower() not in _BROWSE_HIDE]


def columns(conn: sqlite3.Connection, table: str) -> list:
    """[{name, type, notnull, pk}] for one table. Empty list if it is gone."""
    try:
        rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return []
    return [{"name": r[1], "type": (r[2] or "").upper(),
             "notnull": bool(r[3]), "pk": bool(r[5])} for r in rows]


def row_count(conn: sqlite3.Connection, table: str) -> int:
    try:
        return conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    except sqlite3.Error:
        return 0


def searched(table: str) -> bool:
    """Does search read this table's text, or is it search's own output?

    A predicate rather than an exported set, because every caller wants the
    question and not the list, and one of them is a UI that would otherwise
    print "searchable" over `moments.text`.
    """
    return (table or "").lower() not in _ATLAS_OWN


def cell_value(value):
    """One stored value, rendered so it can survive being JSON.

    SQLite will hand back `bytes` for any BLOB, and FastAPI's encoder turns
    `bytes` into a string by decoding it as UTF-8 — which raises on a float
    buffer or a thumbnail and answers the request with a 500. A browser that
    promises to open a database it has never seen cannot fall over on a column
    type, so a blob renders as its size. `vec_payload` is excluded from browsing
    outright; this is for the blob nobody predicted, in an imported shard.
    """
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"‹{len(bytes(value)):,} bytes›"
    return value


# Bumped when anything that turns this schema into moments starts doing it
# differently — the name lists and `time_link` below, and `index.build_passages`,
# which decides what a passage is and which of two identical texts survives.
#
# The fingerprint used to hash only the schema, which is correct as long as a
# fixed set of rules is being applied to it — a new column changes the hash and
# the index picks the column up. It is wrong the moment the rules themselves
# move: adding `frame_t` to `_START_NAMES` and teaching `time_link` to borrow a
# shot's span means the *same* tables now yield times they did not yield before,
# and an install whose `moments` table was already built would keep every
# `t_start` NULL for good. Nothing would ever ask again. So the version of the
# rules is part of what the index was built from, and it belongs in the hash.
#
# 1 → the original schema-only hash.
# 2 → `frame_t`/`frame_t1` recognised as times; `shot_idx` borrows `shot.t0/t1`.
# 3 → a placed passage now outranks an identical unplaced one, instead of losing
#     the `INSERT OR IGNORE` race to it (`index.build_passages`).
_RULES_VERSION = 3


def fingerprint(conn: sqlite3.Connection) -> str:
    """A hash of the schema's shape and the rules read against it.

    The indexer stores this alongside the moment table. When it differs, the
    schema moved and the index is rebuilt — which is how a new column becomes
    searchable without anyone editing code or pressing anything. `_RULES_VERSION`
    is folded in so the reverse also holds: when the extraction rules change, one
    rebuild happens on next boot without anyone knowing they needed to ask.
    """
    parts = [f"rules={_RULES_VERSION}"]
    for t in tables(conn):
        cols = ",".join(f"{c['name']}:{c['type']}" for c in columns(conn, t))
        parts.append(f"{t}({cols})")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


# ══════════════════════════════════════════════════════════════════════════
# ROLE INFERENCE
# ══════════════════════════════════════════════════════════════════════════
def _is_numeric(col: dict) -> bool:
    t = col["type"]
    return any(k in t for k in ("INT", "REAL", "FLOA", "DOUB", "NUM", "DEC"))


def _is_texty(col: dict) -> bool:
    # A column with no declared type is TEXT in practice — pgdump only emits
    # BLOB for bytea, and an untyped SQLite column accepts anything.
    t = col["type"]
    return (not t) or ("CHAR" in t) or ("TEXT" in t) or ("CLOB" in t)


def key_column(cols: list) -> str:
    """The column that identifies which video a row belongs to, or "".

    Preference order matters more than it looks: `chunks` has both `video_uuid`
    and `chunk_id`, and picking `chunk_id` would give every row its own video.

    There is deliberately no "any numeric primary key" fallback — see the module
    docstring. A table whose key cannot be named is left unindexed rather than
    joined to whatever video happens to share its row id.
    """
    by_norm = {_norm(c["name"]): c["name"] for c in cols}
    for want in _KEY_NAMES:
        if want in by_norm:
            return by_norm[want]
    return ""


def time_columns(cols: list) -> tuple:
    """(start, end) column names, either of which may be "".

    A row with a start and no end is a point on the timeline, not a bug: a
    frame note happens at an instant. The moment builder gives those a small
    window rather than dropping them.
    """
    numeric = {_norm(c["name"]): c["name"] for c in cols
               if _is_numeric(c) or not c["type"]}
    start = end = ""
    for want in _START_NAMES:
        if want in numeric and want not in _NOT_TIMELINE:
            start = numeric[want]
            break
    for want in _END_NAMES:
        if want in numeric and want not in _NOT_TIMELINE:
            end = numeric[want]
            break
    if _norm(start) in _NOT_TIMELINE:
        start = ""
    if _norm(end) in _NOT_TIMELINE:
        end = ""
    return start, end


def content_columns(cols: list) -> list:
    """Text columns a person would actually want to search.

    This is the rule that makes new columns work for free: anything text-typed
    that is not an identifier, a path, a timestamp or a short enum is content.
    """
    out = []
    for c in cols:
        if not _is_texty(c) or c["pk"]:
            continue
        n = _norm(c["name"])
        if n in _NOT_CONTENT_EXACT:
            continue
        toks = _tokens(c["name"])
        if not toks:
            continue
        # Judge the final token only. `ocr_text` ends in "text" (content);
        # `local_video_path` ends in "path" (not). A single-token name is
        # judged whole, so `status` is out and `objects` is in.
        if toks[-1] in _NOT_CONTENT_TOKEN:
            continue
        out.append(c["name"])
    return out


def source_label(table: str, column: str) -> str:
    """Which kind of evidence a column carries, for weighting and for colour."""
    t = _norm(table)
    if t.startswith("omni"):
        t = t[4:]
    key = (t, _norm(column))
    if key in _SOURCE_MAP:
        return _SOURCE_MAP[key]
    n = _norm(column)
    for needle, label in _SOURCE_HINTS:
        if needle in n:
            return label
    return "meta"


# Columns whose value names the kind of evidence in that row.
_ROW_SOURCE_COLUMNS = ("channel", "source")
# Only these values are trusted to partition a table. An arbitrary enum would
# split the index into labels nothing knows how to weight or colour, so a
# column has to speak the vocabulary the rest of Atlas already uses.
_KNOWN_SOURCES = frozenset({"narrative", "speech", "visual", "ocr", "caption",
                            "meta", "audio", "concept", "style"})


def _row_source_labels(conn, table: str, cols: list):
    """(column, [values]) when a table labels its own rows, else None.

    Read from the data rather than declared per table, so an evidence store
    that adds a channel next month partitions on it without a code change.
    A column qualifies only if every value it holds is a source Atlas knows —
    one unrecognised value and the whole table falls back to a single spec,
    because a half-labelled index is harder to reason about than an unlabelled
    one.
    """
    by_norm = {_norm(c["name"]): c["name"] for c in cols}
    for want in _ROW_SOURCE_COLUMNS:
        name = by_norm.get(want)
        if not name:
            continue
        try:
            vals = [r[0] for r in conn.execute(
                f"SELECT DISTINCT {_q(name)} FROM {_q(table)} "
                f"WHERE {_q(name)} IS NOT NULL LIMIT 40")]
        except sqlite3.Error:
            continue
        vals = [str(v) for v in vals if str(v).strip()]
        if vals and all(v in _KNOWN_SOURCES for v in vals):
            return name, sorted(set(vals))
    return None


# ══════════════════════════════════════════════════════════════════════════
# DIMENSION JOINS
# ══════════════════════════════════════════════════════════════════════════
def _plural_forms(stem: str) -> tuple:
    forms = [stem, stem + "s", stem + "es"]
    if stem.endswith("y"):
        forms.append(stem[:-1] + "ies")
    return tuple(forms)


def dimension_links(conn: sqlite3.Connection, table: str, cols: list) -> list:
    """Lookup tables this table points at, found by naming convention.

    `posts.creator_id` → `creators.id`, `posts.category_id` → `categories.id`.
    Without this, a creator's name lives in a table whose own key is a category
    id, so it can never be indexed safely on its own — and searching for a
    creator by name would silently return nothing.

    The convention is the only signal available: pg_dump's plain format carries
    foreign keys as ALTER TABLE statements that this pipeline skips, and
    lake.db never declared them.
    """
    present = {_norm(t): t for t in tables(conn)}
    links = []
    for c in cols:
        toks = _tokens(c["name"])
        if len(toks) < 2 or toks[-1] != "id":
            continue
        stem = "".join(toks[:-1])
        for form in _plural_forms(stem):
            target = present.get(form)
            if not target or target == table:
                continue
            tcols = columns(conn, target)
            tnames = {_norm(x["name"]): x["name"] for x in tcols}
            if "id" not in tnames:
                continue
            texts = content_columns(tcols)
            if not texts:
                continue
            links.append({"table": target, "local": c["name"],
                          "remote": tnames["id"], "texts": texts})
            break
    return links


# A row that says which shot it belongs to, in a table that keeps no clock of
# its own. Named exactly rather than by convention: `shot_idx` is a position in
# a list, so a generic "any *_idx points at a table" rule would happily join
# `ordinal` or `frame_idx` to something and put moments at the wrong second.
_SHOT_LINK = "shotidx"


def time_link(conn: sqlite3.Connection, table: str, cols: list,
              key: str, start: str = "", end: str = "") -> dict:
    """Times a table can borrow from the shot each row points at, or {}.

    A `claim` table built by shard replay has no `t0`/`t1` columns at all: the
    claim is placed by `shot_idx` and the seconds live on `shot`. Without this
    the indexer reads every per-shot observation as untimed — on a database built
    entirely by shard replay, which is the only thing a new machine has, that
    was 87 of 87 moments with a NULL `t_start` and a Studio timeline with
    nothing on it. `studio._claims` does this same join to keep an entity
    clickable; doing it here is what makes a *search hit* seekable.

    When the table has a start of its own, the shot is a fallback rather than a
    replacement — `COALESCE(t.t0, s.t0)` — because a column existing is not a row
    having a value in it. The local writer fills `t0`/`t1` only for a frame claim
    and leaves them NULL for a per-shot or whole-reel one, and since the shard
    header declares the columns either way, the same table holds both. The end is
    taken from the shot *only* when the start was, so a point claim with no end of
    its own is not stretched across the whole shot it happens to fall in.

    Returns `{"join", "start", "end", "via", "via_end"}` — the first three SQL
    fragments against alias `t`, the last two the plain-language origin the
    Admin tab prints in place of a column name — or `{}` when there is nothing
    to borrow.
    """
    # No early return on `start and end`. A column existing is not the same as a
    # row having a value in it, and conflating the two is the whole family of bug
    # this function was written to end: the shard header now declares `t0`/`t1`
    # on every `claim` table it creates, including the tables whose every row
    # leaves them NULL, so a guard reading "the row already knows its own span"
    # would look at the schema, believe it, and skip the borrow for exactly the
    # rows that need it. `COALESCE` is what decides, per row, and it decides
    # correctly whether the column is full, empty or half of each.
    by_norm = {_norm(c["name"]): c["name"] for c in cols}
    local = by_norm.get(_SHOT_LINK)
    if not local or not key:
        return {}
    shot = {_norm(t): t for t in tables(conn)}.get("shot")
    if not shot or shot == table:
        return {}
    scols = columns(conn, shot)
    snames = {_norm(c["name"]): c["name"] for c in scols}
    s_key = key_column(scols)
    if not all(n in snames for n in ("idx", "t0", "t1")) or not s_key:
        return {}
    return {
        "join": (f' LEFT JOIN {_q(shot)} s ON s.{_q(s_key)} = t.{_q(key)}'
                 f' AND s.{_q(snames["idx"])} = t.{_q(local)}'),
        "start": (f'COALESCE(t.{_q(start)}, s.{_q(snames["t0"])})' if start
                  else f's.{_q(snames["t0"])}'),
        "end": (f'CASE WHEN t.{_q(start)} IS NULL THEN s.{_q(snames["t1"])}'
                f' ELSE {("t." + _q(end)) if end else "NULL"} END' if start
                else f's.{_q(snames["t1"])}'),
        "via": f'{shot}.{snames["t0"]} via {local}',
        "via_end": f'{shot}.{snames["t1"]} via {local}',
    }


# ══════════════════════════════════════════════════════════════════════════
# THE CATALOG
# ══════════════════════════════════════════════════════════════════════════
def _q(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


# A run of letters long enough to be a word rather than an exponent, a unit or
# an axis label. Three is the shortest that excludes "e+05", "id" and "0.5".
_LEXICAL = re.compile(r"[A-Za-z]{3,}")


def prose_columns(conn: sqlite3.Connection, table: str, cols: list) -> list:
    """`content_columns` narrowed to the ones whose *data* reads like language.

    A name is a good guide and not proof. `frame_metric.values_`,
    `frame_metric.frames` and `frame_vector.frames` are TEXT columns holding
    JSON arrays of floats: every name test calls them content, and no human
    query can ever match one. Left in, they cost encoder time on every build and
    dilute ranking with passages that are numbers.

    So the values decide. A column stays if a sample of them contains actual
    words. Deliberately generous — a quarter of the sample is enough, a column
    with nothing in it is kept (there is nothing to judge, and it contributes no
    passages either way), and a short enum like `"float32"` reads as lexical. It
    drops number dumps, not anything a person wrote.

    Additive: `content_columns` and its other callers are untouched, and the
    rule is about the data, so no table is named here either.
    """
    keep = []
    for name in content_columns(cols):
        try:
            sample = [r[0] for r in conn.execute(
                f"SELECT t.{_q(name)} FROM {_q(table)} t "
                f"WHERE t.{_q(name)} IS NOT NULL "
                f"AND TRIM(t.{_q(name)}) <> '' LIMIT 24")]
        except sqlite3.Error:
            keep.append(name)           # unreadable — judge it by its name
            continue
        vals = [str(v) for v in sample]
        if not vals:
            keep.append(name)
            continue
        lexical = sum(1 for v in vals if _LEXICAL.search(v))
        need = 1 if len(vals) <= 4 else max(2, int(0.25 * len(vals)))
        if lexical >= need:
            keep.append(name)
    return keep


def text_sources(conn: sqlite3.Connection) -> list:
    """Every (table, key, time, text) the indexer should read, as ready SQL.

    The indexer holds no table names at all — it walks this list and runs the
    `sql` on each spec, which always yields exactly four columns:
    (key, start, end, text). Adding a table to the bundle adds rows to search;
    dropping one removes them; neither is a code change.
    """
    specs = []
    for table in tables(conn):
        cols = columns(conn, table)
        if not cols:
            continue
        key = key_column(cols)
        if not key:
            continue                       # nothing to attach a moment to
        start, end = time_columns(cols)
        s_expr = f"t.{_q(start)}" if start else "NULL"
        e_expr = f"t.{_q(end)}" if end else "NULL"
        # A table with no clock of its own can still be placed if its rows name
        # the shot they belong to. Resolved before the text loop so every spec
        # this table produces — one per evidence channel — carries the join.
        borrow = time_link(conn, table, cols, key, start, end)
        join = borrow.get("join", "")
        # `start`/`end` are reported to the Admin tab as where a moment's time
        # came from, and a borrowed time came from somewhere the row cannot name.
        # Left as the bare column name when the row has one, so the common case
        # still reads as a column; described when it does not, because "untimed"
        # would be a false answer about a source that is now placed on the clock.
        s_name, e_name = start, end
        if borrow:
            s_expr, e_expr = borrow["start"], borrow["end"]
            s_name = f"{start} → {borrow['via']}" if start else borrow["via"]
            e_name = f"{end} → {borrow['via_end']}" if end else borrow["via_end"]

        for text_col in prose_columns(conn, table, cols):
            base = (f"SELECT t.{_q(key)}, {s_expr}, {e_expr}, "
                    f"t.{_q(text_col)} FROM {_q(table)} t{join} "
                    f"WHERE t.{_q(text_col)} IS NOT NULL "
                    f"AND TRIM(t.{_q(text_col)}) <> ''")

            # A table that names its own evidence kind per row gets one spec
            # per kind. The evidence store keeps every transcript, OCR hit and
            # caption in a single `claim` table separated by `channel`; one
            # spec for the whole table would label all of them alike, and a
            # transcript would then be weighted like a filename.
            labels = _row_source_labels(conn, table, cols)
            if labels:
                col, values = labels
                for val in values:
                    specs.append({
                        "table": table, "key": key, "start": s_name, "end": e_name,
                        "text": text_col, "source": val, "via": None,
                        "sql": base + f" AND t.{_q(col)} = '{val}'",
                    })
                continue

            specs.append({
                "table": table, "key": key, "start": s_name, "end": e_name,
                "text": text_col, "source": source_label(table, text_col),
                "via": None,
                "sql": base,
            })

        # Dimension text, pulled onto the parent's key so a creator's name is
        # searchable against the videos they made rather than against nothing.
        for link in dimension_links(conn, table, cols):
            # The linked table's columns get the same data test as the parent's:
            # a lookup table can hold a number dump too, and `dimension_links`
            # is shared with the graph, which judges by name on purpose.
            prose = set(prose_columns(conn, link["table"],
                                      columns(conn, link["table"])))
            for text_col in link["texts"]:
                if text_col not in prose:
                    continue
                specs.append({
                    "table": link["table"], "key": key, "start": "", "end": "",
                    "text": text_col,
                    "source": source_label(link["table"], text_col),
                    "via": f'{table}.{link["local"]}',
                    "sql": (f"SELECT t.{_q(key)}, NULL, NULL, d.{_q(text_col)} "
                            f"FROM {_q(table)} t "
                            f"JOIN {_q(link['table'])} d "
                            f"  ON t.{_q(link['local'])} = d.{_q(link['remote'])} "
                            f"WHERE d.{_q(text_col)} IS NOT NULL "
                            f"AND TRIM(d.{_q(text_col)}) <> ''"),
                })
    return specs


def describe(conn: sqlite3.Connection, samples: int = 0) -> dict:
    """Everything the Data tab needs to render a database it has never seen.

    Roles are returned alongside the raw columns so the UI can mark which column
    is the key and which carry searchable text — the same inference search uses,
    shown to the person looking at it.

    It walks `browsable()`, not `tables()`: this is the reader's view of the
    file, and the reader wants `moments` most of all. `indexed` still answers
    search's question, so a table Atlas wrote reads as "not searchable" even
    though its text columns would qualify — `moments.text` is not indexed, it
    *is* the index.
    """
    out = {"fingerprint": fingerprint(conn), "tables": []}
    for table in browsable(conn):
        cols = columns(conn, table)
        key = key_column(cols)
        start, end = time_columns(cols)
        content = set(content_columns(cols))
        reads = searched(table)
        entry = {
            "name": table,
            "rows": row_count(conn, table),
            "key": key,
            "start": start,
            "end": end,
            "indexed": bool(key and content) and reads,
            # Set only where the flag above needs explaining: this table has a key
            # and prose columns, so search *would* read it, and does not because
            # Atlas wrote it. `moments` is the clearest case — it holds every
            # passage search can find and is not itself a source. A table search
            # skips for the ordinary reason that it has no text in it (`shot`) is
            # not labelled, because there is nothing surprising to explain.
            "own": bool(key and content) and not reads,
            "columns": [{
                "name": c["name"],
                "type": c["type"] or "TEXT",
                "pk": c["pk"],
                "role": ("key" if c["name"] == key else
                         "start" if c["name"] == start else
                         "end" if c["name"] == end else
                         "content" if c["name"] in content else "field"),
                # A channel label is a claim about what search calls this text.
                # On a table search never reads it would be a guess presented as
                # a fact — and on `moments` a wrong one, since that table carries
                # the real answer in its own `source` column.
                "source": (source_label(table, c["name"])
                           if reads and c["name"] in content else None),
            } for c in cols],
        }
        if samples:
            try:
                cur = conn.execute(
                    f'SELECT * FROM {_q(table)} LIMIT {int(samples)}')
                names = [d[0] for d in cur.description]
                entry["sample"] = [{n: cell_value(v) for n, v in zip(names, r)}
                                   for r in cur.fetchall()]
            except sqlite3.Error:
                entry["sample"] = []
        out["tables"].append(entry)
    return out


# ══════════════════════════════════════════════════════════════════════════
# KEYS
# ══════════════════════════════════════════════════════════════════════════
_ALL_DIGITS = re.compile(r"^\d+$")
# Namespace markers that carry no identity of their own.
_KEY_NAMESPACE = re.compile(r"^vios[:=]", re.I)
# Producers that spell a numeric Telegram message id with a prefix. Kept to an
# explicit short list on purpose — a generic "letters then digits" rule would
# also match Instagram shortcodes like `Cx1234` and throw away the letters that
# make them unique.
#
# `up_` is the capture ledger's key for a video a person uploaded to the channel
# by hand (`ledger.upload_key`), and the number in it *is* the message id. Left
# unfolded, one such video is two videos in Atlas — `1234` carrying the legacy
# captions and frame notes, `up_1234` carrying the new plane's claims and shots —
# and the asset manifests land under the spelling the reader never asks for.
_PREFIXED_NUMBER = re.compile(r"^(?:tg|msg|id|up)[_:-]?(\d+)$", re.I)


def normalize_key(value) -> str:
    """Reduce any spelling of a video's identity to one canonical form.

    Postgres says `tg1234`, lake.db says `1234` in `posts.video_id` and again
    in `videos.msg_id`, and a manifest says `"1234"`. They are the same reel,
    and collapsing them is what lets a narrative from one table and a
    transcript from another land on the same video without a mapping table.

    What this must NOT do is reduce an identifier to whatever digits it happens
    to contain. The capture plane keys rows by Instagram shortcode, and under a
    digit-extraction rule `REEL1` and `DBd2xyz` become `1` and `2` — so two
    unrelated reels merge into one video and their moments interleave. A
    shortcode is returned whole, and case-sensitively: Instagram's alphabet is
    base64-ish, so `Abc` and `aBc` are different posts.
    """
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(int(value))
    s = str(value).strip()
    if not s:
        return ""
    s = _KEY_NAMESPACE.sub("", s, count=1).strip()
    if _ALL_DIGITS.match(s):
        return s
    m = _PREFIXED_NUMBER.match(s)
    return m.group(1) if m else s
