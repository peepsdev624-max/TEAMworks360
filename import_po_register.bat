@echo off
REM Imports the TEAM Purchase Order Register into the app's Purchase Orders as history
REM - safely: it only adds POs, all flagged Historic and never counted in job costs, shows
REM you a preview first and takes a backup. It never deletes or changes your
REM customers, works orders or existing purchase orders.
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

%PY% -c "import flask, reportlab, PIL, openpyxl, dateutil" >nul 2>nul
if not %errorlevel%==0 (
    echo Installing dependencies ^(first run only^)...
    %PY% -m pip install -r requirements.txt
)

echo.
echo Drag the new Purchase Order Register spreadsheet onto this window ^(or type its full path^),
echo then press Enter.
set /p XL="Purchase Order Register file: "
set XL=%XL:"=%

echo.
echo ===== PREVIEW - nothing is changed yet =====
%PY% import_po_register.py "%XL%"
if not %errorlevel%==0 (
    echo.
    echo Something went wrong - nothing was changed.
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
%PY% import_po_register.py "%XL%" --apply
pause
