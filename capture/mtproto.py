"""
capture.mtproto - one pyrogram client on its own loop, in its own thread.

Lifted verbatim from `vios/process/intake.py` (class Channel, lines 278-473 of
upstream c105313) because it is the only thing the backfill planner needed from
the 17,688-line processing tree, and copying 200 lines is cheaper and clearer
than copying the tree. See WIRE.md for the upstream SHA.

The contract the callers rely on, unchanged: every method returns a falsy value
rather than raising when the transport is simply absent, so "no pyrogram" and
"no API id" are conditions the caller reports, not exceptions it handles.
"""

from __future__ import annotations

import os
import threading
import time


class SourceError(RuntimeError):
    """The bytes could not be obtained. Not the video's fault, not a pass's.

    Defined here rather than lifted, because upstream it lives at
    `intake.py:57` — outside the copied line range — and `download()` raises it
    on the "session is not running" path. A missing exception class turns that
    one clear message into a `NameError` raised from an except-branch, which is
    the worst possible place to lose a diagnosis.
    """


class Channel:
    """One pyrogram client, running on its own event loop, in its own thread.

    The processing worker is a plain thread with no loop of its own, and
    `asyncio.run` per call — which is what the capture plane does for its
    handful of oversize uploads — would reconnect for every download. Here the
    loop is started once and coroutines are posted to it from the worker with
    `run_coroutine_threadsafe`, so the session survives the whole sweep.

    Every method returns a falsy value rather than raising when the transport
    is simply absent. A session without pyrogram installed, or without an API
    id, is a session that uses the Bot API — not a session that fails.
    """

    def __init__(self, tg, log=None):
        self.tg = tg
        self.log = log or (lambda m: None)
        self.ready = False
        self.reason = ""
        self.last_error = ""
        self._loop = None
        self._thread = None
        self._app = None
        self._lock = threading.RLock()

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> bool:
        with self._lock:
            if self.ready:
                return True
            if not (self.tg and self.tg.token and self.tg.api_id
                    and self.tg.api_hash):
                self.reason = ("no API id and hash — large files and capture "
                               "records need MTProto")
                return False
            import asyncio  # noqa: PLC0415
            import importlib.util  # noqa: PLC0415

            import tgcompat  # noqa: PLC0415

            # `find_spec`, because the question is only "is the transport
            # installed" and importing pyrogram to find out costs 1.8 seconds.
            # It is also the guard that still works now that the import lives in
            # `tgcompat.client`: `import tgcompat` always succeeds, so wrapping
            # it in `except ImportError` would have made this branch unreachable.
            if importlib.util.find_spec("pyrogram") is None:
                self.reason = "pyrogram is not installed"
                return False

            # Widen pyrogram's channel id floor before the first call that names
            # the channel. Pyrogram rejects channel ids past 2**31 outright, and
            # a channel created recently has one — which surfaces here as every
            # download failing while the session itself reports healthy. See
            # vios/tgcompat.py for the whole story; `tgcompat.client` below does
            # this too, and this call is here so the note reaches the log once.
            note = tgcompat.patch()
            if note:
                self.log(note)

            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, name="vios-process-mtproto",
                daemon=True)
            self._thread.start()

            async def _boot():
                # Constructed inside the loop thread on purpose: some pyrogram
                # builds capture the running loop at __init__ time, and one
                # built on the worker's thread would post its callbacks
                # somewhere nothing is listening.
                app = tgcompat.client(
                    "vios_process", api_id=int(self.tg.api_id),
                    api_hash=self.tg.api_hash, bot_token=self.tg.token,
                    in_memory=True, no_updates=True,
                    max_concurrent_transmissions=2)
                await app.start()
                return app

            try:
                self._app = self._submit(_boot(), timeout=180)
                self.ready = True
                self.reason = ""
                self.log("MTProto session open")
            except Exception as exc:
                self.reason = f"{type(exc).__name__}: {str(exc)[:160]}"
                self._shutdown_loop()
            return self.ready

    def stop(self) -> None:
        with self._lock:
            if self._app is not None:
                try:
                    self._submit(self._app.stop(), timeout=60)
                except Exception:
                    pass
                self._app = None
            self._shutdown_loop()
            self.ready = False

    def _shutdown_loop(self) -> None:
        loop, thread = self._loop, self._thread
        self._loop = self._thread = None
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if thread and thread.is_alive():
            thread.join(timeout=10)
        try:
            loop.close()
        except Exception:
            pass

    def _submit(self, coro, timeout: float):
        import asyncio  # noqa: PLC0415
        if self._loop is None:
            raise SourceError("MTProto session is not running")
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result(timeout=timeout)

    # ── reading ──────────────────────────────────────────────────────────
    def messages(self, ids: list, timeout: float = 120.0) -> dict:
        """message id → message, for the ids that still exist."""
        if not self.ready or not ids:
            return {}
        want = [int(i) for i in ids if i]
        if not want:
            return {}

        async def _go():
            import asyncio  # noqa: PLC0415

            import tgcompat  # noqa: PLC0415
            from pyrogram.errors import FloodWait  # noqa: PLC0415

            # `self.tg` is handed in by whoever built this Channel, so the type
            # of `.channel` is not this class's to assume. A numeric string
            # reaches pyrogram as a phone number and raises `PeerIdInvalid` —
            # the same words the too-small id floor produced, from a different
            # cause. See `tgcompat.peer`; idempotent, so calling it here as well
            # as at the source costs nothing.
            chan = tgcompat.peer(self.tg.channel)
            for attempt in range(4):
                try:
                    out = await self._app.get_messages(chan, want)
                    if out is None:
                        return []
                    return out if isinstance(out, list) else [out]
                except FloodWait as e:
                    wait = int(getattr(e, "value", getattr(e, "x", 5))) + 1
                    self.log(f"Telegram asked for a {wait}s pause")
                    await asyncio.sleep(min(wait, 120))
            return []

        try:
            msgs = self._submit(_go(), timeout=timeout)
        except Exception as exc:
            # Kept, not just logged: `Source.ensure` raises the error the user
            # actually sees, and "could not download the original" with no
            # cause attached is what made this class of failure unreadable.
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.log(f"message fetch failed: {self.last_error}")
            return {}
        return {int(m.id): m for m in msgs
                if m is not None and not getattr(m, "empty", False)}

    def download(self, msg, dest: str, timeout: float = 900.0) -> bool:
        """Pull one message's media to an absolute path."""
        if not self.ready or msg is None:
            return False
        dest = os.path.abspath(dest)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"

        async def _go():
            import asyncio  # noqa: PLC0415
            from pyrogram.errors import FloodWait  # noqa: PLC0415
            for attempt in range(3):
                try:
                    return await self._app.download_media(msg, file_name=tmp)
                except FloodWait as e:
                    wait = int(getattr(e, "value", getattr(e, "x", 5))) + 1
                    await asyncio.sleep(min(wait, 120))
            return None

        try:
            got = self._submit(_go(), timeout=timeout)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
            self.log(f"download failed: {self.last_error}")
            got = None
        if not got or not os.path.exists(got):
            for stray in (tmp, dest + ".part"):
                if os.path.exists(stray):
                    try:
                        os.remove(stray)
                    except OSError:
                        pass
            return False
        try:
            os.replace(got, dest)
        except OSError:
            return False
        return True
