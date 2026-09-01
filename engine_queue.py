"""
engine_queue.py — the local processing queue, and the thing that actually runs.

Kaggle's queue is 17,688 lines across several notebooks and needs Redis, leases
and twelve-hour timeout recovery, because it coordinates workers that cannot see
each other. This one is a single daemon thread and a sqlite table, because there
is one worker and it is in this process.

**What used to be here.** Between claiming a job and marking it done, this file
contained:

    # Execute pass (stubbed out / family runner hook)
    # In Phase 5.2 family runners execute here.
    # Once pass finishes, write shard and import
    time.sleep(0.05)  # Simulate execution yield

Fifty milliseconds, then `completed`. Every pass reported success, none of them
ran, and the lie could not be undone: `enqueue_job`'s upsert only re-runs a job
whose state is `failed` or `unrunnable`, so a re-queue skipped straight past
every fabricated success. `_repair()` is what clears those rows, and it can
identify them exactly — see its docstring.

**What is here now**, per job, in order:

  1. Claim it — `pending`/`deferred` → `running`, `attempts + 1`.
  2. Ask `runners.blocked()` whether this machine can host the pass at all. If
     not, `unrunnable` with the sentence, and the sentence is the point: *"no
     runner on this machine yet · the GPU-plane build needs faster_whisper"*,
     *"not installed on this machine: cv2 — pip install it and re-queue"* and
     *"ffmpeg is not on PATH"* are three different problems with three different
     fixes, and only the middle one is a `pip install`.
  3. Run it — `runners.run()`, which returns one of five states and never raises
     for an expected outcome.
  4. If it produced evidence, write a shard with `shardwriter.write_shard` and
     replay it with `ingest.import_local_shard`. Nothing writes to `atlas.db` by
     any other route, so evidence made on this laptop travels the same wire
     format as evidence made on Kaggle, through the same replay code.
  5. Record what happened — the state, the reason, the pass's own notes, the row
     count, and which shard holds them.

**Five states, not two.** `completed`, `skipped`, `deferred`, `failed`,
`unrunnable`. Collapsing them is what made the old queue useless as a report: a
reel with no audio track and a reel whose audio decoder crashed are not the same
event, and a coverage matrix reading *4,800 done, 200 skipped* is a finished
sweep while *4,800 done, 200 failed* is an unsolved problem.

**The index is rebuilt when the queue goes quiet**, not per job. `index.rebuild`
is a full rebuild — deliberately, see its docstring — and running it after each
of ten passes on each of five thousand reels would spend the entire sweep
rebuilding. So imports set a dirty flag, the worker rebuilds on the way into
idle, and a long sweep also rebuilds every `INDEX_EVERY` rows so the Search tab
is not frozen for hours. A claim is searchable within one idle tick of being
made, which for a queue that is draining is about ten seconds.

**Nothing here publishes to the channel.** `shardwriter.publish_shard` exists,
works, and is called by nobody — the same deliberate omission as
`capture.backfill.autostart()`. Uploading is outward-facing and it is the user's
decision, not a side effect of pressing Start. The shards are kept on disk
(`paths.SHARD_DIR`) precisely so that decision stays available.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

import paths
import runners
import shardwriter
from atlas import config, ingest
from logger import vios_log as log
from sizing import registry, resources
from sizing.base import ModelCache

SUB = "ENGINE"

# How many times a pass may ask for later before the queue stops believing it.
# `DeferPass` means "not now, nothing wrong" — a model warming up, a file still
# being written — and a pass that says it twelve times is not deferring, it is
# stuck, and a stuck job that stays `deferred` forever is invisible in a way a
# failed one is not.
MAX_DEFERS = 12

# The floor under a requested retry delay. A pass that defers with `retry_after=0`
# would otherwise be re-claimed in the same millisecond, and the worker would spin
# a core rewriting one row.
DEFER_MIN = 5.0

# How long a machine probe is trusted. `resources.probe()` shells out to
# `nvidia-smi`, so calling it per job is a subprocess per job for a number that
# only moves when a model loads or drops. Sixty seconds is short enough that a
# freed card is noticed within a pass or two and long enough that a queue of ten
# ffmpeg passes does not spawn ten processes to be told the same thing.
PROBE_TTL = 60.0

# Rows imported before the index is rebuilt mid-sweep, even though the queue is
# still busy. Without this the Search tab would show nothing new until a
# five-thousand-reel sweep finished.
INDEX_EVERY = 400

# How long to leave a *failing* rebuild alone. The idle tick comes round every
# ten seconds, and a rebuild that fails on the schema will fail identically every
# time, so without a back-off a broken index writes six log lines a minute for
# the life of the process and buries everything else.
INDEX_RETRY = 300.0

_LOCK = threading.RLock()
_RUNNING = False
_PAUSED = False
_STOP_EVENT = threading.Event()
_WAKE_EVENT = threading.Event()
_WORKER_THREAD: Optional[threading.Thread] = None

_CURRENT_JOB: Optional[Dict[str, Any]] = None
_CACHE: Optional[ModelCache] = None

_PROBE: Dict[str, Any] = {}
_PROBED_AT = 0.0

_DIRTY_ROWS = 0                       # evidence rows imported since last rebuild
_INDEX_NEXT_TRY = 0.0                 # back-off after a rebuild that failed


# ══════════════════════════════════════════════════════════════════════════
# THE QUEUE TABLE
# ══════════════════════════════════════════════════════════════════════════

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(paths.JOBS_DB, timeout=config.SQLITE_TIMEOUT)
    conn.row_factory = sqlite3.Row
    # Autocommit, so transactions are the ones this file asks for and not the
    # ones the driver infers. `_claim` needs `BEGIN IMMEDIATE` around its
    # select-then-update, and Python's implicit transaction handling opens a
    # deferred transaction on the UPDATE only — after the SELECT has already
    # read, which is exactly the window where two readers claim one job. The
    # `conn.commit()` calls elsewhere are left in place and harmless: with no
    # transaction open, commit is a no-op.
    conn.isolation_level = None
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    _init_tables(conn)
    return conn


# Columns added after the table shipped. Guarded rather than versioned because
# there is one deployment of this program and `ALTER TABLE ADD COLUMN` on sqlite
# is O(1) and cannot fail on a table that already has the column — the check is
# just to keep the log quiet.
_COLUMNS = (
    # A deferred job is not eligible until this time. Without it, `deferred`
    # would have to mean `pending`, and a pass that asks for thirty seconds
    # would be re-run immediately and forever.
    ("not_before", "REAL"),
    # The pass's own notes, as JSON. This is what makes the Engine tab worth
    # reading: "3 shots · asl 2.13 · metronomic" instead of a green dot.
    ("notes", "TEXT"),
    # Evidence rows the pass produced. Also the discriminator `_repair` uses to
    # find the mock's fabricated successes — see there.
    ("rows", "INTEGER"),
    # The shard those rows are in. Kept because "where did this claim come
    # from" is a real question and because publishing later needs the path.
    ("shard", "TEXT"),
)


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS local_jobs (
        job_id INTEGER PRIMARY KEY AUTOINCREMENT,
        video_key TEXT NOT NULL,
        component_id TEXT NOT NULL,
        -- 'pending' | 'running' | 'completed' | 'skipped' | 'deferred'
        --          | 'failed' | 'unrunnable'
        state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        -- The reason, whatever the state: the exception for a failure, the
        -- explanation for a skip, the shortfall for a held pass. Named `error`
        -- because it shipped that way and the column is read by the interface;
        -- `list_jobs` returns it under `reason` as well, which is the honest
        -- name, and the interface reads that one.
        error TEXT,
        created_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_local_jobs_key_comp
        ON local_jobs(video_key, component_id);
    CREATE INDEX IF NOT EXISTS idx_local_jobs_state ON local_jobs(state);
    """)
    have = {r[1] for r in conn.execute("PRAGMA table_info(local_jobs)")}
    for name, decl in _COLUMNS:
        if name not in have:
            conn.execute(f'ALTER TABLE local_jobs ADD COLUMN "{name}" {decl}')
    conn.commit()


def _repair() -> dict:
    """Undo the mock's fabricated successes and free jobs a crash left running.

    Two repairs, both idempotent, both run once at `start()`.

    **The fabricated successes.** Every job the stubbed worker touched is
    `completed` with no evidence behind it, and because the upsert in
    `enqueue_job` only re-runs `failed` and `unrunnable`, re-queueing could never
    reach them. They are identifiable exactly: `rows` is a column that did not
    exist while the mock did, so `state='completed' AND rows IS NULL` is
    precisely "completed before this file could complete anything". A real run
    always writes `rows`, including when the honest answer is 0, so this cannot
    catch genuine work — and the ambiguity does not recur, because after this
    runs there are no NULL-`rows` completions left to mistake.

    **The abandoned claims.** A job is marked `running` before the pass starts
    and rewritten after it ends, so a process killed mid-pass leaves a row that
    says `running` with nobody running it. It would sit there forever: the picker
    looks for `pending` and `deferred`, and the stats strip would show a worker
    permanently busy with a job that died. Reset to `pending` with the attempt
    already counted, so a reel that reliably kills the process runs out of
    attempts instead of looping.
    """
    conn = _get_db()
    try:
        mock = conn.execute(
            "UPDATE local_jobs SET state='pending', error=NULL, attempts=0, "
            "started_at=NULL, finished_at=NULL "
            "WHERE state='completed' AND rows IS NULL").rowcount
        orphan = conn.execute(
            "UPDATE local_jobs SET state='pending', not_before=NULL, "
            "error='the worker stopped while this was running' "
            "WHERE state='running'").rowcount
        conn.commit()
    finally:
        conn.close()

    if mock:
        log(f"reset {mock} job(s) the stubbed worker had marked completed "
            f"without running anything — they will run for real", SUB, "WARN")
    if orphan:
        log(f"released {orphan} job(s) left running by a stopped worker", SUB)
    return {"mock": mock, "orphaned": orphan}


# ══════════════════════════════════════════════════════════════════════════
# PUTTING WORK IN
# ══════════════════════════════════════════════════════════════════════════

def enqueue_job(video_key: str, component_id: str, force: bool = False) -> bool:
    """Add one pass on one reel. Returns whether a row is now pending.

    Re-queues `failed`, `unrunnable`, `skipped` and `deferred`, and leaves
    `completed` and `running` alone. `skipped` belongs in that list and did not
    used to be reachable: `cuts` skips with *"no shots for this reel yet"*, and
    once a shot pass has run the correct thing for a re-queue to do is run it.

    `force=True` re-runs a completed pass too, which is what a deliberate
    "run this again" means and the only way to reproduce a measurement whose
    inputs have changed. It still will not disturb a `running` job.
    """
    guard = "" if force else \
        " WHERE state IN ('failed','unrunnable','skipped','deferred')"
    conn = _get_db()
    try:
        cur = conn.execute(f"""
            INSERT INTO local_jobs (video_key, component_id, state, created_at)
            VALUES (?, ?, 'pending', ?)
            ON CONFLICT(video_key, component_id) DO UPDATE SET
                state = 'pending',
                error = NULL,
                attempts = 0,
                not_before = NULL,
                started_at = NULL,
                finished_at = NULL
            {guard}
        """, (str(video_key), str(component_id), time.time()))
        conn.commit()
        _WAKE_EVENT.set()
        return cur.rowcount > 0
    except Exception as e:                                       # noqa: BLE001
        log(f"failed to enqueue {video_key}:{component_id} — {e}", SUB, "WARN")
        return False
    finally:
        conn.close()


def enqueue_video(video_key: str, component_ids: Optional[List[str]] = None,
                  force: bool = False) -> Dict[str, Any]:
    """Enqueue passes for one reel. Returns what was queued and what was not.

    **An implicit sweep queues what can actually run; an explicit request is
    answered even when the answer is no.** With no `component_ids`, this queues
    the passes that have a runner here and are not blocked — ten today, so a
    sweep over the library puts ten rows per reel in the queue rather than fifty,
    forty of which would exist only to be marked `unrunnable`. Name the ids and
    every one of them is queued, including the ones this machine cannot host,
    because someone who asks for `transcribe` deserves the row that says *"not
    installed on this machine: faster_whisper"* rather than silence.

    **Insertion order is dependency order**, and that is not cosmetic. The
    picker takes jobs by `job_id`, so the order rows are inserted in is the order
    they run in. `runners.order` is what supplies it, because the catalogue's own
    `needs` cannot: it says `cuts` needs `shots`, the GPU pass, while the
    component that finds boundaries here is `shots-cpu`. Sorted by the catalogue,
    `cuts` runs first and skips on every reel in the archive.

    Returns `{"enqueued", "already", "blocked", "order"}`. `already` is the count
    that needed nothing done, which is the difference between "this reel is
    processed" and "nothing happened", and the interface says which.
    """
    res = _probe()
    blocked = runners.blocked(None, res)

    if component_ids:
        want = [c for c in dict.fromkeys(component_ids) if c in registry.BY_ID]
        unknown = [c for c in dict.fromkeys(component_ids)
                   if c not in registry.BY_ID]
        if unknown:
            log(f"ignoring unknown component(s) {', '.join(unknown)}", SUB,
                "WARN")
    else:
        want = [c for c in registry.defaults()
                if c in runners.HANDLERS and c not in blocked]

    plan = runners.order(want)
    queued = sum(1 for cid in plan if enqueue_job(video_key, cid, force))
    return {"enqueued": queued, "already": len(plan) - queued,
            "blocked": {c: blocked[c] for c in plan if c in blocked},
            "order": plan}


# ══════════════════════════════════════════════════════════════════════════
# READING THE QUEUE
# ══════════════════════════════════════════════════════════════════════════

def list_jobs(state: Optional[str] = None,
              limit: int = 100) -> List[Dict[str, Any]]:
    """Jobs, newest activity first. `reason` and `notes` are decoded for the UI.

    Ordered by when something last happened to a job rather than when it was
    created, because a queue being watched is a queue whose interesting rows are
    the ones that just moved. `created_at` breaks the tie for a batch of rows
    enqueued in the same instant, which preserves dependency order within a reel.
    """
    conn = _get_db()
    try:
        sql = ("SELECT * FROM local_jobs {} ORDER BY "
               "COALESCE(finished_at, started_at, created_at) DESC, "
               "created_at DESC LIMIT ?")
        if state:
            cur = conn.execute(sql.format("WHERE state = ?"),
                               (state, int(limit)))
        else:
            cur = conn.execute(sql.format(""), (int(limit),))
        return [_shape(dict(r)) for r in cur.fetchall()]
    finally:
        conn.close()


def _shape(row: dict) -> dict:
    """One job row as the interface wants it.

    `reason` rather than `error` because four of the five terminal states are not
    errors and calling a skip one is the kind of small dishonesty this program is
    built to avoid. `error` stays in the payload — dropping a field the interface
    already reads would be a regression for nothing.
    """
    row["reason"] = row.get("error") or ""
    notes = row.get("notes")
    if notes:
        try:
            row["notes"] = json.loads(notes)
        except (TypeError, ValueError):
            row["notes"] = {"note": str(notes)}
    else:
        row["notes"] = None
    return row


def get_queue_stats() -> Dict[str, Any]:
    """Counts per state plus what the worker is doing, for the status strip."""
    conn = _get_db()
    try:
        counts = {r["state"]: r["c"] for r in conn.execute(
            "SELECT state, count(*) AS c FROM local_jobs GROUP BY state")}
    finally:
        conn.close()
    return {
        "pending": counts.get("pending", 0),
        "running": counts.get("running", 0),
        "completed": counts.get("completed", 0),
        "skipped": counts.get("skipped", 0),
        "deferred": counts.get("deferred", 0),
        "failed": counts.get("failed", 0),
        "unrunnable": counts.get("unrunnable", 0),
        "current_job": _CURRENT_JOB,
        "running_worker": _RUNNING,
        "paused": _PAUSED,
        "runners": len(runners.have()),
        "index_pending": _DIRTY_ROWS,
    }


# ══════════════════════════════════════════════════════════════════════════
# THE WORKER
# ══════════════════════════════════════════════════════════════════════════

def _probe() -> dict:
    """The measured machine, cached for `PROBE_TTL`. `{}` if the probe raised.

    A failed probe reads downstream as a machine with no GPU, because
    `registry.unrunnable` derives `gpus` from a key that is absent — so every GPU
    pass comes back held with *"no GPU in this session"* rather than with the
    truth, which is that nothing was measured. That is tolerable here and only
    here: the ten components with a runner on this machine are all `device="cpu"`
    and are never held on a hardware ground, so a failed probe cannot stop any
    pass that could actually have run. The WARN is what makes the difference
    visible; the reason string on the held rows would not.
    """
    global _PROBED_AT                                            # noqa: PLW0603
    if _PROBE and (time.time() - _PROBED_AT) < PROBE_TTL:
        return dict(_PROBE)
    try:
        fresh = resources.probe(paths.HOME)
    except Exception as e:                                       # noqa: BLE001
        log(f"machine probe failed — {type(e).__name__}: {e}", SUB, "WARN")
        return {}
    _PROBE.clear()
    _PROBE.update(fresh)
    _PROBED_AT = time.time()
    return dict(_PROBE)


def _claim() -> Optional[dict]:
    """Take the next eligible job and mark it running. None when there is none.

    `pending` and `deferred` together, filtered by `not_before`, so a pass that
    asked for thirty seconds gets thirty seconds and stays visible as deferred
    while it waits. Ordered by `job_id`, which is why `enqueue_video` inserts in
    dependency order — see there.

    `BEGIN IMMEDIATE` takes the database's write lock before the SELECT, which
    makes reading a row and marking it `running` one indivisible act. There is one
    worker thread in this process, so the reader this defends against is a second
    *process*: nothing stops a user launching the app twice, and both instances
    open the same `jobs.db`. Without the lock they would both read job 41, both
    run it, and both write shards of the same measurement.
    """
    conn = _get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""
            SELECT * FROM local_jobs
            WHERE state IN ('pending', 'deferred')
              AND (not_before IS NULL OR not_before <= ?)
            ORDER BY job_id ASC LIMIT 1
        """, (time.time(),)).fetchone()
        if not row:
            conn.execute("ROLLBACK")
            return None
        job = dict(row)
        conn.execute(
            "UPDATE local_jobs SET state='running', started_at=?, "
            "attempts=attempts+1, error=NULL, not_before=NULL "
            "WHERE job_id=?", (time.time(), job["job_id"]))
        conn.execute("COMMIT")
        job["attempts"] = (job.get("attempts") or 0) + 1
        return job
    except sqlite3.OperationalError as e:
        # Another instance holds the write lock past `SQLITE_TIMEOUT`. Not an
        # error to record against any job — there is no job yet — so report it
        # and let the idle wait run; the row will still be there next tick.
        log(f"could not claim a job — {e}", SUB, "WARN")
        return None
    finally:
        conn.close()


def _finish(job_id: int, state: str, reason: str = "", notes: dict = None,
            rows: int = None, shard: str = "", retry_after: float = 0.0) -> None:
    """Write a job's outcome. The only place a terminal state is set."""
    conn = _get_db()
    try:
        conn.execute(
            "UPDATE local_jobs SET state=?, error=?, notes=?, rows=?, "
            "shard=?, finished_at=?, not_before=? WHERE job_id=?",
            (state, reason or None,
             json.dumps(notes, sort_keys=True, default=str) if notes else None,
             rows, shard or None, time.time(),
             (time.time() + max(retry_after, DEFER_MIN))
             if state == "deferred" else None,
             int(job_id)))
        conn.commit()
    finally:
        conn.close()


def _worker_loop() -> None:
    global _RUNNING, _CURRENT_JOB, _CACHE                        # noqa: PLW0603
    log("engine queue worker started", SUB)

    # One atlas connection and one model cache for the thread's whole life. The
    # cache is what stops a model being reloaded per reel; the connection is
    # opened here rather than per job because `ensure_keys` must run before the
    # first shard is replayed, and running it once is the point of it.
    atlas = ingest.connect()
    _CACHE = ModelCache(log=lambda m, level="info": log(m, SUB))
    try:
        made = runners.ensure_keys(atlas)
        if made:
            log(f"declared evidence table(s) {', '.join(made)} with their keys "
                f"before any shard could infer them", SUB)
    except Exception as e:                                       # noqa: BLE001
        log(f"could not pin the evidence keys — {type(e).__name__}: {e}",
            SUB, "ERROR")

    try:
        while not _STOP_EVENT.is_set():
            if _PAUSED:
                _CURRENT_JOB = None
                _WAKE_EVENT.wait(timeout=5.0)
                _WAKE_EVENT.clear()
                continue

            job = _claim()
            if not job:
                _CURRENT_JOB = None
                _reindex(atlas, idle=True)
                _WAKE_EVENT.wait(timeout=10.0)
                _WAKE_EVENT.clear()
                continue

            # `state='running'` rather than the row's own value: `_claim` read
            # this row before it wrote the state, so the dict still says
            # `pending` and the status strip was labelling the pass it is
            # watching run as queued.
            _CURRENT_JOB = dict(job, state="running", detail="starting")
            try:
                _execute(job, atlas)
            except (KeyboardInterrupt, SystemExit):
                # The job stays `running`; `_repair()` releases it next start.
                # Rewriting it as failed here would record a shutdown as a
                # defect in the pass.
                raise
            except Exception as e:                               # noqa: BLE001
                log(f"{job['video_key']}:{job['component_id']} — the queue "
                    f"itself failed: {type(e).__name__}: {e}", SUB, "ERROR")
                _finish(job["job_id"], "failed",
                        f"the queue could not complete this job — "
                        f"{type(e).__name__}: {e}", rows=0)
            _reindex(atlas)
    finally:
        _RUNNING = False
        _CURRENT_JOB = None
        try:
            atlas.close()
        except Exception:                                        # noqa: BLE001
            pass
        log("engine queue worker stopped", SUB)


def _execute(job: dict, atlas: sqlite3.Connection) -> None:
    """Run one job to a terminal state. Records every outcome, raises none.

    The order of the two checks matters. `runners.blocked` is asked first and
    with this machine's measurements, because a pass that cannot be hosted should
    not open the video file to find that out — and because the reason it gives is
    better than any exception the pass would raise. Only then is the pass run.

    A pass that produced evidence is not `completed` until the evidence is *in*
    the database. If the shard will not write or will not replay, the job is
    `failed` with that reason, and it is honest: the measurement happened, and
    nothing anywhere can read it, which is indistinguishable from the
    measurement not having happened. Marking it done would leave the queue
    claiming coverage that the reel does not have.
    """
    global _DIRTY_ROWS                                           # noqa: PLW0603
    key, cid = job["video_key"], job["component_id"]
    started = time.time()

    held = runners.blocked([cid], _probe())
    if cid in held:
        _finish(job["job_id"], "unrunnable", held[cid], rows=0)
        log(f"{key}:{cid} held — {held[cid]}", SUB)
        return

    log(f"running {key} · {cid}", SUB)
    res = runners.run(cid, key, atlas, log=lambda m: log(m, SUB),
                      progress=lambda m: _progress(job, m), cache=_CACHE)
    state, reason, notes = res["state"], res["reason"], res.get("notes") or {}

    if state == "deferred" and (job.get("attempts") or 0) >= MAX_DEFERS:
        _finish(job["job_id"], "failed",
                f"asked to be retried {job['attempts']} times without ever "
                f"becoming runnable — last reason: {reason}", notes, rows=0)
        log(f"{key}:{cid} deferred {job['attempts']} times, giving up", SUB,
            "WARN")
        return

    if state != "completed":
        _finish(job["job_id"], state, reason, notes, rows=0,
                retry_after=float(res.get("retry_after") or 0.0))
        level = "ERROR" if state == "failed" else "INFO"
        log(f"{key}:{cid} {state} — {reason}", SUB, level)
        return

    tables = res["tables"]
    if not tables:
        # A pass that finished and measured nothing. Not a failure and not a
        # skip: it ran, it had nothing to say, and `rows=0` records that
        # distinctly from both.
        _finish(job["job_id"], "completed", "", notes, rows=0)
        log(f"{key}:{cid} done in {res['seconds']}s — no rows", SUB)
        return

    try:
        shard = shardwriter.write_shard(
            tables, dest_dir=os.path.join(paths.SHARD_DIR, _bucket(key)),
            session_id=f"{_tag(key)}_{_tag(cid)}_{int(started)}")
    except Exception as e:                                       # noqa: BLE001
        _finish(job["job_id"], "failed",
                f"measured {res['rows']} row(s) and could not write the shard "
                f"— {type(e).__name__}: {e}", notes, rows=0)
        return

    out = ingest.import_local_shard(shard, atlas)
    if not out.get("ok"):
        _finish(job["job_id"], "failed",
                f"wrote {res['rows']} row(s) to {os.path.basename(shard)} and "
                f"could not replay them — {out.get('note') or 'unknown'}",
                notes, rows=0, shard=shard)
        return

    added = sum((out.get("rows") or {}).values())
    lost = int(out.get("lost") or 0)
    if lost:
        # The replay said `ok` and still did not keep everything — a payload
        # insert that raised, or a `video` row whose key no witness could resolve.
        # Not `failed`: most of the shard landed, and marking the job failed would
        # re-run the whole pass to re-measure rows that are already in the
        # database. Recorded instead, in the two places that outlive this call.
        #
        # Recoverable without re-measuring anything, which is why this is a note
        # and not an error: `import_local_shard` is called with `keep=True` by
        # default, so the file is still under `paths.SHARD_DIR` and replaying it
        # again once the failing insert is fixed lands exactly these rows —
        # `_enrich` fills NULL columns of rows that already exist, so the
        # already-imported majority is not duplicated by the second attempt.
        notes = dict(notes, lost_rows=lost, shard_kept=shard)
        log(f"{key}:{cid} replayed with {lost} row(s) lost — the shard is kept "
            f"at {os.path.basename(shard)} and can be replayed again", SUB,
            "WARN")
    _DIRTY_ROWS += added
    _finish(job["job_id"], "completed", "", notes, rows=res["rows"],
            shard=shard)
    log(f"{key}:{cid} done in {res['seconds']}s — {res['rows']} row(s), "
        f"{added} new", SUB, "SUCCESS")


def _progress(job: dict, message: str) -> None:
    """Carry a pass's live line into the stats payload the strip polls."""
    global _CURRENT_JOB                                          # noqa: PLW0603
    cur = _CURRENT_JOB
    if cur and cur.get("job_id") == job.get("job_id"):
        _CURRENT_JOB = dict(cur, detail=str(message)[:200])


def _bucket(key: str) -> str:
    """Two characters of the reel's key, as a subdirectory.

    Ten shards per reel over an archive of thousands is tens of thousands of
    small files, and a single NTFS directory that size is slow to enumerate for
    every tool that ever looks at it. Two hex characters is 256 buckets, which
    keeps each one small without making the tree deep enough to navigate.
    """
    t = _tag(key)
    return (t[4:6] if t.startswith("loc_") else t[:2]) or "00"


def _tag(s: str) -> str:
    """A filename-safe spelling. Shard names carry the key and the pass id, and
    `shots-cpu` has a hyphen the wire format's own name pattern uses as a
    separator."""
    return "".join(c if (c.isalnum() or c == "_") else "_" for c in str(s))


def _reindex(conn: sqlite3.Connection, idle: bool = False) -> None:
    """Rebuild `moments` and `video_index` when it is worth doing.

    On the way into idle, always — the sweep is over and the claims should be
    searchable. Mid-sweep, only once `INDEX_EVERY` rows have accumulated, and
    without embeddings: a text rebuild is seconds, and re-embedding every passage
    ten times during one sweep is not. The idle rebuild is the one that embeds.

    `index.rebuild` returns `ok: False` for two unrelated reasons and they need
    different handling. A build already running is nothing: the operator started
    one from the Data tab, this one steps aside, and the dirty counter is left
    alone so the next tick tries again. A build that *failed* is a problem, and
    the failure this hits in practice is a schema older than the writer — the
    rebuild's `INSERT` names twenty columns of `video_index` and `ensure_schema`
    creates that table but never widens it, so one missing column fails every
    rebuild from then on. Reported, and then not retried for `INDEX_RETRY`, or a
    permanently broken schema would put a stack of the same line in the log every
    ten seconds for as long as the app is open.

    Either way the counter survives, so nothing is lost by waiting: the rows are
    already in the database and the rebuild reads all of them.
    """
    global _DIRTY_ROWS, _INDEX_NEXT_TRY                          # noqa: PLW0603
    if not _DIRTY_ROWS or (not idle and _DIRTY_ROWS < INDEX_EVERY):
        return
    if time.time() < _INDEX_NEXT_TRY:
        return
    try:
        from atlas import index                                  # noqa: PLC0415
        out = index.rebuild(conn, embed=bool(idle))
    except Exception as e:                                       # noqa: BLE001
        log(f"index rebuild raised — {type(e).__name__}: {e}", SUB, "WARN")
        _INDEX_NEXT_TRY = time.time() + INDEX_RETRY
        return
    if not out.get("ok"):
        note = out.get("note") or "unknown"
        if "already running" in note:
            return                         # the Data tab is rebuilding; wait
        log(f"index rebuild failed, {_DIRTY_ROWS} row(s) still unsearchable — "
            f"{note}", SUB, "ERROR")
        _INDEX_NEXT_TRY = time.time() + INDEX_RETRY
        return
    log(f"reindexed after {_DIRTY_ROWS} new row(s) — "
        f"{out.get('moments', 0)} moment(s) over {out.get('videos', 0)} reel(s)",
        SUB)
    _DIRTY_ROWS = 0
    _INDEX_NEXT_TRY = 0.0
    if idle:
        _regraph(conn)


def _regraph(conn: sqlite3.Connection) -> None:
    """Re-derive the relationship graph after the sweep's index rebuild.

    The graph reads the same claims the index just read, so it goes stale at
    exactly the moment the index does — and it has no dirty counter of its own to
    notice. Boot rebuilds both together (`atlas/server.py:89-93`); the engine
    rebuilt only the index, so a first local sweep left the Graph tab saying *no
    relationships found in this database* over a library it had just finished
    processing, until someone pressed rebuild by hand. Measured on three reels:
    88 claims indexed, graph 0 nodes, then 35 nodes and 63 edges from one manual
    POST with nothing else changed.

    Idle only, unlike the mid-sweep text rebuild. This is derivation from rows
    already written, not new evidence, and doing it between jobs would redo the
    whole graph every `INDEX_EVERY` rows for a result nobody can see until the
    sweep ends.

    Never fatal, for the same reason boot's copy is not: an archive with a stale
    graph is still searchable, still playable, still every other tab.
    """
    try:
        from atlas import graph                                  # noqa: PLC0415
        out = graph.rebuild(conn)
    except Exception as e:                                       # noqa: BLE001
        log(f"graph rebuild raised — {type(e).__name__}: {e}", SUB, "WARN")
        return
    if out.get("ok"):
        log(f"graph rebuilt — {out.get('nodes', 0)} node(s), "
            f"{out.get('edges', 0)} edge(s)", SUB)
    else:
        log(f"graph rebuild skipped — {out.get('note') or 'unknown'}", SUB)


# ══════════════════════════════════════════════════════════════════════════
# CONTROL
# ══════════════════════════════════════════════════════════════════════════

def start() -> None:
    """Start the worker, or revive one that was asked to stop. Repairs first.

    Guarded on the thread being *alive* rather than on `_RUNNING`, and the
    difference is a bug that was reachable from the interface. `stop()` cannot
    interrupt a pass — see there — so between the click and the thread actually
    ending there is a window of up to a minute where a stop has been requested
    and a worker is still running. A flag-based guard reads that window as "not
    running" and starts a second thread, and two threads on one queue means the
    same job claimed twice.

    So a start that lands in that window revives the existing thread instead:
    clear the stop, unpause, wake it. It never got as far as its `finally`, so
    there is nothing to restart.
    """
    global _RUNNING, _WORKER_THREAD, _PAUSED                     # noqa: PLW0603
    with _LOCK:
        _PAUSED = False    # a worker started after a paused stop must wake
        if _WORKER_THREAD is not None and _WORKER_THREAD.is_alive():
            _STOP_EVENT.clear()
            _RUNNING = True
            _WAKE_EVENT.set()
            return
        _repair()
        _STOP_EVENT.clear()
        _RUNNING = True
        _WORKER_THREAD = threading.Thread(target=_worker_loop,
                                          name="vios-engine", daemon=True)
        _WORKER_THREAD.start()


def stop() -> None:
    """Ask the worker to stop after the pass it is on.

    There is no way to interrupt a pass mid-decode and no attempt to invent one:
    ffmpeg is in a subprocess and killing it would leave a half-written frame
    strip that the next run would have to detect. The thread is a daemon, so
    process exit does not wait for it, and a job caught that way is released by
    `_repair()` on the next start rather than being recorded as failed.

    `_RUNNING` is deliberately *not* cleared here. It means "a worker thread is
    alive", the thread's own `finally` is what clears it, and reporting the worker
    as stopped while a pass is still decoding would put a lie in the status strip
    and open the double-start window `start()` documents.
    """
    _STOP_EVENT.set()
    _WAKE_EVENT.set()


def pause() -> None:
    global _PAUSED                                               # noqa: PLW0603
    _PAUSED = True
    _WAKE_EVENT.set()


def resume() -> None:
    global _PAUSED                                               # noqa: PLW0603
    _PAUSED = False
    _WAKE_EVENT.set()
