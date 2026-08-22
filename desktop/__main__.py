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
    icon = os.path.join(_ROOT, "desktop", "vios.png")
    if os.path.isfile(icon):
        kwargs["icon"] = icon           # Phase 6 draws it; absent is fine now.
    os.makedirs(kwargs["storage_path"], exist_ok=True)

    webview.start(**kwargs)
    log("window closed", SUB)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
