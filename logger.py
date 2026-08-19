"""
logger — one process, one ring buffer, one file.

The Kaggle version of this module pushed every line into a Redis list, because
over there the boot script, the UI server, the capture engine and the GPU worker
are four separate processes and the Admin panel had to read all four. It carried a
`_port_open` probe and a 30-second backoff for a measured reason: with no Redis
listening, `Redis(socket_connect_timeout=2).ping()` took **48 seconds** to give
up, so logging became the bottleneck at exactly the moment somebody needed to read
the logs.

Here there is one process. The uvicorn server, the mirror worker, the scan thread
and the model worker are threads inside it, so an in-memory deque *is* the shared
buffer and the whole Redis apparatus — the client, the probe, the backoff, the
dependency — deletes itself. That is the single largest simplification the split
buys in this file.

Two things are kept rather than simplified away:

  * `_safe_print`. The subsystem prefixes are emoji and Windows consoles still
    default to cp1252, where encoding one raises `UnicodeEncodeError`. Unguarded,
    a log line becomes a worker crash. Measured on this machine while building
    this repo: a plain `print("→")` from a subprocess died exactly that way.
  * The rotating file. Kaggle's log was the notebook's stdout, which the operator
    was watching live; a desktop app's stdout goes into a window nobody has open,
    so a week-long capture run needs its log to still exist tomorrow.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from collections import deque

import paths

# ── Console encoding ──────────────────────────────────────────────────────
# Fix the cause, then keep the guard for the case where the fix is unavailable
# (a pipe that reports no encoding, an embedded interpreter, a redirected handle
# already opened in binary). Reconfiguring is better than transliterating: with
# it the operator sees the real characters instead of `?`.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass

SUBSYSTEMS = {
    "SYS":     "⚙️  [SYSTEM]",
    "UI":      "🖥️  [UI]",
    "SCAN":    "📡  [SCAN]",
    "MIRROR":  "⬇️  [MIRROR]",
    "MEDIA":   "🎞️  [MEDIA]",
    "LIBRARY": "📁  [LIBRARY]",
    "CAPTURE": "📥  [CAPTURE]",
    "ENGINE":  "🤖  [ENGINE]",
    "ADMIN":   "🛡️  [ADMIN]",
    "ATLAS":   "🔎  [ATLAS]",
}

LEVELS = {
    "INFO":    "",
    "SUCCESS": "✅ ",
    "WARN":    "⚠️ ",
    "ERROR":   "❌ ",
}

# 2,000 rather than Kaggle's 500. The Admin log view is the only place a
# week-long capture run's pacing decisions can be inspected, and at one line per
# fetch 500 is under an hour of history. This costs a few hundred KB of RAM.
LOG_BUFFER: deque = deque(maxlen=2000)

_lock = threading.Lock()
_LOG_PATH = os.path.join(paths.LOG_DIR, "vios.log")
_MAX_BYTES = 8 * 1024 * 1024   # one rotation, so at most ~16 MB on disk


def _safe_print(msg: str) -> None:
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"), flush=True)
    except Exception:
        pass


def _rotate_if_needed() -> None:
    try:
        if os.path.getsize(_LOG_PATH) < _MAX_BYTES:
            return
    except OSError:
        return
    try:
        os.replace(_LOG_PATH, _LOG_PATH + ".1")
    except OSError:
        pass


def _append_file(line: str) -> None:
    """Best-effort, and never a reason for a caller to fail.

    A log write that raises inside a worker loop turns an observability feature
    into an outage, so every failure here is swallowed. The console line has
    already been emitted by the time this runs, so nothing is lost silently.
    """
    try:
        _rotate_if_needed()
        with open(_LOG_PATH, "a", encoding="utf-8", errors="replace") as f:
            f.write(line + "\n")
    except Exception:
        pass


def vios_log(message, subsystem: str = "SYS", level: str = "INFO") -> None:
    ts = time.strftime("%H:%M:%S")
    prefix = SUBSYSTEMS.get(subsystem, f"[{subsystem}]")
    icon = LEVELS.get(level, "")
    formatted = f"[{ts}] {prefix} {icon}{message}"
    _safe_print(formatted)

    entry = {
        "ts": ts,
        "time": time.time(),
        "subsystem": subsystem,
        "level": level,
        "message": str(message),
    }
    with _lock:
        LOG_BUFFER.append(entry)
    _append_file(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {level:<7} "
                 f"{subsystem:<7} {message}")


def get_recent_logs(count: int = 200, subsystem: str = "",
                    level: str = "") -> list:
    """Recent lines, newest last, optionally filtered.

    Filtering happens here rather than in the client because the Admin view polls
    this and shipping 2,000 rows to filter four of them in TypeScript is the kind
    of thing that shows up as a janky status strip.
    """
    with _lock:
        rows = list(LOG_BUFFER)
    if subsystem:
        rows = [r for r in rows if r["subsystem"] == subsystem]
    if level:
        rows = [r for r in rows if r["level"] == level]
    return rows[-count:]


def log_path() -> str:
    return _LOG_PATH


def dump_json(count: int = 2000) -> str:
    return json.dumps(get_recent_logs(count), ensure_ascii=False)
