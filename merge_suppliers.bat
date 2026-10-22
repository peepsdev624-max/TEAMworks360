@echo off
REM Merges duplicate suppliers (misspellings and name variants) into one record each.
REM Shows you a preview first and takes a backup before changing anything. Close TEAMworks360 before running this.
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

%PY% -c "import flask, reportlab, PIL, openpyxl, dateutil" >nul 2>nul
if not %errorlevel%==0 (
    echo Installing dependencies ^(first run only^)...
    %PY% -m pip install -r requirements.txt
)

echo.
echo ===== PREVIEW - nothing is changed yet =====
%PY% merge_suppliers.py
if not %errorlevel%==0 (
    echo.
    echo Nothing was changed.
    pause
    exit /b 1
)

echo.
set /p GO="Type YES to apply these changes (a backup is taken first): "
if /i not "%GO%"=="YES" (
    echo Cancelled - nothing was changed.
    pause
    exit /b 0
)
%PY% merge_suppliers.py --apply
pause
