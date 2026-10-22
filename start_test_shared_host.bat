@echo off
REM Starts a separate, ISOLATED TEST COPY of the app - for trying things out
REM or letting a few colleagues poke at a sandbox without any risk to the
REM real data. See README.md, "Test copy (sandbox)".
REM
REM This uses its own data folder (TEAMworks360-Test, not TEAMworks360) and
REM its own port (5050, not 5000), so it can run side-by-side with the real
REM start_shared_host.bat on the same machine without the two ever colliding
REM or mixing data.
REM
REM The very first time this runs, it seeds the test folder with a one-time
REM copy of real data from test_seed_data\jpm.db (see that folder's
REM README.txt for exactly what that means and how to refresh it later).
REM After that first copy, this test copy lives completely on its own:
REM nothing done here ever reaches the real database, and nothing added to
REM the real database afterwards will ever appear here.
REM
REM Use start_shared_host.bat instead of this if you want to host the real,
REM live system that everyone's actual work goes into.
cd /d "%~dp0"

set TEAMWORKS360_DATA_DIR=%USERPROFILE%\TEAMworks360-Test

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
REM in sync whenever requirements.txt changes (same list start_shared_host.bat
REM checks, since this also runs via serve_shared.py / waitress).
%PY% -c "import flask, reportlab, PIL, openpyxl, waitress" >nul 2>nul
if not %errorlevel%==0 (
    echo Installing dependencies ^(first run only^)...
    %PY% -m pip install -r requirements.txt
)

REM Delegate "does the test copy already have its own database" to Python,
REM same as the real launchers do, so this is never out of sync with what
REM app.py/db_location.py actually use. Deliberately does NOT call
REM migrate_from_old_product_name() or migrate_legacy_files() - those are
REM one-time moves for the REAL data folder only and have nothing to do
REM with this separate test folder.
%PY% -c "import db_location, os, sys; db_location.ensure_data_dir(); sys.exit(0 if os.path.exists(db_location.DB_PATH) else 1)" >nul 2>nul
if not %errorlevel%==0 (
    if exist "test_seed_data\jpm.db" (
        echo Setting up the test copy for the first time - seeding it from today's snapshot of the real data...
        %PY% -c "import db_location, shutil; db_location.ensure_data_dir(); shutil.copy2('test_seed_data/jpm.db', db_location.DB_PATH); db_location.harden_file_permissions()"
    ) else (
        echo No seed database found at test_seed_data\jpm.db - starting with a brand new, empty test copy instead.
        %PY% seed.py
    )
)

REM A different default port from the real shared host (5000), so the two
REM can run at the same time on the same machine without clashing.
if not defined FLASK_PORT set FLASK_PORT=5050

REM Its own, separately-named Windows Firewall rule, so switching this on or
REM off (or re-running it) never touches the real host's firewall rule and
REM the two never overlap or get confused for one another.
netsh advfirewall firewall delete rule name="TEAMworks360 Test" >nul 2>nul
netsh advfirewall firewall add rule name="TEAMworks360 Test" dir=in action=allow protocol=TCP localport=%FLASK_PORT% >nul 2>nul
if %errorlevel%==0 (
    echo Windows Firewall rule added for port %FLASK_PORT% - other computers on your network should be able to connect.
) else (
    echo Could not add a Windows Firewall rule automatically - this usually needs Administrator rights.
    echo If colleagues can't connect, right-click this file and choose "Run as administrator" once,
    echo or add an inbound rule for TCP port %FLASK_PORT% yourself in Windows Defender Firewall settings.
)

REM A distinctly-named desktop shortcut, so it's never confused with the
REM real "TEAMworks360 (Host)" icon. See create_shortcut.ps1.
where powershell >nul 2>nul
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" -TargetScript "start_test_shared_host.bat" -ShortcutName "TEAMworks360 (Test Host)"
)

%PY% serve_shared.py
pause
