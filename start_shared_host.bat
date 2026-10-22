@echo off
REM Double-click this file ONLY on the one computer that should be the
REM shared host - this machine becomes the server, holding the one real
REM database everyone shares, and everyone else just opens a browser to
REM the address this prints. They don't need Python, this folder, or their
REM own copy of the app - they should use open_shared_app.bat instead.
REM
REM Use start_windows.bat instead of this if you specifically want a
REM separate, local-only copy for yourself (e.g. testing), with nobody
REM else connecting to it and nothing shared with the real data.
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
REM in sync whenever requirements.txt changes. waitress is the one this
REM launcher needs beyond what the single-user launcher checks for.
%PY% -c "import flask, reportlab, PIL, openpyxl, waitress" >nul 2>nul
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
    echo No database found - setting up for the first time...
    %PY% seed.py
    %PY% import_team_order_book.py
)

REM Add a Windows Firewall rule for this port, so other computers on the
REM network can actually reach this machine. Without this, the single most
REM common symptom of shared hosting is exactly what it looks like from
REM here: the app is genuinely listening and the host's own browser works
REM fine, but Windows Firewall silently drops the connection from anyone
REM else, with no error message on either end. Removes any existing rule
REM with this exact name first, so re-running this never piles up
REM duplicates. Best-effort: this usually needs Administrator rights to
REM succeed, but the app must still start normally either way - a failed
REM firewall rule is never a reason to stop.
if not defined FLASK_PORT set FLASK_PORT=5000
REM Also remove any stale rule left over from before this app was renamed
REM from Job Profitability Manager, so an existing install doesn't end up
REM with two overlapping firewall rules for the same port.
netsh advfirewall firewall delete rule name="Job Profitability Manager" >nul 2>nul
netsh advfirewall firewall delete rule name="TEAMworks360" >nul 2>nul
netsh advfirewall firewall add rule name="TEAMworks360" dir=in action=allow protocol=TCP localport=%FLASK_PORT% >nul 2>nul
if %errorlevel%==0 (
    echo Windows Firewall rule added for port %FLASK_PORT% - other computers on your network should be able to connect.
) else (
    echo Could not add a Windows Firewall rule automatically - this usually needs Administrator rights.
    echo If colleagues can't connect, right-click this file and choose "Run as administrator" once,
    echo or add an inbound rule for TCP port %FLASK_PORT% yourself in Windows Defender Firewall settings.
)

REM Create a desktop shortcut with the app's own icon on first run, so
REM there's a proper clickable icon to start hosting with from then on -
REM named distinctly from the everyday "TEAMworks360" icon
REM so this machine's operator never confuses the two. See create_shortcut.ps1.
where powershell >nul 2>nul
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" -TargetScript "start_shared_host.bat" -ShortcutName "TEAMworks360 (Host)"
)

%PY% serve_shared.py
pause
