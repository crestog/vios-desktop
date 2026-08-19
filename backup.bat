@echo off
REM backup — the only thing between a disk failure and losing this application.
REM
REM There is no git remote, by decision, and C: is the only drive on this
REM machine. A `git bundle` is a single file holding every commit on every branch,
REM and it clones like a real repository:
REM
REM     git clone backups\vios-2026-08-19.bundle VIOS-Desktop
REM
REM So this is a complete backup of the code and its whole history, not a copy of
REM the working tree. **Copy the file off this machine** — to a USB stick, another
REM drive, anywhere. A backup that lives on the disk it protects is not a backup.
REM
REM What it deliberately does NOT contain: vios_home\ — the database, the mirrored
REM videos, the proxies, the pyrogram session. Those are gitignored, and they are
REM reconstructible from the Telegram channel, which is the permanent store. The
REM code is not reconstructible, which is why only the code is backed up here.
cd /d "%~dp0"

if not exist backups mkdir backups

REM Sortable, ISO-ordered, locale-independent. %DATE% is formatted by Windows
REM regional settings, so parsing it produces a different filename on a different
REM machine — WMIC always answers yyyymmddHHMMSS.
for /f %%d in ('wmic os get LocalDateTime ^| findstr /r "^[0-9]"') do set STAMP=%%d
set STAMP=%STAMP:~0,4%-%STAMP:~4,2%-%STAMP:~6,2%-%STAMP:~8,4%

git bundle create "backups\vios-%STAMP%.bundle" --all
if errorlevel 1 (
    echo.
    echo Bundle failed. If it says "Refusing to create empty bundle", there are
    echo no commits yet — commit something first.
    pause
    exit /b 1
)

echo.
echo Wrote backups\vios-%STAMP%.bundle
echo Verifying it can actually be read back...
git bundle verify "backups\vios-%STAMP%.bundle"
if errorlevel 1 (
    echo.
    echo VERIFY FAILED — do not trust this file.
    pause
    exit /b 1
)
echo.
echo Now copy that file somewhere that is not this disk.
pause
