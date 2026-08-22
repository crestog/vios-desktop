"""
server/desktop_routes.py — API routes for desktop-specific capabilities:
mirror worker, local library, engine queue, and system status.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

import derive
import engine_queue
import library
import mirror
import paths
from logger import vios_log as log
from sizing import registry

router = APIRouter(tags=["desktop"])

# A week, immutable: a derived artefact is named after a content key and is
# rewritten only by a forced re-derive, so the browser may keep it as long as
# it likes. This is what makes a scrubbed sprite sheet cost zero requests on
# the second pass over the same video.
_FOREVER = {"Cache-Control": "public, max-age=604800, immutable"}


# ── Measured machine facts ────────────────────────────────────────────────
# One cache behind two readers: the host panel (/api/desktop/host, polled every
# minute) and the component catalogue (/api/engine/components, which needs the
# same VRAM ceiling to decide what is runnable). `probe()` shells out to
# `nvidia-smi`, so both go through here and only the first call — or an explicit
# refresh — pays for the measurement.
_HOST_CACHE: Dict[str, Any] = {}


def _resources(refresh: bool = False) -> Dict[str, Any]:
    """The probed machine, cached. Returns {} if the probe itself raised —
    which callers report distinctly from a successful "no GPU here" reading."""
    if refresh or not _HOST_CACHE:
        try:
            from sizing import resources
            fresh = resources.probe(paths.HOME)
            _HOST_CACHE.clear()
            _HOST_CACHE.update(fresh)
        except Exception as e:                              # noqa: BLE001
            log(f"host probe failed — {type(e).__name__}: {e}", "desktop", "WARN")
            return {}
    return dict(_HOST_CACHE)


# ── Mirror Worker ─────────────────────────────────────────────────────────
@router.get("/api/mirror/status")
def get_mirror_status():
    return mirror.status()


@router.post("/api/mirror/start")
def start_mirror():
    mirror.start()
    return {"ok": True, "status": mirror.status()}


@router.post("/api/mirror/pause")
def pause_mirror():
    mirror.pause()
    return {"ok": True, "status": mirror.status()}


@router.post("/api/mirror/resume")
def resume_mirror():
    mirror.resume()
    return {"ok": True, "status": mirror.status()}


@router.post("/api/mirror/prioritize/{video_key}")
def prioritize_mirror(video_key: str):
    mirror.prioritize(video_key)
    return {"ok": True, "key": video_key}


# ── Local Video Library ───────────────────────────────────────────────────
class AddFolderRequest(BaseModel):
    path: str


@router.get("/api/library/folders")
def list_watched_folders():
    return library.list_folders()


@router.post("/api/library/folders")
def add_watched_folder(req: AddFolderRequest):
    res = library.add_folder(req.path)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "Failed to add folder"))
    return res


@router.delete("/api/library/folders/{folder_id}")
def remove_watched_folder(folder_id: int):
    ok = library.remove_folder(folder_id)
    return {"ok": ok}


@router.post("/api/library/scan")
def scan_library():
    return library.scan_all()


@router.get("/api/library/local")
def list_local_videos(status: str = "active", limit: int = 100, offset: int = 0):
    return library.list_videos(status=status, limit=limit, offset=offset)


# ── Local Engine Queue ────────────────────────────────────────────────────
class EnqueueRequest(BaseModel):
    video_key: str
    component_ids: Optional[List[str]] = None


@router.get("/api/engine/stats")
def get_engine_stats():
    return engine_queue.get_queue_stats()


@router.get("/api/engine/jobs")
def list_engine_jobs(state: Optional[str] = None, limit: int = 100):
    return engine_queue.list_jobs(state=state, limit=limit)


@router.post("/api/engine/enqueue")
def enqueue_engine_job(req: EnqueueRequest):
    count = engine_queue.enqueue_video(req.video_key, req.component_ids)
    return {"ok": True, "enqueued": count}


# The worker runs in a daemon thread started at boot, so these three only ever
# flip a flag or spin the thread back up — they return the fresh stats so the
# button that was clicked reflects its own effect without waiting for the poll.
@router.post("/api/engine/start")
def start_engine():
    engine_queue.start()
    engine_queue.resume()  # start() returns early if already running; unpause anyway
    return {"ok": True, "stats": engine_queue.get_queue_stats()}


@router.post("/api/engine/pause")
def pause_engine():
    engine_queue.pause()
    return {"ok": True, "stats": engine_queue.get_queue_stats()}


@router.post("/api/engine/resume")
def resume_engine():
    engine_queue.resume()
    return {"ok": True, "stats": engine_queue.get_queue_stats()}


@router.get("/api/engine/components")
def list_components(refresh: bool = False):
    """The static pipeline catalogue, annotated with what *this* machine can run.

    The engine room's other half: the queue says what is *scheduled*, this says
    what is *possible*. Two things depend on it. Every job carries a bare
    `component_id` — `transcribe`, `shots` — and this is where that resolves to
    a title and a stage. And the `unrunnable` count in the queue stats gets its
    reason: a pass whose `vram_mb` exceeds this laptop's usable VRAM comes back
    `unrunnable: true` with the shortfall spelled out, rather than the number
    sitting there unexplained.

    Runnability is the only field that moves, and only when free VRAM does (a
    model loading, a pass finishing), so the view fetches this once per mount
    instead of polling it. `?refresh=1` re-measures the machine first.
    """
    res = _resources(refresh)
    ids = registry.all_ids()
    blocked = registry.unrunnable(ids, res) if res else {}
    out = []
    for c in registry.CATALOGUE:
        reason = blocked.get(c.id)
        out.append({
            "id": c.id,
            "title": c.title,
            "stage": c.stage,
            "stage_name": c.stage_name,
            "family": c.family,
            "channel": c.channel,
            "model": c.model,
            "device": c.device,
            "cards": c.cards,
            "vram_mb": c.vram_mb,
            "disk_mb": c.disk_mb,
            "seconds": c.seconds,
            "tier": c.tier,
            "default_on": c.default_on,
            "summary": c.summary,
            "produces": list(c.produces),
            "kinds": list(c.kinds),
            "unrunnable": reason is not None,
            "reason": reason,
        })
    return {
        "ok": True,
        "measured": bool(res),
        "total": len(out),
        "runnable": sum(1 for r in out if not r["unrunnable"]),
        "blocked": len(blocked),
        "defaults": sum(1 for c in registry.CATALOGUE if c.default_on),
        "components": out,
    }


# ── Derived artefacts: poster tiers, sprite sheet, keyframes ──────────────
# Atlas's own `/api/poster/{key}` extracts a frame on demand and answers one
# size. The grid needs three sizes — the density slider is only honest if a
# 12-column grid actually fetches small files — and the scrubber needs the
# sprite sheet. Both are produced once by `derive.py`, so these routes are
# pure file serving with no ffmpeg in the request path.

_TIERS = (160, 360, 720)


@router.get("/api/derived/poster/{video_key}")
def get_poster_tier(video_key: str, tier: int = 360):
    """A poster at one of three tiers, falling back down and then out.

    Falling back *down* rather than up: if 720 is missing but 360 exists, a
    slightly soft card beats an empty one, and the alternative — upscaling or
    a 204 — either lies about sharpness or leaves a hole in the grid. Falling
    back *out* to Atlas's on-demand extractor covers the window before the
    mirror worker has reached this video at all, which on a cold archive is
    most of them.
    """
    want = tier if tier in _TIERS else 360
    order = [want] + [t for t in reversed(_TIERS) if t != want]
    for t in order:
        path = derive.poster_path(video_key, t)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return FileResponse(path, media_type="image/jpeg", headers=_FOREVER)
    return Response(status_code=204)


@router.get("/api/derived/sprite/{video_key}")
def get_sprite_sheet(video_key: str):
    path = derive.sprite_path(video_key)
    if not (os.path.exists(path) and os.path.getsize(path) > 0):
        return Response(status_code=204)
    return FileResponse(path, media_type="image/jpeg", headers=_FOREVER)


@router.get("/api/derived/sprite/{video_key}/meta")
def get_sprite_meta(video_key: str):
    """Grid geometry for the scrub strip: cols, rows, tile size, interval.

    Served as its own tiny document rather than folded into `/api/video` so
    the player can ask for it the moment the pointer enters the timeline,
    without waiting on the much larger every-fact-we-know payload.
    """
    path = derive.sprite_meta_path(video_key)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return JSONResponse(json.load(fh), headers=_FOREVER)
    except (OSError, ValueError):
        return JSONResponse({"ok": False, "note": "no sprite sheet yet"},
                            status_code=404)


@router.get("/api/derived/keyframes/{video_key}")
def get_keyframe_index(video_key: str):
    """The scene-change frames and their timestamps.

    `index.json` is written last by the derive pass, so its presence is the
    completion signal for keyframes — a half-extracted directory has no index
    and therefore reads as absent rather than as a short list.
    """
    path = derive.keyframe_index_path(video_key)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return JSONResponse(json.load(fh), headers=_FOREVER)
    except (OSError, ValueError):
        return JSONResponse({"ok": False, "note": "no keyframes yet"},
                            status_code=404)


@router.get("/api/derived/keyframes/{video_key}/{name}")
def get_keyframe_image(video_key: str, name: str):
    """One extracted frame, full source resolution.

    `os.path.basename` on the name, then a containment check on the resolved
    path: the first stops `../` in the URL from ever reaching the filesystem,
    the second catches anything the first missed. A read-only route on
    localhost still gets both, because the cost is two lines.
    """
    root = os.path.abspath(derive.keyframe_dir(video_key))
    path = os.path.abspath(os.path.join(root, os.path.basename(name)))
    if not path.startswith(root + os.sep) or not os.path.exists(path):
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers=_FOREVER)


@router.get("/api/derived/state/{video_key}")
def get_derived_state(video_key: str):
    """Which artefacts exist on disk for this video, read from disk.

    Not from a database column recording what a past run believed it wrote:
    the two disagree exactly when it matters — after a crash, a manual delete,
    or a home directory moved with `VIOS_LOCAL_HOME`.
    """
    got = derive.have(video_key)
    return {"ok": True, "key": video_key, "have": got,
            "complete": derive.complete(video_key)}


# ── Disk & Storage Status ─────────────────────────────────────────────────
@router.get("/api/desktop/disk")
def get_disk_usage():
    got = paths.usage()
    got["free_floor_gb"] = paths.FREE_FLOOR_GB
    got["below_floor"] = paths.below_floor()
    return got


# ── Hardware ──────────────────────────────────────────────────────────────
@router.get("/api/desktop/host")
def get_host_facts(refresh: bool = False):
    """What this machine actually is, measured rather than assumed.

    The status strip states the GPU name and the usable VRAM ceiling, and both
    have to be read from the card: a hardcoded "RTX 3050 · 4900 MB" would keep
    saying that after the app moved to another machine, and the whole point of
    the number is to be trusted when the model browser refuses a download.

    Reads the shared `_resources()` cache — the same probe the component
    catalogue bin-packs against — so `?refresh=1` here also freshens the VRAM
    numbers the Engine tab's runnability flags depend on.
    """
    res = _resources(refresh)
    if not res:
        return {"ok": False, "note": "host probe failed",
                "gpus": [], "gpu_count": 0, "usable_vram_mb": 0}
    return {"ok": True, **res}
