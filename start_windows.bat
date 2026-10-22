@echo off
REM Double-click this file to run a separate, LOCAL-ONLY copy of
REM TEAMworks360 on just this computer - its own database, not
REM connected to anyone else. If TEAM has a shared host set up, use
REM open_shared_app.bat instead so you see (and add to) the real shared data.
setlocal enabledelayedexpansion
cd /d "%~dp0"

where python >nul 2>nul
if %errorlevel%==0 (
    set PY=python
) else (
    where py >nul 2>nul
    if %errorlevel%==0 (
        set PY=py
    ) else (
        echo Python was not found. Install Python 3 from https://www.python.org/downloads/
        echo and make sure to tick "Add Python to PATH" during setup, then try again.
        pause
        exit /b 1
    )
)

REM Skip the dependency check entirely if it's already satisfied - makes
REM every run after the first one noticeably faster. IMPORTANT: this list
REM must include every package requirements.txt adds, or a newly-added
REM dependency silently won't get installed on an existing setup - keep it
REM in sync whenever requirements.txt changes.
%PY% -c "import flask, reportlab, PIL, openpyxl" >nul 2>nul
if not %errorlevel%==0 (
    echo Installing dependencies ^(first run only^)...
    %PY% -m pip install -r requirements.txt
)

REM The database lives in a stable location outside this folder (see
REM db_location.py) so that replacing this whole folder to install an
REM update never touches it. This delegates the "does it exist yet" check
REM to Python so it's never out of sync with what app.py/seed.py actually
REM use - and it also runs the one-time move of a database from an older
REM version of this app that still had it sitting right here next to the code.
%PY% -c "import db_location, os, sys; db_location.migrate_from_old_product_name(); db_location.migrate_legacy_files(os.getcwd()); sys.exit(0 if os.path.exists(db_location.DB_PATH) else 1)" >nul 2>nul
if not %errorlevel%==0 (
    echo.
    echo ======================================================
    echo   No database found on this computer
    echo ======================================================
    echo.
    echo TEAM normally runs ONE SHARED copy of this app from a single host
    echo computer, with everyone else just opening a browser to it - if
    echo that's already set up, use "Open TEAMworks360"
    echo ^(open_shared_app.bat^) instead of this file.
    echo.
    echo Continuing here creates a brand new, SEPARATE copy of the app
    echo with its own database on THIS computer only. Anything entered
    echo here - customers, suppliers, jobs, users, everything - stays on
    echo this computer and will NOT be seen by anyone else.
    echo.
    set /p CONFIRM="Type YES to continue creating a new local-only copy: "
    if /i not "!CONFIRM!"=="YES" (
        echo Cancelled - nothing was created.
        pause
        exit /b 0
    )
    echo Setting up a new local-only copy...
    %PY% seed.py
    %PY% import_team_order_book.py
)

REM Create a desktop shortcut with the app's own icon on first run, so
REM there's a proper clickable icon to start the app with from then on.
REM Done via a separate .ps1 file, which prints its own status directly -
REM simpler and more robust than the batch file trying to capture and
REM parse PowerShell's output, which is its own source of fragility.
where powershell >nul 2>nul
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" -TargetScript "start_windows.bat" -ShortcutName "TEAMworks360 (Local Copy)"
)

echo Starting TEAMworks360 - your browser will open automatically...
%PY% app.py
pause
