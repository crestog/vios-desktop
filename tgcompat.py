"""
vios.tgcompat — make pyrogram able to see modern channel ids.

This module exists because of one hardcoded constant in a library that stopped
being maintained in 2023.

Pyrogram decides whether a chat id is a user, a group or a channel by range
check, in `pyrogram.utils.get_peer_type`:

    MIN_CHAT_ID    = -2147483647             # basic groups
    MIN_CHANNEL_ID = -1002147483647          # -100 followed by 2**31
    MAX_CHANNEL_ID = -1000000000000

    if peer_id < 0:
        if MIN_CHAT_ID <= peer_id:
            return "chat"
        if MIN_CHANNEL_ID <= peer_id < MAX_CHANNEL_ID:
            return "channel"
    raise ValueError(f"Peer id invalid: {peer_id}")

Telegram's internal channel ids used to fit in 32 bits, so that bound held.
They no longer do. A channel created recently gets an id past 2**31, which in
`-100` form looks like `-1004435513595` — *below* pyrogram's floor, so the
range check falls through and every call touching that channel dies with

    ValueError: Peer id invalid: -1004435513595

before a single byte leaves the machine. Nothing is wrong with the id, the
token, or the permissions; the library simply refuses to name the chat.

This bites us in two places and looks like two unrelated faults:

  capture     the MTProto path for files over the Bot API's 50 MB upload
              ceiling raises, so oversize reels fail while small ones sail
              through — which reads as "big files are broken".
  processing  `Channel.messages()` raises inside its own try, logs a fetch
              failure, returns nothing, and `Source.ensure` reports
              "could not download the original from Telegram (message 38)" —
              which reads as "Telegram lost my video".

Widening `MIN_CHANNEL_ID` is the whole fix. The constants are only used for that
range test and for `get_channel_id`, which is arithmetic against
`MAX_CHANNEL_ID` and unaffected. So we move that one floor out to 2**40, which
covers every id Telegram is plausibly issuing this decade, and leave the
ceiling — and every other bound — exactly where it is.

**`MIN_CHAT_ID` must not be touched, and that is not a detail.** The chat branch
is tested *first*, and it is a bare `MIN_CHAT_ID <= peer_id` with no lower
bound. Widen it to cover large ids and every `-100…` channel id starts
satisfying it, so `get_peer_type` returns "chat" for a channel. That is worse
than the bug being fixed: instead of a loud `ValueError` naming the id, pyrogram
builds an `InputPeerChat` for something that is not a chat and the failure
arrives later, from the server, as an unrelated-looking error. Measured on the
real id: widening both floors turned `Peer id invalid: -1004435513595` into a
confident, wrong `"chat"`.

Basic groups do not need the widening anyway. Their ids are small negatives well
inside the existing 32-bit floor, and a group that outgrows it is migrated by
Telegram into a supergroup, which is a `-100…` channel and goes down the branch
this module actually fixes.

**The rule: never write `from pyrogram import Client`. Call `client()`.**

That is not a style preference, it is the entire fix, and the reason is that the
previous rule — "remember to call `patch()` before constructing a Client" — was
followed at three of the five sites that construct one and forgotten at the other
two. Forgotten quietly, because the constant is process-global: whether a feature
works depends on whether some *other* feature happened to open a session first in
the same process. Scan the channel and then restore, and restore works. Restore
into a fresh window and it dies on `Peer id invalid`, with a library message that
names the id and implicates the id, which is the one thing that is not wrong.
Measured, on the two that had no patch call:

    atlas/tgchannel.py   the MTProto reader behind playback and the channel walk
    db_restore.py        the manifest fallback, reached whenever the pinned
                         message is missing — which is every restore on a
                         channel nobody has pinned to

`client()` closes that off by construction rather than by discipline. It is the
only door to a `Client` in this repository, it patches before it builds, and a
grep for `from pyrogram import Client` is now a complete audit of whether anyone
has walked around it.

**The second rule: a channel id reaches pyrogram as an `int`. Call `peer()`.**

Widening the floor got the app past pyrogram's local rejection and into an
identical-looking one from the server — same class, same sentence, different
cause. `config.CHANNEL_ID` coerces to `int`; a channel id typed into the Capture
tab reached `Telegram.__init__` as `'-1004435513595'` and was stored as a string,
and pyrogram reads a numeric string as a **phone number**. Measured: the Bot API
read that exact channel — `getChat` returned the title, two members and a pinned
message — while MTProto refused it, because the two disagreed about what the
value *was*, not about what it said. `peer()` is where that ends, and
`Telegram.__init__` calls it so no consumer has to. Full story at `peer()`.

Three further things this module got wrong, and all three produce exactly the
symptom a missing call produces, which is why the bug looked intermittent.

**A failed import latched.** `patch()` set `_done = True` and *then* imported
pyrogram, inside a `try` that swallowed everything. So the first call that failed
for any reason recorded a permanent verdict, and every later call in that process
— including from a thread where the import would have succeeded — took the fast
path and widened nothing. This is the mechanism behind "it works sometimes":
nothing about the channel, the token or the id changes between the run that works
and the run that does not. A failed import no longer latches.

**And that import fails on a worker thread, which is where all of them happen.**
`pyrogram/__init__.py` imports `sync.py`, which calls `asyncio.get_event_loop()`
at module level. Since Python 3.10 that only auto-creates a loop on the main
thread; anywhere else it raises `RuntimeError: There is no current event loop in
thread 'atlas-mtproto'`. Every MTProto client in this application is built on a
worker thread. Worse, `RuntimeError` is not `ImportError`, so the `except
ImportError` wrapped around each of those five imports never caught it — the
thread just died. Measured: twelve threads calling `patch()` on a fresh process,
twelve RuntimeErrors, and `patch()` reporting "no pyrogram here" to all of them.
`_ensure_event_loop()` is the fix and it is why `client()` is worth having beyond
the patch: it makes the import possible, not just correct.

**`check()` cost 1.8 seconds and therefore had no callers.** Its docstring
claimed the readiness checks used it; nothing did, which is why a bad channel id
was reported by pyrogram three calls into a run instead of by name before it
started. It no longer imports pyrogram at all — the bounds are arithmetic and
this module already knows them — so a preflight can afford to ask.

`patch()` is idempotent, never raises, and is safe to call from a module that may
be imported on a machine with no pyrogram at all.
"""

from __future__ import annotations

import threading

# -100 followed by 2**40. Telegram channel ids crossed 2**31 in 2024; this
# leaves three orders of magnitude of headroom rather than moving the goalpost
# by one bit and having to do it again.
WIDE_MIN_CHANNEL_ID = -1001099511627776

# Pyrogram's own ceiling, restated so `check()` can answer without importing the
# library. Read from pyrogram when it happens to be loaded; this is the fallback
# and it is the value every published pyrogram has carried.
MAX_CHANNEL_ID = -1000000000000

_lock = threading.Lock()
_done = False


def _ensure_event_loop() -> None:
    """Give this thread an event loop, because importing pyrogram needs one.

    `pyrogram/__init__.py:40` imports `sync.py`, which at line 31 calls
    `asyncio.get_event_loop()` at *module* level to keep a `main_loop` for its
    synchronous convenience wrappers. In Python 3.10+ that only auto-creates a
    loop on the main thread; anywhere else it raises

        RuntimeError: There is no current event loop in thread 'atlas-mtproto'

    and `import pyrogram` fails. Every MTProto client in this application is
    built on a worker thread, so that is the common case, not the edge one — and
    it does not raise `ImportError`, so the `except ImportError` guarding each of
    those imports does not catch it. Measured: twelve threads calling `patch()`
    on a process where pyrogram was not yet loaded, twelve RuntimeErrors.

    The loop we set is never run. `sync.py` captures it for wrappers this
    application does not use — `atlas/tgchannel.py:_raw` exists precisely to
    reach past them to the real coroutines — and every caller sets its own loop
    immediately afterwards. It exists so that one import statement can finish.
    """
    import asyncio                                          # noqa: PLC0415
    try:
        asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def patch() -> str:
    """Widen pyrogram's channel id floor. Returns a one-line note, once.

    The note is worth logging: if a future pyrogram fixes this upstream the
    patch becomes a no-op, and the log is how you find out.

    **Once** is part of the contract, not an accident. Every caller does the
    same thing with the return value — `note = patch(); if note: log(note)` —
    and there are five of them plus the boot warm-up, all racing at start-up.
    Handing the same sentence to each printed it six times. So the first call
    that actually moves the constant gets the note and everyone after gets `""`,
    which makes "log it if it is non-empty" correct at every site without any
    site having to know whether it was first. Measured before the change:
    twelve threads, twelve copies of the note.

    Only `MIN_CHANNEL_ID` is moved. See the module docstring for why touching
    `MIN_CHAT_ID` turns a loud error into a silently wrong peer type.

    Three things about the bookkeeping, and each one was a way this function
    could report success while having done nothing:

    it holds a lock, because the `import pyrogram` inside takes about 1.8 seconds
    and a second thread arriving mid-import used to see the flag and leave;
    `_done` is set at the end rather than the start, for the same reason; and
    **a failed import does not latch.** That last one is the whole of the bug the
    user kept hitting. One early call from a thread with no event loop raised
    inside the `try`, set `_done = True` on the way out, and every later call in
    that process — including from the main thread, where the import would have
    worked — took the fast path and widened nothing. A transient, thread-local
    failure was recorded as a permanent verdict about the library.
    """
    global _done
    if _done:                    # fast path, and safe: `_done` is only ever
        return ""                # set once the constant is already moved
    with _lock:
        if _done:
            return ""
        try:
            _ensure_event_loop()
            from pyrogram import utils as _u  # noqa: PLC0415
        except Exception:
            # Retryable on purpose. "pyrogram is genuinely absent" and "this
            # thread could not import it just now" are indistinguishable here,
            # and only one of them is permanent.
            return ""

        changed = False
        if getattr(_u, "MIN_CHANNEL_ID", 0) > WIDE_MIN_CHANNEL_ID:
            _u.MIN_CHANNEL_ID = WIDE_MIN_CHANNEL_ID
            changed = True

        # Some pyrogram builds read the constant into `pyrogram.utils` only,
        # others also expose it on `pyrogram` itself. Keep both in step.
        try:
            import pyrogram as _p  # noqa: PLC0415
            if hasattr(_p, "MIN_CHANNEL_ID"):
                _p.MIN_CHANNEL_ID = _u.MIN_CHANNEL_ID
        except Exception:
            pass

        _done = True
        # Returned, not stored. Only one call in the life of a process can reach
        # this line — every later one short-circuits on `_done` above — so "hand
        # the note out once" needs no second flag to enforce it, and a flag that
        # looked like it enforced something would be the kind of state this
        # module already got wrong once.
        return ("widened pyrogram's channel id floor to -100+2**40 "
                "(modern Telegram channel ids sit past its 32-bit bound)"
                if changed else "")


def peer(value):
    """Whatever the config holds → what pyrogram wants. The other half of this.

    Widening the floor got the app past pyrogram's *local* rejection and straight
    into an identical-looking one from the server:

        PeerIdInvalid: [400 PEER_ID_INVALID] - The peer id being used is invalid
        or not known yet. Make sure you meet the peer before interacting with it

    Same words, different cause, and measured: the Bot API read that channel
    fine — `getChat` returned `type=channel`, a title, two members and a pinned
    message — while MTProto refused it. What differed was the *type*.
    `config.CHANNEL_ID` is coerced to `int` on the way out
    (`config._INT`), but a channel id typed into the Capture tab reaches
    `Telegram.__init__` as the string `'-1004435513595'` and was stored as one,
    and pyrogram's `resolve_peer` does this to a `str`:

        peer_id = re.sub(r"[@+\\s]", "", peer_id.lower())
        try:
            int(peer_id)
        except ValueError:
            ...resolve as a username...
        else:
            return await self.storage.get_peer_by_phone_number(peer_id)

    The substitution strips `@` and `+` but not `-`, so `'-1004435513595'`
    parses as an integer, and pyrogram concludes it is a **phone number** — looks
    it up in an empty session cache, and raises. The Bot API accepts either type
    because it is JSON over HTTPS, which is why this stayed invisible for as long
    as every large-file path was unreachable for the *other* reason.

    So: numeric strings become integers, and everything else is returned
    untouched — `@name`, `name`, and `t.me/name` are the username path, which is
    the one thing about `resolve_peer`'s string handling that is correct.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return value
    body = text[1:] if text.startswith("-") else text
    # `-` is the only sign accepted, and `+` deliberately is not: a leading `+`
    # is how a *phone number* is written, it is not how anyone writes a channel
    # id, and converting it would take pyrogram's phone-number path — the one
    # correct use of a numeric string — and turn it into a positive user id.
    #
    # `isdigit`, not `try: int()`. `int()` accepts "1_0", "٣", and surrounding
    # whitespace; a channel id is ASCII digits with an optional minus, and a
    # normaliser looser than the thing it feeds is a second bug waiting.
    if body.isdigit() and body.isascii():
        return int(text)
    return text


def is_transport_error(exc: BaseException) -> bool:
    """Is this "the socket died" rather than "Telegram said no"?

    The third pyrogram fact this module has to own, and the one that cost the
    most. It lives here rather than beside either client because there are two
    independent MTProto clients in this application — `atlas/tgchannel.py` for
    reading and playback, `capture/mtproto.py` for the backfill sweep — and both
    need the same answer to the same question. A predicate copied into both is a
    predicate that will be improved in one.

    The distinction decides whether reconnecting can possibly help, and getting
    it wrong is expensive in both directions: treat a real refusal as a dead
    socket and the app rebuilds the session in a tight loop against a channel it
    will never be allowed to read; treat a dead socket as a refusal and you get
    what was measured on 30 August — `OSError: [WinError 10053] An established
    connection was aborted by the software in your host machine` every five
    seconds for fifty-six minutes, six reels stranded, and a status endpoint
    cheerfully reporting `running: true` the whole time.

    `OSError` covers the entire Winsock family (10053 aborted, 10054 reset by
    peer, 10060 timed out) and Linux's ECONNRESET/EPIPE, because Python maps all
    of them onto it; `ConnectionError` and `TimeoutError` are subclasses already
    and are named for the reader, not for the check.

    The name and string tests are for pyrogram's own vocabulary, which raises a
    plain `ConnectionError("Client is already terminated")` from its session
    layer with no distinguishable class, and for the auth-key errors that mean
    the *session file* is finished rather than the socket —
    `AuthKeyUnregistered` after a token rotation, `SessionRevoked` after a
    sign-out elsewhere. Those are recoverable in exactly the same way, by
    building a new session, which is why they belong on this side of the line.

    `asyncio.IncompleteReadError` is the shape a connection takes when the peer
    closes mid-frame — the most common outcome of a session left idle for hours,
    which is precisely the eight-hour gap this was reported from.
    """
    import asyncio                                          # noqa: PLC0415
    if isinstance(exc, (OSError, ConnectionError, TimeoutError,
                        asyncio.IncompleteReadError, asyncio.TimeoutError)):
        return True
    if type(exc).__name__ in (
            "AuthKeyUnregistered", "AuthKeyDuplicated", "AuthKeyInvalid",
            "SessionRevoked", "SessionExpired", "Unauthorized",
            "ServerError", "ServiceUnavailable", "InternalServerError"):
        return True
    text = str(exc).lower()
    return any(s in text for s in (
        "already terminated", "not connected", "connection", "socket",
        "closed", "reset by peer", "timed out", "broken pipe"))


def client(*args, **kwargs):
    """A patched `pyrogram.Client`. **The only way to build one in this repo.**

    Every argument goes straight through, so this reads as `Client(...)` at the
    call site and there is nothing to remember beyond the name. What it adds is
    the two lines that cannot be left out: this thread gets an event loop so the
    import can finish at all, and the channel id floor is widened before the
    object that will resolve a peer id exists.

    Raises `ImportError` if pyrogram is absent, which is what `from pyrogram
    import Client` did — every caller already handles it, and turning it into
    something else would break their "no transport installed" messages. It will
    not raise `RuntimeError` about a missing event loop, which that line *did* on
    every worker thread, and which no caller handles.
    """
    patch()
    _ensure_event_loop()         # cheap, idempotent, and the reason this works
    from pyrogram import Client  # noqa: PLC0415
    return Client(*args, **kwargs)


def warm() -> str:
    """Do the 1.8-second pyrogram import now, off the critical path.

    Called from a daemon thread at boot. Three reasons, and the first is not the
    correctness of the patch — `client()` owns that:

    the first Telegram action of a session pays for `pyrogram.raw.types` no
    matter who triggers it, and paying it during boot rather than during a
    restore is the difference between a progress line that sits still for two
    seconds and one that does not; if a future call site does bypass `client()`,
    the constant is already moved by the time anyone can press a button; and the
    log line it returns is the only place the patch announces itself, which is
    how you would find out that a future pyrogram had fixed this upstream.
    """
    return patch()


def check(channel) -> str:
    """Why this channel id will not work, or "" if it will.

    Called from the readiness checks so a bad id is named before a run starts
    rather than three hours in, and so a *genuinely* malformed id — a username
    typed where a numeric id belongs, a group id that was never migrated, a
    positive number pasted from the wrong field — is still reported after the
    patch has stopped the false positives.

    Deliberately arithmetic, and deliberately not `pyrogram.utils.get_peer_type`.
    Asking pyrogram means importing pyrogram, which is 1.8 seconds; a preflight
    that costs 1.8 seconds does not get called, and the previous version of this
    function was never called by anything. The bounds are three constants and one
    comparison, both of which this module already has to know to do its job.

    It answers for the *patched* library, because `client()` guarantees the patch
    ran. If pyrogram is already loaded its live ceiling is used rather than the
    restated one, so an upstream that moves the bound is followed rather than
    contradicted.
    """
    text = str(channel or "").strip()
    if not text:
        return "no channel id"
    if text.startswith("@"):
        return ""              # a username; pyrogram resolves those by lookup
    try:
        peer = int(text)
    except ValueError:
        return (f"{text[:60]!r} is not a channel id. It looks like -100 "
                f"followed by digits, or an @username.")

    ceiling = MAX_CHANNEL_ID
    import sys                                             # noqa: PLC0415
    loaded = sys.modules.get("pyrogram.utils")
    if loaded is not None:
        ceiling = getattr(loaded, "MAX_CHANNEL_ID", ceiling)

    if peer > 0:
        return ""              # a user or bot id; not this module's business
    if WIDE_MIN_CHANNEL_ID <= peer < ceiling:
        return ""              # a channel or supergroup — the normal case
    if peer >= -2147483647:
        return (f"{peer} is a basic-group id, not a channel id. Telegram gives "
                f"a channel an id of the form -100 followed by digits. If this "
                f"group has grown into a supergroup its id will have changed; "
                f"read the new one from the channel itself.")
    if peer < WIDE_MIN_CHANNEL_ID:
        return (f"{peer} is below every channel id Telegram issues (-100 "
                f"followed by up to 2**40). Check for a missing digit or an "
                f"extra one.")
    return (f"{peer} is not in the range Telegram uses for channels "
            f"({WIDE_MIN_CHANNEL_ID} to {ceiling}).")
