@echo off
setlocal EnableDelayedExpansion
:: wisper-transcribe — Windows launcher
:: Double-click this file to start the web UI.
:: The first run will set up the virtual environment automatically (~5 min).

cd /d "%~dp0"

echo.
echo wisper-transcribe
echo =================

:: ── First-time setup ─────────────────────────────────────────────────────────
if not exist ".venv\" (
    echo First run -- setting up wisper-transcribe ^(this takes a few minutes^)...
    echo.
    powershell -ExecutionPolicy Bypass -File setup.ps1
    if !errorlevel! neq 0 (
        echo.
        echo Setup failed. See messages above.
        pause
        exit /b 1
    )
)

:: ── Start server ─────────────────────────────────────────────────────────────
echo.
echo Starting wisper at http://localhost:8080
echo Press Ctrl+C to stop.
echo.

:: Open browser after the server has had a moment to start
start "" /B cmd /c "timeout /t 3 /nobreak >nul 2>&1 && start http://localhost:8080"

:: Activate venv and launch
call .venv\Scripts\activate.bat
wisper server --host 127.0.0.1 --port 8080

:: Keep window open if server exits with an error
if !errorlevel! neq 0 (
    echo.
    echo Server stopped unexpectedly. Press any key to close.
    pause > nul
)
