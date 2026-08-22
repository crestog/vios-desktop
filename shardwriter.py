"""
shardwriter — write evidence shards in the self-describing wire format.

This module is the writer half of the only contract between the laptop and Kaggle.
The reader is `atlas/ingest.py:import_shard` and `vios/process/intake.py:restore_shards`.

Wire format specification (from WIRE.md):
  - File name: `vios-evidence-<seq>-<session>.jsonl.gz`
  - Compression: gzip
  - Line 1: JSON header object
    {"_": "vios-evidence-shard", "schema": 3, "session": "<id>", "at": <epoch_float>,
     "tables": {"claim": {"columns": {"video_key": "TEXT", ...}, "keys": ["uid"]}, ...}}
  - Line 2+: One JSON object per row
    {"t": "claim", "video_key": "...", "kind": "...", "value": "...", ...}

Properties respected:
  1. Self-describing: Header declares columns, SQL types, and key columns per table.
  2. Additive: Reader widens existing tables automatically.
  3. Atomic: Writes to `.part` and renames on completion.
"""

from __future__ import annotations

import gzip
import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

import paths
from logger import vios_log as log
from sizing import SCHEMA_VERSION

SUB = "ENGINE"


def _infer_type(val: Any) -> str:
    """Infer SQLite column type for a sample value."""
    if val is None:
        return "TEXT"
    if isinstance(val, bool):
        return "INTEGER"
    if isinstance(val, int):
        return "INTEGER"
    if isinstance(val, float):
        return "REAL"
    return "TEXT"


def _build_table_meta(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Inspect rows to determine columns, types, and candidate key."""
    if not rows:
        return {"columns": {}, "keys": []}

    columns: Dict[str, str] = {}
    for r in rows:
        for k, v in r.items():
            if k not in columns and v is not None:
                columns[k] = _infer_type(v)

    # Candidate keys: look for standard identifier columns
    keys = []
    col_names = set(columns.keys())
    if "uid" in col_names:
        keys = ["uid"]
    elif "video_key" in col_names and "idx" in col_names:
        keys = ["video_key", "idx"]
    elif "video_key" in col_names and "kind" in col_names and "name" in col_names:
        keys = ["video_key", "kind", "name"]
    elif "video_key" in col_names and "t0" in col_names:
        keys = ["video_key", "t0"]
    elif "video_key" in col_names:
        keys = ["video_key"]

    return {"columns": columns, "keys": keys}


def write_shard(
    tables: Dict[str, List[Dict[str, Any]]],
    dest_dir: Optional[str] = None,
    session_id: Optional[str] = None,
    seq: int = 1,
) -> str:
    """Write rows to a gzipped evidence shard file.

    Parameters:
      tables: dict mapping table_name -> list of row dicts.
      dest_dir: destination directory (defaults to paths.SCRATCH_DIR).
      session_id: session tag (defaults to random token).
      seq: sequence number.

    Returns the absolute path to the generated .jsonl.gz file.
    """
    dest_dir = dest_dir or paths.SCRATCH_DIR
    os.makedirs(dest_dir, exist_ok=True)

    session = session_id or f"local_{secrets.token_hex(4)}"
    filename = f"vios-evidence-{seq:04d}-{session}.jsonl.gz"
    dest_path = os.path.join(dest_dir, filename)
    tmp_path = dest_path + ".part"

    # Build schema header
    tables_meta = {}
    total_rows = 0
    for tname, rows in tables.items():
        if rows:
            tables_meta[tname] = _build_table_meta(rows)
            total_rows += len(rows)

    header = {
        "_": "vios-evidence-shard",
        "schema": SCHEMA_VERSION,
        "session": session,
        "at": time.time(),
        "tables": tables_meta,
    }

    try:
        with gzip.open(tmp_path, "wt", encoding="utf-8") as gz:
            # Write line 1: header
            gz.write(json.dumps(header, separators=(",", ":")) + "\n")

            # Write row lines
            for tname, rows in tables.items():
                for r in rows:
                    rec = {"t": tname, **r}
                    gz.write(json.dumps(rec, separators=(",", ":")) + "\n")

        os.replace(tmp_path, dest_path)
        log(f"wrote evidence shard {filename} ({total_rows} rows, {os.path.getsize(dest_path)} bytes)", SUB, "SUCCESS")
        return dest_path
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        log(f"failed to write shard {filename} — {e}", SUB, "ERROR")
        raise


def publish_shard(shard_path: str) -> bool:
    """Publish a generated shard to the Telegram channel via tg_transport."""
    if not os.path.exists(shard_path):
        log(f"shard file not found: {shard_path}", SUB, "WARN")
        return False

    try:
        import tg_transport
        caption = f"📦 Evidence Shard: {os.path.basename(shard_path)}"
        msg_id = tg_transport.send_document(shard_path, caption=caption)
        if msg_id:
            log(f"published shard {os.path.basename(shard_path)} to channel (msg {msg_id})", SUB, "SUCCESS")
            return True
        log(f"tg_transport could not publish shard {os.path.basename(shard_path)}", SUB, "WARN")
        return False
    except Exception as e:
        log(f"failed to publish shard: {e}", SUB, "WARN")
        return False
