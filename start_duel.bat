@echo off
set SESSION_NAME=DUEL
set SCRIPT=duel.py

:menu
cls
echo ===========================================
echo    MBII DUEL MANAGEMENT (WINDOWS)
echo ===========================================
echo 1) Start Duel (Detached)
echo 2) Stop Duel
echo 3) Restart Duel
echo 4) Status
echo 5) Exit
echo ===========================================
set /p choice="Choose an option (1-5): "

if "%choice%"=="1" goto start
if "%choice%"=="2" goto stop
if "%choice%"=="3" goto restart
if "%choice%"=="4" goto status
if "%choice%"=="5" exit
goto menu

:start
tasklist /FI "WINDOWTITLE eq %SESSION_NAME%" | find /i "python.exe" >nul
if %errorlevel% equ 0 (
    echo [!] Duel is already running!
    pause
    goto menu
)
echo [*] Starting Duel in a new detached window...
:: Starts minimized so it stays out of your way
start "%SESSION_NAME%" /min python %SCRIPT%
echo [+] Duel is now running in the background.
pause
goto menu

:stop
echo [*] Killing Duel process...
taskkill /FI "WINDOWTITLE eq %SESSION_NAME%" /F >nul 2>&1
echo [+] Duel has been stopped.
pause
goto menu

:restart
call :stop
timeout /t 2 >nul
call :start
goto menu

:status
tasklist /FI "WINDOWTITLE eq %SESSION_NAME%" | find /i "python.exe" >nul
if %errorlevel% equ 0 (
    echo [+] Duel Status: RUNNING
) else (
    echo [-] Duel Status: STOPPED
)
pause
goto menu