"""
library — local video library support: watched folders, indexed in place.

Two sources feed this application:
  1. The Telegram channel (the cloud archive, synced via mirror.py).
  2. Local folders on this machine (indexed in place, never copied).

Key Invariants:
  - **Indexed in place, never copied**: A 30 GB folder of local videos is indexed
    where it sits. Copying files would double disk usage for no benefit.
  - **Identity by content hash**: sha256 of first 1 MiB + last 1 MiB + exact byte size.
    Computes in milliseconds even on large files and stays stable across renames and moves.
  - **Derived artefacts live in paths.HOME**: Proxies, sprites, posters, and keyframes
    land under paths.MEDIA_DIR, addressed by video_key. The user's original directory
    is read-only and never modified.
  - **Resilience to moves/deletions**: If a file is moved/renamed, its hash matches
    and the path updates with zero re-processing. If a file vanishes, it is marked
    'missing' rather than deleted, preserving all claims and notes.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import derive
import paths
from atlas import config
from atlas.media import safe_name
from logger import vios_log as log

SUB = "LIBRARY"

# Video extensions recognized for indexing
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".wmv", ".ts", ".3gp"
})

_LOCK = threading.RLock()


def _get_db() -> sqlite3.Connection:
    """Connect to library.db with timeout and WAL mode."""
    conn = sqlite3.connect(paths.LIBRARY_DB, timeout=config.SQLITE_TIMEOUT)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    _init_tables(conn)
    return conn


def _init_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS watched_folders (
        folder_id INTEGER PRIMARY KEY AUTOINCREMENT,
        path TEXT UNIQUE NOT NULL,
        added_at REAL NOT NULL,
        last_scanned_at REAL,
        enabled INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS local_videos (
        file_hash TEXT PRIMARY KEY,
        video_key TEXT UNIQUE NOT NULL,
        path TEXT NOT NULL,
        filename TEXT NOT NULL,
        size_bytes INTEGER NOT NULL,
        mtime REAL NOT NULL,
        duration REAL,
        width INTEGER,
        height INTEGER,
        fps REAL,
        status TEXT NOT NULL DEFAULT 'active', -- 'active' | 'missing'
        first_indexed_at REAL NOT NULL,
        last_seen_at REAL NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_local_videos_path ON local_videos(path);
    CREATE INDEX IF NOT EXISTS idx_local_videos_status ON local_videos(status);
    """)
    conn.commit()


def compute_content_hash(path: str) -> str:
    """Fast, stable content hash: sha256(first 1MB + last 1MB + byte_size).

    Takes <5ms even on 10 GB video files and remains identical across renames.
    """
    try:
        size = os.path.getsize(path)
        if size == 0:
            return ""

        h = hashlib.sha256()
        h.update(str(size).encode("utf-8"))

        chunk_size = 1024 * 1024  # 1 MiB
        with open(path, "rb") as f:
            # First 1MB
            first_chunk = f.read(chunk_size)
            h.update(first_chunk)

            # Last 1MB (if file is larger than 1MB)
            if size > chunk_size:
                f.seek(max(0, size - chunk_size))
                last_chunk = f.read(chunk_size)
                h.update(last_chunk)

        return h.hexdigest()
    except Exception as e:
        log(f"could not compute hash for {path} — {e}", SUB, "WARN")
        return ""


def key_from_hash(file_hash: str) -> str:
    """Generate canonical video_key for local files: loc_<16-char-hash>."""
    return f"loc_{file_hash[:16]}"


# ── Watched Folders Management ────────────────────────────────────────────
def add_folder(folder_path: str) -> Dict[str, Any]:
    """Add a watched folder path and run initial scan."""
    norm_path = os.path.abspath(os.path.normpath(folder_path))
    if not os.path.isdir(norm_path):
        return {"ok": False, "error": f"Directory not found: {norm_path}"}

    with _LOCK:
        conn = _get_db()
        try:
            conn.execute(
                "INSERT INTO watched_folders (path, added_at) VALUES (?, ?)",
                (norm_path, time.time())
            )
            conn.commit()
            log(f"added watched folder: {norm_path}", SUB, "SUCCESS")
        except sqlite3.IntegrityError:
            log(f"folder already watched: {norm_path}", SUB, "INFO")
        finally:
            conn.close()

    # Trigger background scan
    threading.Thread(target=scan_folder, args=(norm_path,), daemon=True).start()
    return {"ok": True, "path": norm_path}


def remove_folder(folder_id: int) -> bool:
    """Remove a watched folder from the watchlist."""
    with _LOCK:
        conn = _get_db()
        try:
            conn.execute("DELETE FROM watched_folders WHERE folder_id = ?", (folder_id,))
            conn.commit()
            log(f"removed watched folder ID {folder_id}", SUB, "INFO")
            return True
        finally:
            conn.close()


def list_folders() -> List[Dict[str, Any]]:
    """List all watched folders and their stats."""
    with _LOCK:
        conn = _get_db()
        try:
            cur = conn.execute("SELECT * FROM watched_folders ORDER BY added_at ASC")
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


# ── Scanning & Indexing ───────────────────────────────────────────────────
def _sync_to_atlas_index(item: Dict[str, Any]) -> None:
    """Upsert video record into atlas.db's video_index table."""
    if not os.path.exists(paths.DB_PATH):
        return

    try:
        conn = sqlite3.connect(paths.DB_PATH, timeout=config.SQLITE_TIMEOUT)
        with conn:
            conn.execute("""
                INSERT INTO video_index (
                    video_key, msg_id, title, caption, creator, category,
                    duration, width, height, fps, size_mb, likes, created_at,
                    local_path, poster, moment_count, sources, has_speech, has_narrative, text_len
                ) VALUES (
                    :video_key, NULL, :title, '', 'Local Library', 'local',
                    :duration, :width, :height, :fps, :size_mb, 0, :created_at,
                    :local_path, :poster, 0, 'local', 0, 0, 0
                ) ON CONFLICT(video_key) DO UPDATE SET
                    local_path = excluded.local_path,
                    title = excluded.title,
                    duration = excluded.duration,
                    width = excluded.width,
                    height = excluded.height,
                    fps = excluded.fps,
                    size_mb = excluded.size_mb
            """, {
                "video_key": item["video_key"],
                "title": item["filename"],
                "duration": item["duration"] or 0.0,
                "width": item["width"] or 0,
                "height": item["height"] or 0,
                "fps": item["fps"] or 0.0,
                "size_mb": round(item["size_bytes"] / (1024 * 1024), 2),
                "created_at": item["mtime"],
                "local_path": item["path"],
                "poster": f"{safe_name(item['video_key'])}.t360.jpg",
            })
        conn.close()
    except Exception as e:
        log(f"failed to sync {item['video_key']} to atlas video_index: {e}", SUB, "WARN")


def scan_folder(folder_path: str) -> Dict[str, Any]:
    """Scan a single folder recursively, index in place, and derive missing artefacts."""
    folder_path = os.path.abspath(os.path.normpath(folder_path))
    if not os.path.isdir(folder_path):
        return {"scanned": 0, "indexed": 0, "errors": 1}

    log(f"scanning folder: {folder_path}", SUB)
    found_files: List[str] = []

    for root, _, files in os.walk(folder_path):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in VIDEO_EXTENSIONS:
                found_files.append(os.path.join(root, f))

    indexed_count = 0
    scanned_count = len(found_files)
    now = time.time()

    conn = _get_db()
    try:
        for fpath in found_files:
            try:
                st = os.stat(fpath)
                size = st.st_size
                mtime = st.st_mtime
                if size == 0:
                    continue

                # Compute hash
                fhash = compute_content_hash(fpath)
                if not fhash:
                    continue

                vkey = key_from_hash(fhash)
                filename = os.path.splitext(os.path.basename(fpath))[0]

                # Check if already in local_videos
                cur = conn.execute("SELECT * FROM local_videos WHERE file_hash = ?", (fhash,))
                existing = cur.fetchone()

                facts = {}
                if not existing or not existing["duration"]:
                    facts = derive.probe(fpath)

                duration = facts.get("duration", existing["duration"] if existing else 0.0)
                width = facts.get("width", existing["width"] if existing else 0)
                height = facts.get("height", existing["height"] if existing else 0)
                fps = facts.get("fps", existing["fps"] if existing else 0.0)

                record = {
                    "file_hash": fhash,
                    "video_key": vkey,
                    "path": fpath,
                    "filename": filename,
                    "size_bytes": size,
                    "mtime": mtime,
                    "duration": duration,
                    "width": width,
                    "height": height,
                    "fps": fps,
                    "status": "active",
                    "first_indexed_at": existing["first_indexed_at"] if existing else now,
                    "last_seen_at": now,
                }

                conn.execute("""
                    INSERT INTO local_videos (
                        file_hash, video_key, path, filename, size_bytes, mtime,
                        duration, width, height, fps, status, first_indexed_at, last_seen_at
                    ) VALUES (
                        :file_hash, :video_key, :path, :filename, :size_bytes, :mtime,
                        :duration, :width, :height, :fps, :status, :first_indexed_at, :last_seen_at
                    ) ON CONFLICT(file_hash) DO UPDATE SET
                        path = excluded.path,
                        filename = excluded.filename,
                        size_bytes = excluded.size_bytes,
                        mtime = excluded.mtime,
                        status = 'active',
                        last_seen_at = excluded.last_seen_at
                """, record)
                conn.commit()

                # Sync to Atlas index
                _sync_to_atlas_index(record)
                indexed_count += 1

                # Trigger derivation if incomplete
                if not derive.complete(vkey):
                    try:
                        derive.derive(vkey, fpath, facts=facts)
                    except Exception as de:
                        log(f"derive error for {filename}: {de}", SUB, "WARN")

            except Exception as e:
                log(f"error processing file {fpath}: {e}", SUB, "WARN")

        # Update folder last_scanned_at
        conn.execute(
            "UPDATE watched_folders SET last_scanned_at = ? WHERE path = ?",
            (now, folder_path)
        )
        conn.commit()
    finally:
        conn.close()

    log(f"finished scanning {folder_path}: {indexed_count}/{scanned_count} indexed", SUB, "SUCCESS")
    return {"scanned": scanned_count, "indexed": indexed_count, "errors": 0}


def scan_all() -> Dict[str, Any]:
    """Scan all registered watched folders and reconcile missing files."""
    folders = list_folders()
    total_scanned = 0
    total_indexed = 0

    for f in folders:
        if f.get("enabled", 1):
            res = scan_folder(f["path"])
            total_scanned += res.get("scanned", 0)
            total_indexed += res.get("indexed", 0)

    # Mark active files whose path no longer exists as 'missing'
    conn = _get_db()
    try:
        cur = conn.execute("SELECT file_hash, path FROM local_videos WHERE status = 'active'")
        for row in cur.fetchall():
            if not os.path.exists(row["path"]):
                conn.execute(
                    "UPDATE local_videos SET status = 'missing' WHERE file_hash = ?",
                    (row["file_hash"],)
                )
                log(f"local file marked missing: {row['path']}", SUB, "WARN")
        conn.commit()
    finally:
        conn.close()

    return {"folders": len(folders), "scanned": total_scanned, "indexed": total_indexed}


def list_videos(status: str = "active", limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    """List indexed local videos."""
    conn = _get_db()
    try:
        cur = conn.execute(
            "SELECT * FROM local_videos WHERE status = ? ORDER BY first_indexed_at DESC LIMIT ? OFFSET ?",
            (status, limit, offset)
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()
