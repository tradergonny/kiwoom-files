@echo off
REM Kiwoom Auto Sell - Windows run script (ASCII only)

cd /d "%~dp0"

REM --- Check Python ---
where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [ERROR] Python is not installed or not in PATH.
  echo Please install Python from https://www.python.org
  echo During install, check "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

REM --- Create virtual environment if missing ---
if not exist venv (
  echo [*] Creating virtual environment...
  python -m venv venv
  if errorlevel 1 (
    echo [ERROR] Failed to create venv.
    pause
    exit /b 1
  )
)

REM --- Use venv python directly ---
set "VENV_PY=%~dp0venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
  echo [ERROR] venv python not found: %VENV_PY%
  echo Delete the venv folder and run this script again.
  pause
  exit /b 1
)

REM --- Install dependencies ---
echo [*] Installing dependencies...
"%VENV_PY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VENV_PY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
  echo [ERROR] pip install failed. Check internet connection.
  pause
  exit /b 1
)

REM --- Kill any old server on port 8765 ---
echo [*] Checking port 8765...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :8765 ^| findstr LISTENING') do (
  echo [*] Killing old server process PID %%a
  taskkill /PID %%a /F >nul 2>nul
)

echo.
echo ================================================
echo  Kiwoom Auto Sell server starting...
echo  On this PC:  http://localhost:8765
echo  From iPhone via Tailscale: http://[PC-Tailscale-IP]:8765
echo  Stop server: Ctrl+C
echo ================================================
echo.

REM --- Run server on all interfaces (needed for Tailscale remote access) ---
"%VENV_PY%" -m uvicorn app.main:app --host 0.0.0.0 --port 8765

pause
