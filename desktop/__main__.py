"""
desktop — the window. `python -m desktop`, or VIOS.bat.

Three things happen here and the order is the whole file:

  1. **Credentials into the environment, first.** Every module in this
     application reads `os.environ` and has no fallback literal, because a
     default value once put a live bot token in a public repository. Upstream
     that produced a session with all four secrets stored correctly which still
     printed "Telegram disabled" — nothing had asked. So this runs before a
     single module that reads a credential is imported.
  2. **uvicorn in a daemon thread**, on a port confirmed free.
  3. **The window**, opened only once the server answers.

Step 3 waits on purpose. WebView2 renders a browser error page for a refused
connection, and that page is not replaced when the port comes up a moment
later — so racing the server costs a blank window and a relaunch. Waiting for
one successful request costs a few hundred milliseconds and cannot fail that way.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser

# The repo root on sys.path, so `python -m desktop` works from anywhere and
# VIOS.bat does not need to care what the working directory is.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import creds                                        # noqa: E402
import paths                                         # noqa: E402
from logger import vios_log as log                   # noqa: E402

SUB = "UI"
TITLE = "VIOS"


def _free_port(preferred: int) -> int:
    """`preferred` if it is free, otherwise whatever the OS hands out.

    Two copies of the app must not fight over one port. The preferred port is
    tried first anyway so that `npm run dev`'s proxy target and any bookmarked
    `/api/docs` stay predictable in the normal single-instance case.
    """
    for port in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("no free port on the loopback interface")


def _serve(port: int):
    """uvicorn on the loopback interface only, in a daemon thread.

    127.0.0.1 rather than 0.0.0.0 because **there is no authentication in this
    application**. It is one user on one machine; binding it to a reachable
    address would publish the archive, the credential-status endpoints and the
    capture controls to the network.
    """
    import uvicorn
    from server.app import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", access_log=False,
                            timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, name="vios-http", daemon=True).start()
    return server


def _wait_until_up(port: int, timeout: float = 25.0) -> bool:
    """Block until the port accepts a connection, or give up and say so."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


def folder_dialog(window) -> str:
    """The native folder picker — the reason this shell is pywebview.

    The local video library needs the user to point at real directories, and no
    web page can open a directory chooser that returns a filesystem path. This
    is the whole justification for a desktop shell over a browser tab, so it
    lives here as a first-class function rather than inline in a route.

    `FOLDER_DIALOG` was renamed to `FileDialog.FOLDER` in pywebview 6 and the
    old name warns on every access, so prefer the new one and keep the old as
    the fallback.
    """
    import webview
    kind = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
    if kind is None:
        kind = webview.FOLDER_DIALOG
    picked = window.create_file_dialog(kind, allow_multiple=False)
    if not picked:
        return ""
    return picked[0] if isinstance(picked, (list, tuple)) else str(picked)


class Bridge:
    """What the frontend can call that a browser could not.

    Exposed as `window.pywebview.api.*`. Deliberately tiny: everything that is
    plain data goes over HTTP like the rest of the app, and only the genuinely
    native capabilities live here. A wide bridge would be a second API surface
    with no OpenAPI schema and no generated types.

    **The underscore on `_window` is load-bearing.** pywebview builds the JS
    bridge by walking this object with `dir()` and recursing into every
    non-callable attribute that has a `__module__` (`webview/util.py:180`). A
    plain `self.window` therefore hands it the pywebview `Window`, whose
    `.native` is the WinForms control, and the walk descends into the entire .NET
    object graph. Measured: hundreds of lines of
    `Error while processing window.native.AccessibilityObject.Bounds.Empty.Empty…
    maximum recursion depth exceeded`, plus `CoreWebView2 can only be accessed
    from the UI thread` — the walker reading COM properties off the UI thread
    during window init. Names starting with `_` are skipped, which stops all of
    it at the root.
    """

    def __init__(self):
        self._window = None

    def pick_folder(self) -> str:
        return folder_dialog(self._window) if self._window else ""

    def open_home(self) -> str:
        """Reveal the application's data folder in Explorer."""
        try:
            os.startfile(paths.HOME)                    # noqa: S606
        except Exception as e:                          # noqa: BLE001
            log(f"could not open {paths.HOME} — {type(e).__name__}: {e}",
                SUB, "WARN")
        return paths.HOME

    def open_path(self, path: str) -> str:
        """Reveal one watched folder or one local video in Explorer.

        The Library view lists local videos by their real path, and "show me
        this file" is the obvious next thing to want. Only an existing path is
        opened — `startfile` on a string that is not a path will happily hand
        it to the shell's URL handler, and a value that arrived over the bridge
        is not something to pass to a shell resolver unchecked.
        """
        target = os.path.abspath(str(path or ""))
        if not os.path.exists(target):
            log(f"open_path: nothing at {target}", SUB, "WARN")
            return ""
        try:
            os.startfile(target)                        # noqa: S606
        except Exception as e:                          # noqa: BLE001
            log(f"could not open {target} — {type(e).__name__}: {e}", SUB, "WARN")
            return ""
        return target

    def open_url(self, url: str) -> str:
        """Open one reel's permalink in the real browser.

        The Capture tab lists ledger rows by their Instagram URL, and "show me
        the post that failed" is the first thing anyone wants when a fetch keeps
        erroring. A plain `<a href>` cannot do it: inside a webview an external
        link navigates *the application* to Instagram, and there is no back
        button — the window is the app.

        Two guards, for the same reason `open_path` refuses a non-path. The
        scheme must be `https`, so a `file:` or `javascript:` string that reached
        the bridge cannot be handed to the shell; and the host must be one this
        app has business opening, so a compromised or mistyped ledger row cannot
        turn a click here into a visit to anywhere at all. The allowlist is the
        two hosts the ledger's own canonicaliser produces.
        """
        from urllib.parse import urlparse
        raw = str(url or "").strip()
        try:
            parts = urlparse(raw)
        except ValueError:
            return ""
        host = (parts.hostname or "").lower()
        allowed = host in ("instagram.com", "www.instagram.com")
        if parts.scheme != "https" or not allowed:
            log(f"open_url: refusing {raw[:120]}", SUB, "WARN")
            return ""
        try:
            webbrowser.open(raw, new=2)
        except Exception as e:                          # noqa: BLE001
            log(f"could not open {raw[:120]} — {type(e).__name__}: {e}",
                SUB, "WARN")
            return ""
        return raw


def main() -> int:
    import webview

    # ── 1. credentials, before anything reads one ──
    exported = creds.export_to_env()
    if exported:
        # Names only, never values — this line goes to a log file.
        log(f"credentials in environment: {', '.join(sorted(exported))}", SUB)

    from atlas import config as atlas_config
    missing = atlas_config.missing_secrets()
    if missing:
        log(f"Telegram is not configured yet — set {', '.join(missing)} in "
            f"Admin. The app opens regardless and reads whatever is local.",
            SUB, "WARN")

    # ── 2. the server ──
    port = _free_port(atlas_config.PORT)
    _serve(port)
    url = f"http://127.0.0.1:{port}/"
    if not _wait_until_up(port):
        log(f"server did not come up on {port} within 25s — not opening a "
            f"window on a refused connection", SUB, "ERROR")
        return 1
    log(f"serving {url}", SUB)

    # ── 3. the window ──
    bridge = Bridge()
    window = webview.create_window(
        TITLE, url, js_api=bridge, width=1600, height=1000,
        min_size=(960, 640), text_select=True, zoomable=True,
    )
    bridge._window = window

    log(f"window open — home is {paths.HOME}", SUB)

    # `private_mode` and `storage_path` belong to start(), not create_window() —
    # they configure the whole webview process, not one window. Measured against
    # pywebview 6.2.1: create_window() accepts neither and raises TypeError.
    #
    # private_mode defaults to True, which discards localStorage on exit. Every
    # piece of remembered UI state — density, view mode, saved searches, the last
    # query — lives there, so the default would quietly reset the app on every
    # launch.
    kwargs = {
        "private_mode": False,
        "storage_path": os.path.join(paths.SESSION_DIR, "webview"),
        # debug=True gives the WebView2 devtools on right-click. On by default:
        # this is a single-user application on the machine that builds it, and
        # reading a console error without relaunching is worth more than a tidy
        # context menu.
        "debug": os.environ.get("VIOS_DEBUG", "1") != "0",
    }
    # On Windows the icon must be an `.ico`, and that is not a preference — it is
    # the only format that works. The WinForms backend does `self.Icon =
    # Icon(path)` (`webview/platforms/winforms.py:244`) and `System.Drawing.Icon`
    # reads only the ICO container, so a PNG raises `ArgumentException: Argument
    # 'picture' must be a picture that can be used as a Icon` *on the .NET UI
    # thread*. Nothing in Python catches that: the process dies inside
    # `webview.start` without unwinding, so `_run()`'s handler never fires, no
    # dialog appears, and the log simply stops after "Using WinForms / Chromium".
    # Measured — the app served 109 routes, opened a window, and vanished with no
    # traceback. So a PNG is not a fallback here; it is a crash, and listing it as
    # one would leave a landmine for whoever deletes the `.ico`. The GTK and Cocoa
    # backends do want the PNG, hence the two orders.
    for name in ("vios.ico",) if os.name == "nt" else ("vios.png", "vios.ico"):
        candidate = os.path.join(_ROOT, "desktop", name)
        if os.path.isfile(candidate):
            kwargs["icon"] = candidate
            break
    os.makedirs(kwargs["storage_path"], exist_ok=True)

    webview.start(**kwargs)
    log("window closed", SUB)
    return 0


# ══════════════════════════════════════════════════════════════════════════
# LAUNCHED FROM AN ICON
# ══════════════════════════════════════════════════════════════════════════
# The desktop shortcut runs `pythonw.exe -m desktop`, which is the only way to
# open a window without a console flashing behind it. `pythonw` costs two things
# that a console gave for free, and both are handled here rather than accepted:
# there is nowhere for output to go, and there is nowhere for a crash to be seen.
STREAM_LOG = "console.log"


def _redirect_streams_to_log() -> str:
    """Point `stdout`/`stderr` at a file when there is no console, and say where.

    Under `pythonw` both are `None`, so every `print` in this application raises
    `AttributeError` — `logger._safe_print` swallows it and the file log survives,
    but uvicorn's startup lines and any traceback outside a `try` are simply gone.
    Sending them to a file is strictly better than the console they replace: it is
    still there tomorrow, and the Admin tab can read it.

    Returns "" when a console is present, which is also the signal that a failure
    can just be printed instead of shown in a dialog.
    """
    if sys.stdout is not None and sys.stderr is not None:
        return ""
    target = os.path.join(paths.LOG_DIR, STREAM_LOG)
    try:
        os.makedirs(paths.LOG_DIR, exist_ok=True)
        # Line-buffered: a crash must not lose the lines that explain it.
        handle = open(target, "a", encoding="utf-8", errors="replace",
                      buffering=1)
    except OSError:
        # Better a launch with no output than no launch. `logger` writes its own
        # file through its own handle and does not depend on this.
        return ""
    handle.write(f"\n{'=' * 70}\n{time.strftime('%Y-%m-%d %H:%M:%S')}  "
                 f"launched without a console\n{'=' * 70}\n")
    sys.stdout = sys.stderr = handle
    return target


def _message_box(title: str, text: str) -> None:
    """A native error dialog. The only way an icon launch can report anything."""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)  # MB_ICONERROR
    except Exception:                                   # noqa: BLE001
        pass


def _run() -> int:
    """`main()`, with somewhere for its output to go and a way to fail visibly.

    A shortcut that does nothing when double-clicked is indistinguishable from a
    broken shortcut, so every path out of here that is not a working window says
    so on screen — but only when there is no console, because a terminal already
    shows a traceback better than a dialog can.
    """
    stream_log = _redirect_streams_to_log()
    try:
        code = main()
    except Exception as e:                              # noqa: BLE001
        import traceback
        traceback.print_exc()
        log(f"launch failed — {type(e).__name__}: {e}", SUB, "ERROR")
        if stream_log:
            _message_box(
                f"{TITLE} could not start",
                f"{type(e).__name__}: {e}\n\n"
                f"The full traceback is in:\n{stream_log}\n\n"
                f"The application log is in:\n{paths.LOG_DIR}")
        return 1
    if code and stream_log:
        _message_box(
            f"{TITLE} could not start",
            f"The window did not open (exit code {code}).\n\n"
            f"Output from this launch is in:\n{stream_log}\n\n"
            f"The application log is in:\n{paths.LOG_DIR}")
    return code


if __name__ == "__main__":
    raise SystemExit(_run())
