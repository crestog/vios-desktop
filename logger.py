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
import re
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


# ── Credential redaction ──────────────────────────────────────────────────
# A log line may say *which* secret was involved and must never say what it was.
# Both halves matter: `[BOT_TOKEN]` tells the operator exactly which credential
# to look at, which is the entire diagnostic value the raw string ever had.
#
# This exists because of a real leak, and the shape of that leak is why the fix
# lives here rather than in the callers. Nobody wrote a line containing a token.
# `requests` and `httpx` both put the full request URL into their own exception
# messages, so `getMe: ConnectError` reached the log — and the rotating file on
# disk — as `https://api.telegram.org/bot<id>:<secret>/getMe`. Any caller that
# ever formats an exception is a leak, so the choke point is the only place that
# can be made safe once.
_REDACT_FIELDS = ("BOT_TOKEN", "API_HASH", "HF_TOKEN", "IG_COOKIES")

# Matched by shape as well as by value, so a stale token still sitting inside an
# exception raised before a rotation is caught even though `config` no longer
# knows it. `{20,}` is well under a real token's secret half (35 chars) and well
# above anything in a path segment that could be mistaken for one.
_TOKEN_RE = re.compile(r"/bot(\d{5,}:)?[A-Za-z0-9_-]{20,}")
_BARE_TOKEN_RE = re.compile(r"\b\d{7,12}:[A-Za-z0-9_-]{30,}\b")

_secrets: tuple = ()
_secrets_at = 0.0
_SECRETS_TTL = 5.0          # matches config's own credential-file cache


def _live_secrets() -> tuple:
    """Current credential values, cached briefly.

    `config` is imported lazily because `logger` is the module everything else
    imports first; a top-level import here would make the credential layer a
    prerequisite for printing a line. Re-read rather than snapshotted, so a token
    typed into the Admin form is redacted from the very next log line without a
    restart — and a token that was rotated stops being redacted only after it has
    stopped being a secret.
    """
    global _secrets, _secrets_at
    now = time.monotonic()
    if now - _secrets_at < _SECRETS_TTL:
        return _secrets
    found = []
    try:
        import config as _config
        for name in _REDACT_FIELDS:
            try:
                val = getattr(_config, name, "")
            except Exception:
                continue
            if isinstance(val, str) and len(val.strip()) >= 8:
                found.append((val.strip(), f"[{name}]"))
    except Exception:
        pass
    # Longest first, so a value that contains another is replaced whole.
    found.sort(key=lambda pair: -len(pair[0]))
    _secrets = tuple(found)
    _secrets_at = now
    return _secrets


def redact(text: str) -> str:
    """Replace credential values with their credential names."""
    out = text
    for value, name in _live_secrets():
        if value in out:
            out = out.replace(value, name)
    out = _TOKEN_RE.sub("/bot[BOT_TOKEN]", out)
    return _BARE_TOKEN_RE.sub("[BOT_TOKEN]", out)


def vios_log(message, subsystem: str = "SYS", level: str = "INFO") -> None:
    # Redacted once, at the top, so every downstream copy of the line is clean:
    # the console, the ring buffer the Admin view polls, and the rotating file on
    # disk. Doing it in each caller instead would mean every future `log(...)` is
    # a chance to leak, and the leak this fixes was not written by a caller at
    # all — an HTTP client put the request URL, token and all, into its own
    # exception message.
    message = redact(str(message))
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
        "message": message,
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
