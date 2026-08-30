"""
Channel access.

Two transports, chosen per operation rather than per program:

  Bot API (HTTP)   getChat for the pinned manifest, and getFile for anything
                   under 20 MB. One request, no session file, no login. This is
                   the fast path Atlas uses on a cold start.
  MTProto (pyrogram)  everything the Bot API cannot do: reading an arbitrary
                   message by id, and downloading a file over 20 MB. Both are
                   required — the whole-channel scan is a walk over message ids,
                   and reels routinely exceed the HTTP download cap.

The one thing neither can do is list history. `messages.getHistory` is a
user-only method and returns BOT_METHOD_INVALID for a bot account, so there is
no "give me the last N messages" call available to us at all. The scan works
around it the way the harvester does: find the newest id by posting a message
and reading back its own id, then walk backwards asking for ids in batches.
Ids are dense enough in a channel that this is cheap, and it is the only method
a bot is actually permitted to use.

Pyrogram is async and Atlas's callers are not, so the client lives on its own
event loop in its own thread and every call out of here is a synchronous
wrapper with a deadline. That is deliberate: the bug that made the exporter
hang forever was an await with no timeout, and none of this code can repeat it.
"""

import asyncio
import json
import os
import re
import threading
import time

import requests

import tgcompat

from . import config

_LOG = []
_LOG_LOCK = threading.Lock()

# `/api/log` serves this ring buffer straight to the browser, so it needs the
# same credential redaction the desktop logger applies — and it needs to be the
# *same* rule, not a second copy that can drift. `logger` imports only `paths`,
# so this is safe from here even though `atlas` is a package below it.
try:
    from logger import redact as _redact
except Exception:                                      # noqa: BLE001
    # Atlas also runs on the Kaggle side, where the desktop logger is absent.
    # A crude but real fallback beats logging the token verbatim.
    _TOKEN_RE = re.compile(r"/bot(\d{5,}:)?[A-Za-z0-9_-]{20,}")

    def _redact(text: str) -> str:
        return _TOKEN_RE.sub("/bot[BOT_TOKEN]", text)


def log(msg: str, level: str = "INFO") -> None:
    msg = _redact(str(msg))
    line = f"{time.strftime('%H:%M:%S')} · {level} · {msg}"
    with _LOG_LOCK:
        _LOG.append(line)
        del _LOG[:-200]
    # Logging must never be able to fail a request. A Windows console runs
    # cp1252, so a print carrying the satellite glyph raises UnicodeEncodeError
    # — and this is called from inside the playback generator, where an
    # exception would abort a working video stream over a cosmetic character.
    try:
        print(f"📡 [ATLAS] {msg}", flush=True)
    except Exception:                                  # noqa: BLE001
        try:
            print(f"[ATLAS] {msg}".encode("ascii", "replace").decode("ascii"),
                  flush=True)
        except Exception:                              # noqa: BLE001
            pass


def recent_log(limit: int = 200) -> list:
    with _LOG_LOCK:
        return list(_LOG)[-int(limit):] if limit else list(_LOG)


# ══════════════════════════════════════════════════════════════════════════
# BOT API — the no-session fast path
# ══════════════════════════════════════════════════════════════════════════
_HTTP_TIMEOUT = (10, 60)          # (connect, read)


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"


def _call(method: str, params: dict = None, timeout=_HTTP_TIMEOUT) -> dict:
    """One Bot API call. Raises on transport failure, returns the result dict.

    Telegram answers 200 with ok=false for logical errors, so the status code
    alone never tells you whether it worked.
    """
    r = requests.get(_api(method), params=params or {}, timeout=timeout)
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"{method}: non-JSON reply ({r.status_code})")
    if not body.get("ok"):
        raise RuntimeError(f"{method}: {body.get('description', 'failed')}")
    return body.get("result")


def probe() -> dict:
    """Can we reach the channel at all? Answered in one round trip.

    This is the first thing the UI calls, because every other failure in Atlas
    is downstream of it and a missing token otherwise shows up as an empty
    library that looks like an empty channel.
    """
    missing = config.missing_secrets()
    if not config.BOT_TOKEN:
        return {"ok": False, "missing": missing,
                "error": "No bot token. Atlas cannot read the channel."}
    try:
        me = _call("getMe", timeout=(5, 15))
        chat = _call("getChat", {"chat_id": config.CHANNEL_ID}, timeout=(5, 20))
        pinned = (chat or {}).get("pinned_message")
        return {
            "ok": True,
            "missing": missing,
            "bot": me.get("username"),
            "channel": chat.get("title") or str(config.CHANNEL_ID),
            "channel_id": config.CHANNEL_ID,
            "pinned_message_id": (pinned or {}).get("message_id"),
            "mtproto": bool(config.API_ID and config.API_HASH),
        }
    except Exception as exc:
        return {"ok": False, "missing": missing, "error": str(exc)[:240]}


def pinned_message() -> dict:
    """The channel's pinned message, or None. The exporter pins each manifest,
    so this is the newest bundle in a single call — no scan needed to start."""
    try:
        chat = _call("getChat", {"chat_id": config.CHANNEL_ID})
        return (chat or {}).get("pinned_message")
    except Exception as exc:
        log(f"getChat failed: {exc}", "WARN")
        return None


def http_download(file_id: str, dest: str) -> bool:
    """Download by file_id over HTTP. False when the file is too big for the
    Bot API's 20 MB getFile cap, which is the caller's cue to use MTProto."""
    try:
        info = _call("getFile", {"file_id": file_id})
    except Exception as exc:
        # Not a fault, and it must not read like one. Every caller treats False
        # as "use MTProto next", and for a video over the 20 MB ceiling that is
        # the expected path — it succeeded on every occurrence of the last run,
        # while the log said "getFile failed" 60 times and sent the reader
        # looking for a download problem that did not exist.
        cap = config.HTTP_DOWNLOAD_LIMIT // 1048576
        if "too big" in str(exc).lower():
            log(f"over the Bot API's {cap} MB cap — fetching it over MTProto "
                f"instead")
        else:
            log(f"getFile unavailable ({exc}) — falling back to MTProto", "WARN")
        return False
    size = info.get("file_size") or 0
    if size > config.HTTP_DOWNLOAD_LIMIT:
        return False
    url = (f"https://api.telegram.org/file/bot{config.BOT_TOKEN}/"
           f"{info['file_path']}")
    tmp = dest + ".part"
    try:
        with requests.get(url, stream=True, timeout=(10, 300)) as r:
            r.raise_for_status()
            os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    if chunk:
                        f.write(chunk)
        os.replace(tmp, dest)
        return True
    except Exception as exc:
        log(f"download failed: {exc}", "WARN")
        if os.path.exists(tmp):
            os.remove(tmp)
        return False


# ══════════════════════════════════════════════════════════════════════════
# MTPROTO — arbitrary message reads and large downloads
# ══════════════════════════════════════════════════════════════════════════
def _raw(client, name: str):
    """The genuine coroutine function behind a pyrogram method.

    Pyrogram ships `sync.py`, which at import time rewrites every async method
    on Client into a synchronous wrapper via `functools.wraps`. Whether that
    wrapper returns a coroutine, a finished result, or a future depends on
    which thread you are on and whether a loop is already running — so calling
    `client.get_messages(...)` from Atlas's worker threads returned a *result*
    where this module expected an awaitable, and handing that to
    `run_coroutine_threadsafe` raised "An asyncio.Future, a coroutine or an
    awaitable is required".

    `functools.wraps` leaves the original on `__wrapped__`, which is the async
    method, unbound. Calling it with the client as the first argument gives a
    real coroutine every time, on any thread, with no dependence on ambient
    loop state. Falls back to the attribute itself for a pyrogram build that
    does not apply the sync layer.
    """
    method = getattr(client, name)
    inner = getattr(method, "__wrapped__", None)
    if inner is None:
        return method
    return lambda *a, **kw: inner(client, *a, **kw)


# "The socket died" versus "Telegram said no" — the distinction that decides
# whether reconnecting can help. It lives in `tgcompat` because this module and
# `capture/mtproto.py` each own an independent MTProto client and both need the
# same answer; see `tgcompat.is_transport_error` for the incident behind it.
_is_transport_error = tgcompat.is_transport_error


class _Mtproto:
    """A pyrogram client pinned to a private event loop on a private thread.

    Callers are synchronous FastAPI handlers and background workers. Rather
    than dragging an async runtime through the whole program (or reaching for
    nest_asyncio, which makes reentrancy bugs look like hangs), the client owns
    one loop and every entry point here is `submit(make_coro, timeout)`.

    **It rebuilds itself, and that is the point of this class now.** The version
    this replaced latched `_ready` on the first attempt and thereafter answered
    `start()` with `self._client is not None` — a check on whether an *object*
    exists, not on whether its socket does. So the first dropped connection was
    permanent for the life of the process: measured, one `WinError 10053` at
    10:53:19 poisoned every download until 11:49 and beyond, with the mirror
    retrying against a dead socket every five seconds and reporting itself
    healthy the entire time. A laptop left open overnight hits this every night.

    The recovery is a generation counter rather than a flag. When a call fails
    with a transport error it asks for the session it used to be retired, naming
    the generation it saw; if another thread already retired that generation the
    request is a no-op. Without it, twenty worker threads failing on the same
    dead socket would each tear down and rebuild — nineteen of them destroying a
    session a sibling had just finished negotiating.
    """

    def __init__(self):
        self._loop = None
        self._thread = None
        self._client = None
        self._ready = threading.Event()
        self._error = None
        self._lock = threading.RLock()
        self._gen = 0
        self._builds = 0
        self._restarts = 0
        self._last_restart = 0.0
        self._last_fail = ""

    # ── lifecycle ────────────────────────────────────────────────────────
    def available(self) -> bool:
        return bool(config.API_ID and config.API_HASH and config.BOT_TOKEN)

    # A failed rebuild is cheap but not free — it costs a DC handshake attempt —
    # and the thing that triggers rebuilds is a worker loop that retries every
    # few seconds. Without a floor, an unplugged network turns into a connect
    # storm. Twenty seconds is short enough that a wifi drop recovers on the
    # first retry a user could notice and long enough that fifty stranded reels
    # cannot compound into fifty handshakes.
    _REBUILD_COOLDOWN = 20.0

    def start(self) -> bool:
        """Idempotent. Returns False if MTProto cannot be used *right now*.

        "Right now" is the change. This used to be a permanent verdict: the
        first answer was cached in `_ready` and every later caller got
        `self._client is not None`, which asks whether an object was constructed
        and not whether its connection is alive.

        Two latches had to go, and the second one is subtle enough that fixing
        only the first would have looked like a fix and not been one.

        The first is the obvious one: a session that dies is never replaced.
        `recycle()` clears `_ready`, so the next call through here builds a new
        one.

        The second is a *failed build*. `_run` sets `_ready` in a `finally`, so a
        connect attempt that raises still marks the class as having an answer —
        and the old code's `if self._thread is None` guard then saw the dead
        thread object and never spawned another. So one failed reconnect (a wifi
        drop, a DNS blip like the `NameResolutionError` this app logged at
        boot) was as permanent as the original bug. A thread that has finished
        is not a build in flight, and this now says so.
        """
        if not self.available():
            self._error = ("MTProto needs TELEGRAM_API_ID and "
                           "TELEGRAM_API_HASH as well as the bot token.")
            return False
        with self._lock:
            if self._ready.is_set() and self._client is not None:
                return True
            building = self._thread is not None and self._thread.is_alive()
            if not building:
                since = time.time() - self._last_restart
                if self._builds and since < self._REBUILD_COOLDOWN:
                    # Recently tried and lost. Say so rather than queueing
                    # another handshake behind the one that just failed.
                    return False
                self._thread = None
                self._loop = None
                self._client = None
                self._ready.clear()
                self._builds += 1
                self._last_restart = time.time()
                self._thread = threading.Thread(target=self._run,
                                                name="atlas-mtproto",
                                                daemon=True)
                self._thread.start()
        # 120 s: a cold pyrogram start negotiates an auth key and can be slow
        # on a fresh container, but it must not be unbounded.
        self._ready.wait(timeout=120)
        return self._client is not None

    def recycle(self, generation: int = -1, why: str = "") -> None:
        """Retire the session so the next `start()` builds a fresh one.

        `generation` is the sequence number the caller was using when its call
        failed. If it no longer matches, some other thread has already replaced
        that session and this is a stale request from a sibling that failed on
        the same dead socket — dropping it is the whole point. Pass -1 to mean
        "whatever is current", which is what an explicit reconnect wants.

        Tearing down is best-effort by construction. The session being retired is
        one whose socket is known to be broken, so `client.stop()` is quite
        likely to raise; the loop is stopped and the thread joined with a
        deadline either way, and anything still holding the old client will fail
        its next call and land back here with a generation that has moved on.
        """
        with self._lock:
            if generation >= 0 and generation != self._gen:
                return
            client, loop, thread = self._client, self._loop, self._thread
            self._client = self._loop = self._thread = None
            self._gen += 1
            self._restarts += 1
            # `_last_restart` is deliberately NOT touched here. It records when a
            # *build* was last attempted, and `start()` uses it as the cooldown
            # floor — so stamping it on the way out of a teardown would make the
            # very next `start()` refuse for twenty seconds, which turns
            # `mtproto_reconnect()` into a function that reliably reports
            # failure and then quietly works later. The build path owns that
            # clock.
            if why:
                self._last_fail = why[:200]
                self._error = f"reconnecting after {why[:160]}"
            self._ready.clear()
        if client is not None and loop is not None:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _raw(client, "stop")(), loop)
                fut.result(timeout=15)
            except Exception:
                pass
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None and thread.is_alive():
            thread.join(timeout=15)
        if loop is not None:
            try:
                loop.close()
            except Exception:
                pass
        log(f"MTProto session retired — {why[:120] or 'on request'}", "WARN")

    def _run(self):
        # `find_spec`, not a real import: the question here is only "is the
        # transport installed", and answering it by importing pyrogram costs 1.8
        # seconds on a thread whose whole job is to not block anything. It is
        # also the honest guard now that the import itself moved into
        # `tgcompat.client` — `import tgcompat` always succeeds, so keeping
        # `except ImportError` around it would have made this branch dead code
        # and turned "no transport" into a 120-second wait.
        import importlib.util
        if importlib.util.find_spec("pyrogram") is None:
            self._error = "pyrogram is not installed"
            self._ready.set()
            return
        import tgcompat

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop

        async def _start():
            try:
                # `tgcompat.client`, not `pyrogram.Client`. Two things it does
                # that a bare import did not, and this thread needed both:
                # it widens pyrogram's channel id floor, without which every
                # call naming a modern channel dies on `Peer id invalid` while
                # the session itself reports healthy; and it gives the importing
                # thread an event loop, without which `import pyrogram` raises
                # `RuntimeError` — which is not `ImportError`, so the guard
                # above never caught it and this thread died before setting
                # `_ready`, leaving `start()` to wait out its full 120 s.
                client = tgcompat.client(
                    "atlas_reader",
                    api_id=config.API_ID, api_hash=config.API_HASH,
                    bot_token=config.BOT_TOKEN,
                    workdir=config.SESSION_DIR,
                    no_updates=True,
                    in_memory=False,
                    # Pyrogram defaults this to 1, which puts every file
                    # transfer behind a single semaphore. With streaming
                    # playback that means a prefetch warming somebody else's
                    # thumbnail stalls the video actually on screen. Four lets
                    # the player, a warm and a background download coexist
                    # without inviting the flood limits a wide pool trips.
                    max_concurrent_transmissions=4)
                await _raw(client, "start")()
                self._client = client
                self._error = None       # cleared: a successful rebuild must not
                                         # leave the previous drop's message
                                         # showing in the status panel forever
                log("MTProto session ready" if self._builds <= 1 else
                    f"MTProto session rebuilt (attempt {self._builds})")
            except Exception as exc:
                self._error = f"{type(exc).__name__}: {str(exc)[:200]}"
                log(f"MTProto unavailable — {self._error}", "WARN")
            finally:
                self._ready.set()

        loop.run_until_complete(_start())
        established = self._client is not None
        if established:
            loop.run_forever()
        # The loop has stopped: either `recycle()` asked it to, or the session
        # never started. Nothing else is allowed to submit to it, so make that
        # true rather than hoping — a stale `_loop` here is a
        # `run_coroutine_threadsafe` into a loop nobody is running, which hangs
        # until the caller's timeout instead of failing.
        #
        # `_ready` is only cleared when a session actually existed. Clearing it
        # on a *failed* build would race the `_ready.wait(120)` in `start()`:
        # `_start`'s `finally` sets it, this line unsets it, and a caller that
        # had not yet reached its wait sits there for the full two minutes
        # waiting for an event that has already come and gone.
        with self._lock:
            if self._loop is loop:
                self._loop = None
                if established:
                    self._client = None
                    self._ready.clear()
        try:
            if not loop.is_closed():
                loop.close()
        except Exception:
            pass

    def submit(self, make_coro, timeout: float, tries: int = 2):
        """Run a coroutine on the client's loop and wait, with a deadline.

        **`make_coro` is a factory, not a coroutine, and that is what makes the
        retry possible.** A coroutine object can only be awaited once, so the
        previous signature — `submit(_go(), timeout)` — physically could not be
        retried: by the time the failure was visible the only thing that could
        be run again had already been consumed. Callers now pass `_go`, and the
        second attempt builds a fresh coroutine against the fresh client.

        A coroutine is still accepted, for any call site that has not been
        converted, and gets exactly one attempt. That is not a courtesy so much
        as a refusal to guess: re-awaiting a spent coroutine raises
        `RuntimeError: cannot reuse already awaited coroutine`, which would read
        in the log as a bug in this class rather than as a call site to fix.

        On a transport error the session is retired and the call is tried once
        more on a new one. On anything else — FloodWait handled upstream, a
        message that does not exist, a permission refusal — the exception
        propagates untouched, because reconnecting cannot change the answer and
        a retry loop that hides real errors is how a scan silently returns half
        a channel.
        """
        reusable = callable(make_coro)
        if not reusable:
            tries = 1
        last = None
        for attempt in range(max(1, tries)):
            with self._lock:
                gen = self._gen
            if not self.start():
                raise RuntimeError(self._error or "MTProto unavailable")
            with self._lock:
                loop = self._loop
            if loop is None:
                # Retired between `start()` and here. Not an error — go round.
                last = RuntimeError("MTProto session was retired mid-call")
                continue
            coro = make_coro() if reusable else make_coro
            fut = asyncio.run_coroutine_threadsafe(coro, loop)
            try:
                return fut.result(timeout=timeout)
            except Exception as exc:
                fut.cancel()
                last = exc
                if attempt >= max(1, tries) - 1 or not _is_transport_error(exc):
                    raise
                self.recycle(gen, f"{type(exc).__name__}: {str(exc)[:120]}")
        raise last if last is not None else RuntimeError("MTProto unavailable")

    @property
    def generation(self) -> int:
        return self._gen

    def health(self) -> dict:
        """What the status endpoints report, so a dead socket is visible.

        The failure this whole class was rewritten for was invisible for
        fifty-six minutes because nothing anywhere said "the connection is
        down" — `/api/mirror/status` said `running: true` and the counts simply
        stopped moving. A number of restarts and the last transport failure are
        the two facts that would have made it obvious.
        """
        with self._lock:
            return {
                "available": self.available(),
                "connected": self._client is not None and self._ready.is_set(),
                "generation": self._gen,
                "builds": self._builds,
                "restarts": self._restarts,
                "last_restart": self._last_restart,
                "last_transport_error": self._last_fail,
                "error": self._error or "",
            }

    @property
    def client(self):
        return self._client

    @property
    def loop(self):
        return self._loop

    @property
    def error(self):
        return self._error


_mt = _Mtproto()


def mtproto_ready() -> bool:
    return _mt.start()


def mtproto_error() -> str:
    return _mt.error or ""


def mtproto_health() -> dict:
    """Connection state, restart count and last transport failure.

    Exposed as a module function because every status endpoint in the app reaches
    this module and none of them should reach into `_mt`. What it is for: the
    dropped-session failure was invisible for fifty-six minutes because no
    endpoint anywhere reported the transport at all, so the mirror's frozen
    counts were the only symptom and they read as slowness.
    """
    return _mt.health()


def mtproto_reconnect(why: str = "asked to") -> bool:
    """Throw the session away and build a new one. Returns True if it came back.

    The manual door behind the Admin panel's reconnect button, and the thing a
    "Download now" click can do when the queue has been failing: recovery is
    automatic on transport errors, but a session can be *logically* stale in ways
    no exception announces — a token rotated, a bot removed and re-added — and
    then the only cure is a fresh session that nobody has to wait eight hours to
    trigger by accident.
    """
    _mt.recycle(-1, why)
    return _mt.start()


async def _with_floodwait(make_coro, tries: int = 4):
    """Retry a call across FloodWait, which the channel WILL throw during a
    full scan. Anything else propagates — a retry loop that swallows real
    errors is how a scan silently returns half a channel."""
    from pyrogram.errors import FloodWait
    for attempt in range(tries):
        try:
            return await make_coro()
        except FloodWait as e:
            wait = int(getattr(e, "value", getattr(e, "x", 5))) + 1
            if attempt == tries - 1:
                raise
            log(f"FloodWait {wait}s — pausing the scan", "WARN")
            await asyncio.sleep(wait)
    return None


def newest_message_id() -> int:
    """The highest message id in the channel.

    A bot cannot ask. It can, however, post — and the id it gets back is the
    current head. So: post a dot, keep the number, delete the dot. The channel
    sees a message that exists for well under a second, which is the price of
    an API that has no getHistory for bots.
    """
    try:
        sent = _call("sendMessage", {"chat_id": config.CHANNEL_ID,
                                     "text": ".",
                                     "disable_notification": "true"})
        mid = sent["message_id"]
        try:
            _call("deleteMessage", {"chat_id": config.CHANNEL_ID,
                                    "message_id": mid})
        except Exception:
            # A failed delete leaves a dot in the channel. Harmless, and much
            # less bad than failing the scan over it.
            pass
        return int(mid)
    except Exception as exc:
        log(f"could not determine newest message id: {exc}", "ERROR")
        return 0


def get_messages(ids: list, timeout: float = 120) -> list:
    """Read messages by id over MTProto. Missing ids come back as None and are
    dropped — deleted messages and service events leave gaps everywhere."""
    if not ids:
        return []

    async def _go():
        # Read the client *here*, not in the enclosing scope. `submit` may build
        # a second coroutine from this factory after retiring a dead session, and
        # a client captured before that retirement is the very object whose
        # socket just failed.
        client = _mt.client
        return await _with_floodwait(
            lambda: _raw(client, "get_messages")(
                tgcompat.peer(config.CHANNEL_ID), ids))

    out = _mt.submit(_go, timeout=timeout)
    if out is None:
        return []
    if not isinstance(out, list):
        out = [out]
    return [m for m in out if m is not None and not getattr(m, "empty", False)]


_LAST_DL_ERROR = ""


def last_download_error() -> str:
    """Why the most recent MTProto download failed, or "".

    `download_message` returns a bool because most callers only branch on it,
    but the mirror has to be able to tell a user *why* a reel is not on disk —
    and "could not download the original" with no cause attached is precisely
    the message that made an hour of dead-socket failures look like Telegram
    losing a video. Read it straight after a False.
    """
    return _LAST_DL_ERROR


def download_message(message, dest: str, progress=None,
                     timeout: float = 900) -> bool:
    """Download a message's media over MTProto. Handles files of any size."""
    global _LAST_DL_ERROR

    async def _go():
        return await _with_floodwait(
            lambda: _raw(_mt.client, "download_media")(
                message, file_name=dest,
                progress=(lambda c, t: progress(c, t)) if progress else None))

    try:
        got = _mt.submit(_go, timeout=timeout)
        if bool(got) and os.path.exists(dest):
            _LAST_DL_ERROR = ""
            return True
        _LAST_DL_ERROR = ("Telegram returned no file — the message may have "
                          "been deleted or has no media")
        return False
    except Exception as exc:
        _LAST_DL_ERROR = f"{type(exc).__name__}: {str(exc)[:180]}"
        log(f"MTProto download failed: {str(exc)[:160]}", "WARN")
        return False


def download_by_id(message_id: int, dest: str, progress=None) -> bool:
    """Fetch one message's media straight to `dest`, whatever its size."""
    msgs = get_messages([message_id])
    if not msgs:
        return False
    return download_message(msgs[0], dest, progress=progress)


# ══════════════════════════════════════════════════════════════════════════
# STREAMING — bytes out of Telegram without waiting for the whole file
# ══════════════════════════════════════════════════════════════════════════
CHUNK = 1024 * 1024          # pyrogram's fixed stream granularity
STREAM_STALL = 90.0          # give up on a transfer that has gone quiet


def stream_chunks(message, first_chunk: int = 0, chunk_limit: int = 0,
                  queue_size: int = 4):
    """Yield 1 MiB pieces of a message's media, starting at `first_chunk`.

    This is what makes a video playable before it has finished arriving.
    `download_media` has to complete before it returns anything, so a 30 MB
    reel means half a minute of nothing; `stream_media` hands back pieces as
    they land, and Telegram lets us skip straight to the chunk containing the
    byte the browser asked for — so seeking to the middle of a video does not
    fetch the beginning.

    Pyrogram is async and the caller is a synchronous response generator, so the
    async generator runs on the MTProto loop and pushes into a bounded queue.
    Bounded matters: without it a fast connection buffers the whole file into
    RAM, which is exactly what this is meant to avoid. The queue applies
    backpressure, so Atlas reads from Telegram at roughly the speed the browser
    drains it.

    Every hand-off — pieces, the end marker, an error — goes through `_offer`,
    which waits by awaiting rather than by blocking. A blocking `put` on the
    loop thread would freeze the entire MTProto client the moment a viewer
    paused a video with a full queue, and nothing could cancel it because a
    thread stuck in C code has no await point to interrupt.
    """
    import queue as _queue

    if not _mt.start():
        raise RuntimeError(_mt.error or "MTProto unavailable")
    # Both read once, together, before anything is scheduled. A generator that
    # re-read `_mt.loop` later could post its pump to a loop built after a
    # reconnect while its client came from before one.
    generation, loop, client = _mt.generation, _mt.loop, _mt.client
    if loop is None or client is None:
        raise RuntimeError(_mt.error or "MTProto session was retired")

    q = _queue.Queue(maxsize=queue_size)
    DONE = object()

    async def _offer(item):
        while True:
            try:
                q.put_nowait(item)
                return
            except _queue.Full:
                await asyncio.sleep(0.02)

    async def _pump():
        agen = None
        try:
            agen = _raw(client, "stream_media")(
                message, offset=first_chunk, limit=chunk_limit)
            async for piece in agen:
                await _offer(piece)
            await _offer(DONE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:                       # noqa: BLE001
            await _offer(exc)
        finally:
            # Release Telegram's side of the transfer straight away rather than
            # leaving it to garbage collection, which on a paused video could be
            # a long time holding a media session open.
            if agen is not None:
                try:
                    await agen.aclose()
                except Exception:                      # noqa: BLE001
                    pass

    fut = asyncio.run_coroutine_threadsafe(_pump(), loop)
    try:
        while True:
            try:
                item = q.get(timeout=STREAM_STALL)
            except _queue.Empty:
                # Nothing for a long time and the pump is gone: the loop died or
                # the coroutine was cancelled out from under us. A plain
                # blocking get would hold this worker thread forever.
                if fut.done():
                    return
                # A stall this long on a live socket is indistinguishable from a
                # dead one from here, and the cost of being wrong is one
                # reconnect. Retiring the session is what stops a single
                # half-open TCP connection from making every subsequent seek in
                # every video hang for ninety seconds.
                _mt.recycle(generation, f"stream stalled {STREAM_STALL:.0f}s")
                raise RuntimeError("Telegram stopped sending — no data for "
                                   f"{STREAM_STALL:.0f}s")
            if item is DONE:
                return
            if isinstance(item, Exception):
                # Retire before raising, not instead of raising. The player will
                # re-request this byte range within a second or two; that retry
                # should meet a new session rather than the one that just died.
                if _is_transport_error(item):
                    _mt.recycle(
                        generation,
                        f"{type(item).__name__}: {str(item)[:120]}")
                raise item
            yield item
    finally:
        # The browser closing the tab mid-video lands here. Cancelling stops
        # pulling bytes we are throwing away.
        fut.cancel()


def message_by_id(message_id: int):
    """One message object, or None. Used by the streaming player."""
    msgs = get_messages([message_id])
    return msgs[0] if msgs else None


def media_facts(message) -> dict:
    """Size and type of a message's media, without fetching any of it.

    A 206 response has to state the file's total length in `Content-Range`
    before a single byte is sent, so this is read off the message itself.
    """
    info = message_document(message)
    return {
        "size": int(info.get("file_size") or 0),
        "mime": info.get("mime") or "video/mp4",
        "name": info.get("file_name") or "",
        "duration": info.get("duration"),
    }


# ══════════════════════════════════════════════════════════════════════════
# MESSAGE SHAPES
# ══════════════════════════════════════════════════════════════════════════
def message_document(msg) -> dict:
    """Normalise a message down to what Atlas cares about.

    Handles both shapes, because both transports are in use and they disagree
    on everything: pyrogram hands back objects with attributes, `.id` and a
    `datetime`, while the Bot API hands back plain dicts with `message_id` and
    a Unix int. The pinned-manifest fast path comes from the Bot API and the
    channel walk comes from pyrogram, so a function that only understood one of
    them would make the fast path silently find no bundles at all.
    """
    if msg is None:
        return {}

    if isinstance(msg, dict):
        media = msg.get("document") or msg.get("video") or {}
        vid = msg.get("video") or {}
        return {
            "message_id": msg.get("message_id") or msg.get("id"),
            "date": msg.get("date"),
            "caption": msg.get("caption") or "",
            "file_name": media.get("file_name"),
            "file_id": media.get("file_id"),
            "file_size": media.get("file_size"),
            "mime": media.get("mime_type"),
            "is_video": bool(vid),
            "duration": vid.get("duration") or None,
            "width": vid.get("width") or None,
            "height": vid.get("height") or None,
        }

    doc = getattr(msg, "document", None)
    vid = getattr(msg, "video", None)
    media = doc or vid
    when = getattr(msg, "date", None)
    return {
        "message_id": getattr(msg, "id", None) or getattr(msg, "message_id",
                                                          None),
        "date": (when.timestamp() if hasattr(when, "timestamp") else when),
        "caption": getattr(msg, "caption", None) or "",
        "file_name": getattr(media, "file_name", None) if media else None,
        "file_id": getattr(media, "file_id", None) if media else None,
        "file_size": getattr(media, "file_size", None) if media else None,
        "mime": getattr(media, "mime_type", None) if media else None,
        "is_video": vid is not None,
        "duration": getattr(vid, "duration", None) if vid else None,
        "width": getattr(vid, "width", None) if vid else None,
        "height": getattr(vid, "height", None) if vid else None,
    }


def looks_like_manifest(info: dict) -> bool:
    """Is this message a bundle manifest?

    Two independent signals, because either one alone has a failure mode: the
    file name is what the exporter writes today, and the caption marker is what
    survives if a future exporter renames the file. Matching either keeps Atlas
    reading bundles it did not write.
    """
    name = (info.get("file_name") or "").lower()
    cap = info.get("caption") or ""
    if name.startswith("manifest-") and name.endswith(".json"):
        return True
    if "VIOS bundle" in cap and "manifest" in cap.lower():
        return True
    return "✅ VIOS bundle" in cap


SHARD_PREFIX = "vios-evidence-"
SHARD_SUFFIX = ".jsonl.gz"


def looks_like_shard(info: dict) -> bool:
    """Is this message an evidence shard from the GPU plane?

    Two independent lanes post to this channel and they share no format. The
    harvester posts *bundles*: a manifest naming the parts of a SQLite
    snapshot. The process engine posts *shards*: one gzipped JSONL file per
    batch of claims, complete on its own, with no manifest anywhere and nothing
    pinned. A reader that only knew manifests walked straight past every claim
    the GPU ever produced — which is exactly what Atlas did.

    Same two signals as a manifest, for the same reason: the file name is what
    the engine writes today, the caption marker is what survives a rename.
    """
    name = (info.get("file_name") or "").lower()
    if name.startswith(SHARD_PREFIX) and name.endswith(SHARD_SUFFIX):
        return True
    cap = (info.get("caption") or "").strip().lower()
    return cap.startswith("vios evidence")


def shard_seq(info: dict) -> str:
    """The shard's own id — `<site>-<seq>` — from its name, caption, or failing
    both, its message id.

    The site prefix is the load-bearing half. Ten Kaggle accounts each number
    their shards from one, so `0007` alone names ten different files; keyed on
    that, importing worker 3's seventh shard would mark worker 7's as held.
    """
    name = (info.get("file_name") or "").strip()
    if name.lower().startswith(SHARD_PREFIX) and \
            name.lower().endswith(SHARD_SUFFIX):
        got = name[len(SHARD_PREFIX):-len(SHARD_SUFFIX)].strip()
        if got:
            return got
    cap = (info.get("caption") or "").strip()
    m = re.match(r"vios\s+evidence\s*[·:•-]\s*(\S+)", cap, re.I)
    if m:
        return m.group(1)
    return f"msg{info.get('message_id')}"


def fetch_document(info: dict, dest: str) -> bool:
    """Pull a message's file to `dest`, cheap transport first.

    HTTP needs no session and no login, so it is tried whenever the message
    carried a `file_id`; it returns False rather than raising when the file is
    over the Bot API's 20 MB ceiling, and MTProto picks that up.
    """
    ok = False
    if info.get("file_id"):
        ok = http_download(info["file_id"], dest)
    if not ok and info.get("message_id"):
        ok = download_by_id(info["message_id"], dest)
    return bool(ok and os.path.exists(dest))


def read_manifest_document(info: dict, work_dir: str) -> dict:
    """Download a manifest message and parse it. Manifests are a few KB, so the
    HTTP path always fits and MTProto is only the fallback."""
    dest = os.path.join(work_dir, f"manifest-{info['message_id']}.json")
    if not fetch_document(info, dest):
        return None
    try:
        with open(dest, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        log(f"manifest {info['message_id']} is not readable JSON: {exc}", "WARN")
        return None
    if not isinstance(data, dict) or "parts" not in data:
        return None
    data["_message_id"] = info["message_id"]
    data["_date"] = info.get("date")
    return data
