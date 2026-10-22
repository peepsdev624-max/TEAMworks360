@echo off
REM Double-click to try TEAMworks360 on THIS computer only, with made-up demo
REM data - no real TEAM information is in this copy at all. Everything you
REM enter stays on this computer, in its own separate folder
REM (TEAMworks360-Demo), and nothing is shared with anyone else.
REM
REM Sign in with:   tester  /  TestDrive2026
REM You'll be asked to set up two-factor authentication the first time -
REM have an authenticator app (Microsoft or Google Authenticator) on your phone.
cd /d "%~dp0"

set TEAMWORKS360_DATA_DIR=%USERPROFILE%\TEAMworks360-Demo

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

%PY% -c "import flask, reportlab, PIL, openpyxl" >nul 2>nul
if not %errorlevel%==0 (
    echo Installing dependencies ^(first run only^)...
    %PY% -m pip install -r requirements.txt
)

%PY% -c "import db_location, os, sys; db_location.ensure_data_dir(); sys.exit(0 if os.path.exists(db_location.DB_PATH) else 1)" >nul 2>nul
if not %errorlevel%==0 (
    echo Setting up demo data for the first time...
    %PY% seed.py
    %PY% add_test_user.py
)

echo.
echo Starting the TEST COPY - your browser will open automatically.
echo Sign in with:  tester  /  TestDrive2026
echo.
%PY% app.py
pause
