"""
mirror — the background mirror worker.

This module is what turns "reads from Telegram" into "everything is local",
removing the network and Telegram's rate limits from the hot path entirely.

How it works:
  1. Walks known videos from `video_index` (and `library.db`).
  2. Identifies videos needing download (Telegram originals not yet in `paths.VIDEO_DIR`).
  3. Identifies videos needing derivation (proxy, sprite sheet, posters, keyframes).
  4. Manages a prioritized work queue:
     - User-requested or currently-viewed videos jump to the front of the queue.
     - Newest / highest moment-count videos processed first in background.
  5. Enforces polite concurrency:
     - At most 2 concurrent Telegram downloads (MTProto/Bot API rate limit politeness).
     - At most 2 concurrent derivation passes at below-normal CPU priority.
  6. Enforces the disk floor:
     - Measures `paths.below_floor()` before every download and derivation.
     - If free space falls below `paths.FREE_FLOOR_GB`, pauses the worker and logs.
     - Automatically resumes when free space recovers.
  7. Never buffers whole videos in RAM:
     - Downloads stream directly to `.part` files on disk and atomic rename.
"""

from __future__ import annotations

import collections
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Set

import derive
import paths
from atlas import config, tgchannel
from atlas.media import safe_name
from logger import vios_log as log

SUB = "MIRROR"

# Concurrency limits
_MAX_DOWNLOAD_SLOTS = int(os.environ.get("VIOS_MIRROR_DOWNLOAD_JOBS", "2"))
_DOWNLOAD_SEMAPHORE = threading.Semaphore(_MAX_DOWNLOAD_SLOTS)

# State & Stats
_LOCK = threading.RLock()
_RUNNING = False
_PAUSED = False
_STOP_EVENT = threading.Event()
_WAKE_EVENT = threading.Event()
_WORKER_THREAD: Optional[threading.Thread] = None

# In-flight tracking
_ACTIVE_DOWNLOADS: Dict[str, Dict[str, Any]] = {}
_ACTIVE_DERIVES: Dict[str, Dict[str, Any]] = {}
_PRIORITY_QUEUE: collections.deque = collections.deque()
_RECENT_ERRORS: collections.deque = collections.deque(maxlen=50)

_STATS = {
    "total_videos": 0,
    "downloaded": 0,
    "derived": 0,
    "bytes_downloaded": 0,
    "last_cycle_at": 0.0,
    "last_error": "",
}


def _video_src_path(video_key: str, local_path: Optional[str] = None) -> str:
    """Return local original video path if present on disk, else ''."""
    key = str(video_key)
    # Check if registered local file from library
    if local_path and os.path.exists(local_path) and os.path.getsize(local_path) > 0:
        return local_path
    # Check if mirrored original in paths.VIDEO_DIR
    target = os.path.join(paths.VIDEO_DIR, f"{safe_name(key)}.mp4")
    if os.path.exists(target) and os.path.getsize(target) > 0:
        return target
    # Check fallback legacy cache path if any
    legacy = os.path.join(paths.VIDEO_DIR, f"{key}.mp4")
    if os.path.exists(legacy) and os.path.getsize(legacy) > 0:
        return legacy
    return ""


def prioritize(video_key: str) -> None:
    """Push a video to the front of the queue (e.g. user clicked to watch)."""
    key = str(video_key)
    with _LOCK:
        if key not in _PRIORITY_QUEUE:
            _PRIORITY_QUEUE.appendleft(key)
    _WAKE_EVENT.set()


def _get_target_list() -> List[Dict[str, Any]]:
    """Query atlas.db and library.db for all known videos and their status."""
    items = []
    if not os.path.exists(paths.DB_PATH):
        return items

    try:
        conn = sqlite3.connect(paths.DB_PATH, timeout=config.SQLITE_TIMEOUT)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT video_key, msg_id, local_path, duration, moment_count, created_at "
            "FROM video_index "
            "ORDER BY moment_count DESC, created_at DESC"
        )
        for r in cur.fetchall():
            items.append({
                "key": str(r["video_key"]),
                "msg_id": int(r["msg_id"]) if r["msg_id"] else None,
                "local_path": r["local_path"],
                "duration": float(r["duration"] or 0.0),
                "moment_count": int(r["moment_count"] or 0),
            })
        conn.close()
    except Exception as e:
        log(f"could not query video_index: {e}", SUB, "WARN")

    return items


def _download_video(item: Dict[str, Any]) -> bool:
    """Download one video over Telegram (MTProto / Bot API) directly to disk."""
    key = item["key"]
    msg_id = item.get("msg_id")
    if not msg_id:
        try:
            msg_id = int(key)
        except (ValueError, TypeError):
            return False

    # No credentials means no channel, and the worker comes back every 30 s for
    # every video that is missing. Without this the log fills with the same
    # failure per reel per cycle, and the useful half of this worker — deriving
    # proxies for the files that *are* on disk — is buried under it. The mirror
    # still runs on a machine with no Telegram: it just cannot fetch what is
    # only in the channel.
    if not config.telegram_ready():
        return False

    dest = os.path.join(paths.VIDEO_DIR, f"{safe_name(key)}.mp4")
    tmp = dest + ".part"

    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return True

    # Check disk floor before downloading
    if paths.below_floor():
        log(f"disk space below {paths.FREE_FLOOR_GB:.1f} GB floor — skipping download for {key}", SUB, "WARN")
        return False

    with _DOWNLOAD_SEMAPHORE:
        with _LOCK:
            _ACTIVE_DOWNLOADS[key] = {
                "key": key,
                "msg_id": msg_id,
                "got": 0,
                "total": 0,
                "started_at": time.time(),
                "percent": 0.0,
                "speed_kbps": 0.0,
            }

        last_tick = [time.time(), 0]

        def _progress(current: int, total: int):
            now = time.time()
            dt = now - last_tick[0]
            if dt >= 0.5:
                bytes_delta = current - last_tick[1]
                speed = (bytes_delta / dt) / 1024.0
                last_tick[0] = now
                last_tick[1] = current
                pct = round(100.0 * current / total, 1) if total else 0.0
                with _LOCK:
                    if key in _ACTIVE_DOWNLOADS:
                        _ACTIVE_DOWNLOADS[key].update(
                            got=current, total=total, percent=pct, speed_kbps=round(speed, 1)
                        )

        ok = False
        try:
            # Try MTProto download
            ok = tgchannel.download_by_id(msg_id, tmp, progress=_progress)
            if ok and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                os.replace(tmp, dest)
                with _LOCK:
                    _STATS["bytes_downloaded"] += os.path.getsize(dest)
                log(f"mirrored original for {key} ({os.path.getsize(dest) / (1024*1024):.1f} MB)", SUB, "SUCCESS")
                return True
        except Exception as e:
            with _LOCK:
                _RECENT_ERRORS.append({"key": key, "error": str(e), "at": time.time()})
                _STATS["last_error"] = str(e)
            log(f"failed to download {key} (msg {msg_id}): {e}", SUB, "WARN")
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            with _LOCK:
                _ACTIVE_DOWNLOADS.pop(key, None)

    return False


def _process_item(item: Dict[str, Any]) -> None:
    """Ensure the video is downloaded and derived."""
    key = item["key"]
    local_src = _video_src_path(key, item.get("local_path"))

    # 1. Download if missing and from Telegram
    if not local_src and item.get("msg_id"):
        if _STOP_EVENT.is_set():
            return
        downloaded = _download_video(item)
        if downloaded:
            local_src = _video_src_path(key, item.get("local_path"))

    if not local_src:
        return

    # 2. Derive if incomplete
    if not derive.complete(key):
        if _STOP_EVENT.is_set():
            return
        if paths.below_floor():
            log(f"disk space below floor — skipping derivation for {key}", SUB, "WARN")
            return

        with _LOCK:
            _ACTIVE_DERIVES[key] = {"key": key, "started_at": time.time()}

        try:
            derive.derive(key, local_src, force=False)
        except Exception as e:
            with _LOCK:
                _RECENT_ERRORS.append({"key": key, "error": str(e), "at": time.time()})
                _STATS["last_error"] = str(e)
            log(f"derivation failed for {key}: {e}", SUB, "WARN")
        finally:
            with _LOCK:
                _ACTIVE_DERIVES.pop(key, None)


def _worker_loop() -> None:
    """Main background loop."""
    global _RUNNING
    log("mirror worker started", SUB)

    while not _STOP_EVENT.is_set():
        if _PAUSED or paths.below_floor():
            _WAKE_EVENT.wait(timeout=10.0)
            _WAKE_EVENT.clear()
            continue

        # 1. Handle priority queue first
        prioritized_key = None
        with _LOCK:
            if _PRIORITY_QUEUE:
                prioritized_key = _PRIORITY_QUEUE.popleft()

        if prioritized_key:
            items = _get_target_list()
            matched = next((it for it in items if it["key"] == prioritized_key), None)
            if matched:
                _process_item(matched)
            continue

        # 2. Scan all known targets
        targets = _get_target_list()
        total = len(targets)
        downloaded_count = 0
        derived_count = 0

        for it in targets:
            if _STOP_EVENT.is_set() or _PAUSED or paths.below_floor():
                break

            k = it["key"]
            src = _video_src_path(k, it.get("local_path"))
            if src:
                downloaded_count += 1
            if derive.complete(k):
                derived_count += 1
            else:
                # Needs download or derive
                _process_item(it)

        with _LOCK:
            _STATS["total_videos"] = total
            _STATS["downloaded"] = downloaded_count
            _STATS["derived"] = derived_count
            _STATS["last_cycle_at"] = time.time()

        # Wait before next pass if all caught up
        _WAKE_EVENT.wait(timeout=30.0)
        _WAKE_EVENT.clear()

    _RUNNING = False
    log("mirror worker stopped", SUB)


def start() -> None:
    """Start the mirror worker in a daemon thread."""
    global _RUNNING, _WORKER_THREAD, _PAUSED
    with _LOCK:
        if _RUNNING:
            return
        _STOP_EVENT.clear()
        _PAUSED = False  # a worker restarted after a paused stop() must wake runnable
        _RUNNING = True
        _WORKER_THREAD = threading.Thread(target=_worker_loop, name="vios-mirror", daemon=True)
        _WORKER_THREAD.start()


def stop() -> None:
    """Stop the mirror worker."""
    global _RUNNING
    _STOP_EVENT.set()
    _WAKE_EVENT.set()
    with _LOCK:
        _RUNNING = False


def pause() -> None:
    """Pause mirroring."""
    global _PAUSED
    _PAUSED = True
    _WAKE_EVENT.set()
    log("mirror paused", SUB)


def resume() -> None:
    """Resume mirroring."""
    global _PAUSED
    _PAUSED = False
    _WAKE_EVENT.set()
    log("mirror resumed", SUB)


def status() -> Dict[str, Any]:
    """Status dictionary for API and status strip."""
    with _LOCK:
        active_dl = list(_ACTIVE_DOWNLOADS.values())
        active_drv = list(_ACTIVE_DERIVES.values())
        recent_errs = list(_RECENT_ERRORS)
        stats_copy = dict(_STATS)

    disk = paths.usage()
    below = paths.below_floor()

    return {
        "running": _RUNNING,
        "paused": _PAUSED,
        "below_floor": below,
        "total_videos": stats_copy["total_videos"],
        "downloaded": stats_copy["downloaded"],
        "derived": stats_copy["derived"],
        "bytes_downloaded": stats_copy["bytes_downloaded"],
        "active_downloads": active_dl,
        "active_derives": active_drv,
        "priority_queued": len(_PRIORITY_QUEUE),
        "recent_errors": recent_errs,
        "last_error": stats_copy["last_error"],
        "disk": disk,
    }
