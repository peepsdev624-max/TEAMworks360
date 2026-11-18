@echo off
REM Clears the hours booked on the Hours Quoted & Booked screen so the app can start
REM capturing real hours. Shows a preview first and takes a backup before it changes anything.
REM Close TEAMworks360 before running this.
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
        pause
        exit /b 1
    )
)

echo.
echo ===== PREVIEW - nothing is changed yet =====
%PY% clear_hours.py
if not %errorlevel%==0 (
    echo.
    echo Something went wrong - nothing was changed.
    pause
    exit /b 1
)

echo.
set /p GO="Type YES to clear the booked hours (a backup is taken first): "
if /i not "%GO%"=="YES" (
    echo Cancelled - nothing was changed.
    pause
    exit /b 0
)
%PY% clear_hours.py --apply
pause
