@echo off
REM VIOS — open the application.
REM
REM Double-click this, or run it from a terminal. `cd /d "%~dp0"` makes the repo
REM root the working directory regardless of where the shortcut lives, so a
REM Start Menu or taskbar shortcut behaves the same as a double-click here.
cd /d "%~dp0"
python -m desktop
REM Only pause on failure. A clean exit closing the window is the normal case and
REM should not leave a console sitting there waiting for a keypress; a crash
REM should never vanish before its traceback can be read.
if errorlevel 1 (
    echo.
    echo VIOS exited with code %errorlevel%.
    echo The full log is in vios_home\logs ^(or %%VIOS_LOCAL_HOME%%\logs^).
    pause
)
