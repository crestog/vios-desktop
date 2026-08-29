"""
desktop/make_shortcut.py — put VIOS on the Desktop.

`python -m desktop.make_shortcut` writes `VIOS.lnk` to the real Desktop folder,
pointing at this checkout. Run it again after moving or renaming the folder; a
shortcut stores an absolute path and will not follow.

Three decisions worth stating, because each one is a failure mode avoided.

**`pythonw.exe`, not `python.exe`.** A shortcut to `python.exe` opens a console
window that sits behind the app for its whole life and reappears in the taskbar as
a second entry. `pythonw` has no console, which is why `__main__._run` redirects
`stdout`/`stderr` into `logs/console.log` and shows a dialog if the window never
opens — an icon that silently does nothing is worse than an ugly console.

**The Desktop path comes from the shell, not from `~/Desktop`.** OneDrive moves it
to `%USERPROFILE%\\OneDrive\\Desktop` and leaves the old folder in place, so
guessing writes a shortcut to a directory the user is not looking at.
`[Environment]::GetFolderPath('Desktop')` asks the shell where it actually is.

**The `.lnk` is written by the shell too.** The format is a documented binary
structure and also a nest of `LinkTargetIDList` shell-item encodings; `WScript.
Shell.CreateShortcut` is the supported writer and it is already installed. This
module only feeds it, through the environment rather than through string
interpolation, so a path containing a quote or an apostrophe cannot become
PowerShell syntax.
"""

from __future__ import annotations

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

NAME = "VIOS.lnk"
# Plain ASCII on purpose: this string is handed to PowerShell through the
# environment, and the console code page is cp1252 — an em dash survives the trip
# as a question mark on some machines, in a tooltip nobody can edit afterwards.
DESCRIPTION = "VIOS - your local reel archive, searchable and playable offline"

# Builds the shortcut from four environment variables, so nothing here is
# interpolated into the script text. Written to fail loudly: `-Stop` turns a COM
# error into a non-zero exit instead of a warning and a shortcut that is not there.
_PS = r"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not $desktop) { throw 'the shell did not report a Desktop folder' }
$link = Join-Path $desktop $env:VIOS_LNK_NAME
$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($link)
$sc.TargetPath       = $env:VIOS_LNK_TARGET
$sc.Arguments        = '-m desktop'
$sc.WorkingDirectory = $env:VIOS_LNK_CWD
$sc.WindowStyle      = 1
$sc.Description      = $env:VIOS_LNK_DESC
if (Test-Path $env:VIOS_LNK_ICON) { $sc.IconLocation = $env:VIOS_LNK_ICON + ',0' }
$sc.Save()
if (-not (Test-Path $link)) { throw "Save() reported success but $link is absent" }
Write-Output $link
"""


def launcher() -> str:
    """`pythonw.exe` beside the running interpreter, or the interpreter itself.

    Falling back rather than failing: a console window is a blemish, and a
    shortcut that does not exist is a broken feature. Some embedded and
    Store-packaged Pythons ship no `pythonw`.
    """
    exe = os.path.abspath(sys.executable)
    folder, name = os.path.split(exe)
    if name.lower().startswith("python") and "w" not in name.lower()[:8]:
        candidate = os.path.join(folder, name.replace("python", "pythonw", 1))
        if os.path.isfile(candidate):
            return candidate
    return exe


def create() -> str:
    """Write the shortcut and return where it landed."""
    target = launcher()
    env = dict(os.environ)
    env.update({
        "VIOS_LNK_NAME": NAME,
        "VIOS_LNK_TARGET": target,
        "VIOS_LNK_CWD": _ROOT,
        "VIOS_LNK_ICON": os.path.join(_HERE, "vios.ico"),
        "VIOS_LNK_DESC": DESCRIPTION,
    })
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-Command", _PS],
        capture_output=True, text=True, env=env, timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"could not write the shortcut — {detail[:600]}")
    return (proc.stdout or "").strip().splitlines()[-1].strip()


def main() -> int:
    if os.name != "nt":
        print("This writes a Windows .lnk; there is nothing to do here.")
        return 1
    icon = os.path.join(_HERE, "vios.ico")
    if not os.path.isfile(icon):
        print("no desktop/vios.ico yet — drawing it first")
        from desktop import make_icon
        make_icon.main()
    try:
        where = create()
    except Exception as e:                              # noqa: BLE001
        print(f"failed: {e}")
        return 1
    print(f"shortcut : {where}")
    print(f"runs     : {launcher()} -m desktop")
    print(f"in       : {_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
