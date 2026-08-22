"""
server/admin_routes.py — credentials, the wire contract, and the restore path.

Three groups of routes, and the reason there are only three is that most of what
an admin screen shows already has an owner. `/api/status` reports the boot and
the ingest, `/api/bundles` lists every bundle and shard ever imported,
`/api/desktop/disk` and `/api/desktop/host` report the machine, `/api/channel`
reports the channel, and `/api/scan` and `/api/reindex` are the two buttons that
actually move data. None of them are re-exported here: a second route answering a
question the first already answers is a second answer, and the two drift.

What was genuinely missing:

  **Credentials that survive a launch.** `/api/capture/config` already accepts a
  typed token and bridges it into `os.environ` through `creds.adopt`, which is
  enough for the process that is running and for nothing after it. The store half
  — `creds.save_local` / `creds.forget_local` — had no route at all, so the one
  thing the form exists for, *not typing it again next launch*, was unreachable
  from the interface. WIRE.md records the upstream half of this as defect 1; this
  is the other end of the same gap.

  **The wire contract, stated out loud.** `sizing.SCHEMA_VERSION` is 3 here, every
  shard header carries the number it was written with, and `bundles.schema` keeps
  it. When the Kaggle side bumps to 4, this laptop starts reading files it does
  not understand — a case `atlas/ingest.py` says is "reported loudly rather than
  skipped quietly". Loudly still needs somebody looking, and this is where they
  look.

  **Restore, with its effects visible.** `db_restore` computes a plan before it
  writes a byte, and the plan carries `effects`: every store an apply replaces,
  every store it leaves for the user to re-derive. That list is the entire reason
  an inspect mode exists, and it had no way to reach a screen.

There is deliberately **no export route.** `db_export` builds a bundle from
`config.DB_PATH` and pins its manifest to the channel — and the channel is the
one contract this application shares with the Kaggle plane. Here `config.DB_PATH`
is `<HOME>/lake/lake.db`, which has no reader in this repository: `atlas/*` opens
`atlas.config.DB_PATH`, which is `paths.DB_PATH`, a different file. An export
from this laptop would publish an empty index over the pinned manifest the
*other* program restores from. That is not a feature needing a warning label, it
is a way to break the other machine, so the route does not exist.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import creds
from logger import vios_log as log

SUB = "ADMIN"

router = APIRouter(tags=["admin"])

# The grep in the module docstring, as a sentence a panel can render.
# `db_restore` knows precisely what an apply replaces — it computes
# `plan.effects` and names five stores — but it cannot know that on *this*
# machine the store it replaces is not the store the reader reads. That fact
# belongs to this repository's layout, so it is stated here rather than patched
# into a file WIRE.md records as copied unchanged.
_RESTORE_SCOPE = (
    "This restores the harvest index (lake.db) from the newest bundle in the "
    "channel — the Kaggle plane's database, not this one. The reader's atlas.db "
    "is built by the channel scan, and a restore does not touch it. Inspect is "
    "the half that earns its place here: it is read-only, and it reports what "
    "the newest bundle in the channel actually contains."
)

# ══════════════════════════════════════════════════════════════════════════
# CREDENTIALS
# ══════════════════════════════════════════════════════════════════════════
class CredentialForm(BaseModel):
    """Every field optional, because blank means *leave it alone*.

    The form starts empty on a machine that already has all six stored — the
    rule `capture/engine.py:262` states as "never returns a secret; the UI shows
    presence, not value" — so a submission with one field filled must change one
    credential and not delete five. `creds.save_local` merges into the existing
    file and `creds.adopt` never blanks a variable, so both halves already
    behave that way; this model's job is not to undo it by defaulting a missing
    field to `""`.

    `channel_id` and `api_id` accept an int as well as a string: they are digits,
    a JSON form will send them as numbers about as often as text, and pydantic v2
    does not coerce one into the other.
    """

    bot_token: Optional[str] = None
    channel_id: Optional[Union[int, str]] = None
    api_id: Optional[Union[int, str]] = None
    api_hash: Optional[str] = None
    hf_token: Optional[str] = None
    ig_cookies: Optional[str] = None


@router.get("/api/admin/credentials")
def get_credentials() -> Dict[str, Any]:
    """Presence and origin for all six. Never a value.

    `creds.describe()` is the function whose entire contract is that, and it also
    answers the question one layer behind an empty field: `kaggle_reason` and
    `kaggle_advice` distinguish "no secret stored" from "could not ask the store",
    which is the failure a setup screen exists to explain.
    """
    try:
        return {"ok": True, **creds.describe()}
    except Exception as e:                                     # noqa: BLE001
        log(f"credential store unreadable — {type(e).__name__}: {e}", SUB, "WARN")
        raise HTTPException(
            status_code=500,
            detail=f"could not read the credential store — {type(e).__name__}: {e}",
        ) from e


@router.post("/api/admin/credentials")
def save_credentials(form: CredentialForm) -> Dict[str, Any]:
    """Store what was typed, and make it live in the same call.

    Two writes, in this order, and both are required.

    `save_local` puts the values in `~/.vios/credentials.json` so the next launch
    has them, which is the whole point of the form. `adopt` puts them in
    `os.environ` so *this* process has them, because `tg_transport`,
    `db_restore` and capture's uploader all read the environment — and
    `config.__getattr__` reads the environment **before** the file. Saving alone
    would leave a stale exported token beating the one just typed: a form that
    reports success and changes nothing until a restart.

    Only field names are logged. The values are the thing this whole module is
    built not to emit.
    """
    typed = {k: v for k, v in form.model_dump().items()
             if v is not None and str(v).strip()}
    if not typed:
        raise HTTPException(status_code=400,
                            detail="Nothing to save — every field was blank.")
    try:
        saved = creds.save_local(typed)
    except RuntimeError as e:
        # `save_local`'s own two refusals, and both are sentences worth showing
        # verbatim: it will not write on Kaggle (the filesystem there is either
        # wiped or published), and it will not write an empty file.
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"could not write {creds.local_path()} — {e}") from e

    exported = creds.adopt(typed)
    log(f"credentials stored — {', '.join(sorted(typed))}", SUB)
    return {
        "ok": True,
        "path": saved["path"],
        "stored": saved["fields"],
        "changed": sorted(typed),
        "exported": sorted(set(exported.values())),
        "credentials": creds.describe(),
    }


@router.post("/api/admin/credentials/forget")
def forget_credentials() -> Dict[str, Any]:
    """Delete the stored file. Does **not** unset the environment.

    Deliberate, and the panel says so rather than leaving it to be discovered:
    this process may be an hour into a week-long capture with an open Telegram
    session, and pulling its token out from under it would fail an upload
    mid-reel to make a checkbox look consistent. Forget is about what the *next*
    launch knows.
    """
    try:
        res = creds.forget_local()
    except OSError as e:
        raise HTTPException(
            status_code=500,
            detail=f"could not remove {creds.local_path()} — {e}") from e
    log("stored credentials removed" if res["removed"]
        else "no stored credentials to remove", SUB)
    return {
        "ok": True,
        **res,
        "effect": ("Removed from disk. This process keeps the credentials it "
                   "already loaded — the change takes effect on the next launch."),
        "credentials": creds.describe(),
    }


# ══════════════════════════════════════════════════════════════════════════
# THE WIRE CONTRACT
# ══════════════════════════════════════════════════════════════════════════
_WIRE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "WIRE.md")

_ROW = re.compile(r"^\|\s*(?P<k>[^|]+?)\s*\|\s*(?P<v>[^|]*?)\s*\|\s*$")


def _clean(s: str) -> str:
    """Strip the markdown a table cell wears — backticks and bold."""
    return s.replace("`", "").replace("**", "").strip()


def _provenance() -> Dict[str, Any]:
    """The Provenance table out of WIRE.md, parsed rather than duplicated.

    WIRE.md is the file whose own instruction is "update this file whenever shard
    or manifest handling changes on either side, including the upstream SHA it was
    verified against". Restating that SHA as a constant in this module would give
    it a second home and the second home would be the stale one, so it is read
    from the source of truth on each call — the file is 4 KB and next to the
    module, and this route is not polled.

    Tolerant on purpose: the parse is a convenience over prose, and a reworded
    heading must degrade to `null` in a banner, never a 500 on the admin screen.
    """
    out: Dict[str, Any] = {"upstream": None, "commit": None, "lifted_on": None,
                           "schema_at_commit": None, "path": _WIRE_PATH,
                           "parsed": False}
    try:
        with open(_WIRE_PATH, "r", encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        out["note"] = f"WIRE.md unreadable — {e}"
        return out

    cells: Dict[str, str] = {}
    for line in lines:
        m = _ROW.match(line)
        if not m:
            continue
        key = _clean(m.group("k")).lower()
        if key and set(key) != {"-"}:
            cells.setdefault(key, _clean(m.group("v")))

    for key, val in cells.items():
        if "upstream repository" in key:
            out["upstream"] = val
        elif "commit lifted" in key:
            out["commit"] = val.split()[0] if val else None
        elif key.startswith("lifted on"):
            out["lifted_on"] = val
        elif "schema_version" in key:
            digits = re.search(r"\d+", val)
            out["schema_at_commit"] = int(digits.group()) if digits else None
    out["parsed"] = any(out[k] is not None
                        for k in ("upstream", "commit", "lifted_on"))
    return out


def _imported() -> Dict[str, Any]:
    """One pass over the `bundles` table, tallied for a banner.

    Bundles and shards share that table and are told apart by the `seq` prefix —
    `import_shard` writes `"shard:" + shard_seq(info)` (`atlas/ingest.py:851`)
    into the same eleven columns a manifest import uses. Both carry the
    `SCHEMA_VERSION` they were written with, which is the number this whole
    section is about.
    """
    out: Dict[str, Any] = {"readable": False, "bundles": 0, "shards": 0,
                           "failed": 0, "bytes": 0, "newest_at": None,
                           "by_schema": {}, "max_schema": None}
    try:
        from atlas import ingest, server as atlas_server
        rows: List[dict] = ingest.bundle_rows(atlas_server.db())
    except Exception as e:                                     # noqa: BLE001
        out["note"] = f"atlas.db unreadable — {type(e).__name__}: {e}"
        return out

    out["readable"] = True
    for r in rows:
        if str(r.get("status") or "") != "ok":
            out["failed"] += 1
            continue
        if str(r.get("seq") or "").startswith("shard:"):
            out["shards"] += 1
        else:
            out["bundles"] += 1
        out["bytes"] += int(r.get("bytes") or 0)
        at = r.get("imported_at")
        if at and (out["newest_at"] is None or at > out["newest_at"]):
            out["newest_at"] = at
        # A schema of None is a v1 bundle, which predates the header carrying
        # one. Counted under "unknown" rather than dropped: it is a real file
        # that really imported, and a tally that omits it under-reports the
        # archive to make the banner tidier.
        sch = r.get("schema")
        label = str(int(sch)) if isinstance(sch, (int, float)) else "unknown"
        out["by_schema"][label] = out["by_schema"].get(label, 0) + 1
        if isinstance(sch, (int, float)):
            n = int(sch)
            if out["max_schema"] is None or n > out["max_schema"]:
                out["max_schema"] = n
    return out


@router.get("/api/admin/wire")
def get_wire() -> Dict[str, Any]:
    """Is this build still able to read what the other program is writing?

    Four numbers and one verdict. `ours` is `sizing.SCHEMA_VERSION`, the number
    `shardwriter.write_shard` stamps into every header it produces and the number
    `atlas.ingest` reads back. `highest_seen` is the largest schema any file that
    actually imported was written with. `schema_at_commit` is what WIRE.md
    recorded when this tree was lifted.

    The verdict that matters is `ahead`: the channel holds files written by a
    newer processing plane than this reader understands. `atlas/ingest.py` is
    explicit that such a file is "reported loudly rather than skipped quietly",
    and this is the loud part — a shard imported under a schema this code has
    never seen may be missing columns it needs, and the honest response is to
    pull the upstream commit, not to keep scanning.

    `wire_stale` is the smaller, sneakier failure: WIRE.md claiming a schema
    number that no longer matches this tree's constant means the contract document
    was not updated with the code, and the document is the only thing keeping the
    two repositories in step.
    """
    from sizing import SCHEMA_VERSION

    prov = _provenance()
    got = _imported()
    ours = int(SCHEMA_VERSION)
    seen = got["max_schema"]

    if not got["readable"]:
        verdict, headline = "unknown", "Cannot read atlas.db to check."
    elif seen is None:
        verdict = "empty"
        headline = ("Nothing imported yet — run a channel scan and this reports "
                    "what arrived.")
    elif seen > ours:
        verdict = "ahead"
        headline = (f"The channel carries schema {seen}; this build reads {ours}. "
                    f"Pull the upstream commit before trusting a search — a newer "
                    f"shard can hold columns this reader does not know about.")
    elif seen == ours:
        verdict = "current"
        headline = f"Schema {ours} on both sides."
    else:
        verdict = "behind"
        headline = (f"Everything imported was written at schema {seen}; this build "
                    f"reads {ours}. Older files read correctly.")

    return {
        "ok": True,
        "schema": {"ours": ours, "highest_seen": seen,
                   "at_commit": prov["schema_at_commit"],
                   "by_schema": got["by_schema"]},
        "verdict": verdict,
        "headline": headline,
        "wire_stale": bool(prov["schema_at_commit"] is not None
                           and prov["schema_at_commit"] != ours),
        "provenance": prov,
        "imported": {k: got[k] for k in
                     ("readable", "bundles", "shards", "failed", "bytes",
                      "newest_at")},
        "note": got.get("note") or prov.get("note") or "",
    }


# ══════════════════════════════════════════════════════════════════════════
# RESTORE
# ══════════════════════════════════════════════════════════════════════════
class RestoreApply(BaseModel):
    """`confirm` is not decoration.

    An apply replaces a database. `db_restore.start_restore` will happily run one
    with no argument at all, which is right for a CLI and wrong for an HTTP route
    a stray click can reach — so the destructive mode needs a body that says so,
    and the panel only sends it once the plan has been shown.

    `seq` pins the exact bundle the user was looking at. Left null, `db_restore`
    reuses whatever the last inspect found; that is its documented behaviour and
    it is correct, but only if the two are seconds apart. A pinned seq is how a
    panel that has been open for ten minutes stops being a guess.
    """

    confirm: bool = False
    seq: Optional[str] = None


def _restore():
    """`db_restore`, imported late.

    It pulls in `db_export` for four helpers, and both open `config`, which reads
    the credential store. Nothing here is expensive, but the whole module tree is
    dead weight on a launch that never opens this tab — and an import error must
    cost this one panel rather than the 99 routes beside it.
    """
    try:
        import db_restore
        return db_restore
    except Exception as e:                                     # noqa: BLE001
        log(f"db_restore unavailable — {type(e).__name__}: {e}", SUB, "WARN")
        raise HTTPException(
            status_code=503,
            detail=f"restore is unavailable — {type(e).__name__}: {e}") from e


@router.get("/api/admin/restore")
def get_restore_status() -> Dict[str, Any]:
    """The job, plus the two things the job cannot know about itself.

    `stalled_s` comes from `restore_status()` and is the age of the last
    transferred byte — the number that separates a slow download from a dead one,
    which the module's own docstring says the old panel could not do.

    `missing` is the credentials a restore would need, recomputed per call by
    `config.missing_telegram_secrets()` rather than cached, so a panel that showed
    "Telegram is not configured" tells the truth again one save later without a
    restart. `scope` is this repository's layout fact, which the lifted module has
    no way to state.
    """
    mod = _restore()
    st = dict(mod.restore_status())
    try:
        import config
        st["missing"] = config.missing_telegram_secrets()
    except Exception:                                          # noqa: BLE001
        st["missing"] = []
    st["ok"] = True
    st["scope"] = _RESTORE_SCOPE
    st["target"] = getattr(mod, "DB_PATH", "")
    return st


@router.post("/api/admin/restore/inspect")
def inspect_restore() -> Dict[str, Any]:
    """Read the newest manifest in the channel and compute the plan. Writes nothing.

    Returns as soon as the thread is running; the panel polls
    `/api/admin/restore` and renders `plan.effects` when the state reaches
    `ready`. The point of the mode is that the destructive case — a local database
    already holding more rows than the bundle — is `plan.destructive: true` on a
    screen, before anything has been overwritten rather than after.
    """
    res = _restore().start_restore("inspect")
    if not res.get("ok"):
        # `start_restore` refuses in three named situations, and each one is a
        # sentence the user can act on: a restore already running, an export
        # running against the same session file, or an unknown mode.
        raise HTTPException(status_code=409,
                            detail=res.get("error", "restore refused"))
    log("restore inspect started", SUB)
    return {"ok": True, "mode": "inspect", "scope": _RESTORE_SCOPE}


@router.post("/api/admin/restore/apply")
def apply_restore(req: RestoreApply) -> Dict[str, Any]:
    """Download the bundle and replace the harvest index. Destructive.

    Refused without `confirm`, and refused before an inspect has produced a plan:
    an apply with nothing to compare against is a user authorising a diff they
    were never shown. `db_restore` snapshots the current index to scratch first,
    so the mistake is recoverable within the session — which is a reason to say
    yes to the button existing, not a reason to skip the confirmation.
    """
    mod = _restore()
    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail=("Run an inspect first, read the plan, then send "
                    "confirm: true. An apply replaces the harvest index."))
    if req.seq is None and not (mod.restore_status().get("plan") or {}):
        raise HTTPException(
            status_code=409,
            detail=("No inspected plan to apply. Run an inspect, or name the "
                    "bundle with `seq`."))
    res = mod.start_restore("apply", req.seq)
    if not res.get("ok"):
        raise HTTPException(status_code=409,
                            detail=res.get("error", "restore refused"))
    log(f"restore APPLY started — seq={req.seq or 'newest inspected'}", SUB)
    return {"ok": True, "mode": "apply", "seq": res.get("seq"),
            "scope": _RESTORE_SCOPE}

