"""
dbhealth — a database that cannot be opened must not stop the application.

This module exists because of a measured failure. On 30 August the reader's
database reached a state where *every fresh connection* raised

    sqlite3.DatabaseError: malformed database schema (ix_vecpay_space)
      - no such table: main.vec_payload

while the already-running process kept serving `/api/library` perfectly. That
is not a contradiction: sqlite parses `sqlite_master` once per connection and
caches the result, so a process that opened its connections before the damage
was written never sees it. The consequence is the dangerous part — the app
looked completely healthy right up until it was restarted, and then it would
have failed to open its own database on boot, with no way forward that a user
could reach from the UI.

Dumping `sqlite_master` under `PRAGMA writable_schema=ON` showed indexes whose
tables were absent and a b-tree walk that bled into another table's row data in
the type/name/rootpage columns. That is physical page damage, not a DDL ordering
mistake, and there is no in-place repair worth attempting from inside a boot
sequence.

So this module takes the other route, and it is the route this application is
uniquely able to take: **throw the database away and rebuild it.** Every byte in
`atlas.db` was either downloaded from the Telegram channel or computed from
something that was, which is the same property that makes deleting HOME a
supported operation (see `paths.py`). A corrupt reader database is therefore not
a data-loss event, it is a cache miss that costs time.

Three rules, each of which was a way to make the situation worse:

  1. **Quarantine, never delete.** The file moves to `HOME/quarantine/` with a
     timestamp and a note saying what was wrong with it. A future sqlite build,
     or `sqlite3 .recover`, may read what this one cannot, and the file is the
     only copy of whatever the last pass computed. Disk is not the constraint
     here; an unrecoverable mistake is.

  2. **The write-ahead log goes with it.** A `-wal` and `-shm` left behind next
     to a *new* empty database is a second corruption, freshly manufactured:
     sqlite would replay committed frames belonging to pages that no longer mean
     what they meant. All three files move together or none do.

  3. **Derived sidecars go with it too.** `moments.vec` is a flat matrix whose
     row order is defined by rows in `atlas.db`. Keeping it beside a rebuilt
     database means search would answer with vectors belonging to moments that
     no longer exist — wrong answers, silently, which is worse than no answers.
     They are rebuilt by the same pass that refills the tables.

The check itself is one statement. `select count(*) from sqlite_master` forces
sqlite to prepare the schema, which is exactly the step that raises on the
damage above, so the probe and the symptom are the same event. `integrity_check`
is deliberately not used: it walks every b-tree, it is slow on a large archive,
and on the real corrupt file it failed for an unrelated reason
(`vtable constructor failed: moments_fts`) — a boot check that cannot finish is
a boot check that gets removed.
"""

from __future__ import annotations

import os
import sqlite3
import time

import paths

# Files that describe rows inside a database and are meaningless without them.
# Keyed by database path so `quarantine()` can move a set, not a file.
_SIDECARS = {
    "atlas.db": ("moments.vec", "moments.vec.json"),
}

# What sqlite appends to a database path for the write-ahead log and its shared
# memory index. Both are recreated on demand; neither may outlive its database.
_JOURNALS = ("-wal", "-shm", "-journal")


def probe(path: str) -> str:
    """"" if this database can be opened and read, else why it cannot.

    Read-only via URI, so probing never creates a file and never writes a
    journal into a directory we are about to move. A path that does not exist is
    healthy by definition — a database the app has not built yet is the normal
    state on a fresh machine, and treating absence as damage would quarantine
    empty files forever.
    """
    if not os.path.exists(path):
        return ""
    try:
        if os.path.getsize(path) == 0:
            # Zero bytes is a valid empty sqlite file as far as sqlite cares; it
            # will initialise it on first write. Nothing to quarantine.
            return ""
    except OSError:
        return ""
    conn = None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10.0)
        # Forces the schema parse. This is the statement that raises
        # "malformed database schema" on the file that motivated this module.
        conn.execute("select count(*) from sqlite_master").fetchone()
        conn.execute("pragma user_version").fetchone()
        return ""
    except sqlite3.DatabaseError as exc:
        return f"{type(exc).__name__}: {exc}"
    except sqlite3.Error as exc:
        # OperationalError for "unable to open database file" — a locked or
        # permission-denied file is not corrupt, and quarantining it would move
        # a perfectly good archive out from under a running instance.
        text = str(exc).lower()
        if "malformed" in text or "not a database" in text or "corrupt" in text:
            return f"{type(exc).__name__}: {exc}"
        return ""
    except OSError:
        return ""
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


def _move_aside(src: str, dest_dir: str) -> str:
    """Move one file into `dest_dir`, timestamped. Returns "" if it did not move."""
    if not os.path.exists(src):
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = os.path.basename(src)
    dest = os.path.join(dest_dir, f"{stamp}-{base}")
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(dest_dir, f"{stamp}-{n}-{base}")
        n += 1
    try:
        os.replace(src, dest)
        return dest
    except OSError:
        return ""


def quarantine(path: str, reason: str) -> str:
    """Move a database, its journals and its sidecars aside. Returns a note.

    The order matters: the database moves first, and the journals follow. If the
    process dies between the two, what is left behind is a `-wal` with no
    database, which sqlite ignores — the harmless residue. Moving the journals
    first would leave a database with no log, which sqlite reads as complete
    while missing every committed frame the log still held.
    """
    os.makedirs(paths.QUARANTINE_DIR, exist_ok=True)
    moved = []
    got = _move_aside(path, paths.QUARANTINE_DIR)
    if not got:
        # Could not move it — almost always because something in this process,
        # or another instance, still holds it open. Say so and change nothing:
        # a half-quarantined database is the state this module exists to avoid.
        return (f"could not move the damaged {os.path.basename(path)} aside "
                f"(in use?) — {reason}")
    moved.append(got)
    for suffix in _JOURNALS:
        side = _move_aside(path + suffix, paths.QUARANTINE_DIR)
        if side:
            moved.append(side)
    for name in _SIDECARS.get(os.path.basename(path).lower(), ()):
        side = _move_aside(os.path.join(os.path.dirname(path), name),
                          paths.QUARANTINE_DIR)
        if side:
            moved.append(side)
    try:
        with open(got + ".why.txt", "w", encoding="utf-8") as fh:
            fh.write(
                f"{os.path.basename(path)} could not be opened on "
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}.\n\n"
                f"{reason}\n\n"
                "It was moved here so the application could start with a fresh\n"
                "one and rebuild from the Telegram channel. Nothing was lost\n"
                "that was not either downloaded from the channel or computed\n"
                "from something that was.\n\n"
                "Deleting this folder is safe. It is kept only in case a future\n"
                "sqlite build, or `sqlite3 broken.db .recover`, can read what\n"
                "this one could not.\n\n"
                "Moved together:\n"
                + "".join(f"  {os.path.basename(m)}\n" for m in moved))
    except OSError:
        pass
    return (f"{os.path.basename(path)} was damaged and has been moved to "
            f"quarantine/ — rebuilding it from the channel. ({reason})")


def ensure_usable(path: str) -> str:
    """Probe, quarantine if damaged, return a note for the log ("" if healthy)."""
    reason = probe(path)
    if not reason:
        return ""
    return quarantine(path, reason)


def boot() -> list:
    """Check every database this application owns. Returns notes for the log.

    Called once, at module import time from `server/app.py`, before any router
    is imported — because a router import can reach a `connect()` at import
    time, and a check that runs after the first connection is a check that runs
    after the crash it exists to prevent.
    """
    notes = []
    for db in (paths.DB_PATH, paths.JOBS_DB, paths.LIBRARY_DB, paths.MIRROR_DB,
               os.path.join(paths.HOME, "capture_ledger.db")):
        try:
            note = ensure_usable(db)
        except Exception as exc:                    # never block a boot
            note = f"database check failed for {os.path.basename(db)}: {exc}"
        if note:
            notes.append(note)
    return notes


def quarantined() -> list:
    """What is sitting in quarantine, for the Admin panel to show and offer."""
    out = []
    try:
        for e in os.scandir(paths.QUARANTINE_DIR):
            if not e.is_file() or e.name.endswith(".why.txt"):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            out.append({"name": e.name, "bytes": st.st_size,
                        "at": st.st_mtime})
    except OSError:
        return []
    return sorted(out, key=lambda d: d["at"], reverse=True)
