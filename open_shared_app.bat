@echo off
REM Double-click this file to open TEAMworks360.
REM
REM This is the file everyone EXCEPT the shared host computer should use.
REM It does NOT start a server, does NOT install anything, and does NOT
REM create any database on this computer - it only opens your normal web
REM browser to the shared copy that TEAM's host computer is already
REM running, exactly the way everyone else sees it. Nothing entered in
REM the app is ever stored on this computer.
setlocal enabledelayedexpansion
cd /d "%~dp0"

set CONFIG_FILE=shared_host_address.txt

if exist "%CONFIG_FILE%" (
    set /p HOST_URL=<"%CONFIG_FILE%"
) else (
    echo ======================================================
    echo   TEAMworks360 - one-time setup
    echo ======================================================
    echo.
    echo This computer hasn't been told where the shared app lives yet.
    echo Ask whoever manages it for the address - it looks something like
    echo http://192.168.1.50:5000 - and enter it below. You'll only need
    echo to do this once on this computer.
    echo.
    set /p HOST_URL="Shared app address: "
    echo !HOST_URL!| findstr /b /i "http://" >nul
    if errorlevel 1 (
        echo !HOST_URL!| findstr /b /i "https://" >nul
        if errorlevel 1 set HOST_URL=http://!HOST_URL!
    )
    echo !HOST_URL!> "%CONFIG_FILE%"
    echo.
)

REM Create a desktop shortcut with the app's own icon on first run, so
REM there's a proper clickable icon to use from then on. Same mechanism as
REM the other launchers - see create_shortcut.ps1.
where powershell >nul 2>nul
if %errorlevel%==0 (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create_shortcut.ps1" -TargetScript "open_shared_app.bat" -ShortcutName "TEAMworks360"
)

echo Opening %HOST_URL% ...
start "" "%HOST_URL%"

echo.
echo If the page doesn't load: check the host computer is switched on,
echo and that you're on the same office network as it. If the address
echo has changed, delete the file called shared_host_address.txt in this
echo folder and run this again to enter the new one.
timeout /t 5 >nul
