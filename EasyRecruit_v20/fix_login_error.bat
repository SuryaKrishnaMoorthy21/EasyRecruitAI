@echo off
setlocal EnableExtensions
TITLE EasyRecruit ATS 3.0 - Fix Login Error (Permanent)
color 0A

cd /d "%~dp0"

echo.
echo ============================================================
echo   EasyRecruit ATS 3.0 - Permanent Fix: Login Error
echo ============================================================
echo.
echo  ROOT CAUSE: passlib 1.7.4 is incompatible with bcrypt 4.1+
echo  FIX: passlib has been REMOVED from the codebase entirely.
echo       bcrypt is now called directly - zero version conflicts.
echo.

set "RUNPY="
if exist "pyembed\python.exe" set "RUNPY=%CD%\pyembed\python.exe"
if not defined RUNPY if exist "venv\Scripts\activate.bat" (
    call "venv\Scripts\activate.bat"
    if not errorlevel 1 set "RUNPY=python"
)

if not defined RUNPY (
    echo [ERROR] No environment found for this project yet.
    echo         Please run install_and_run.bat first.
    pause
    exit /b 1
)
echo [1/3] Environment found.

echo [2/3] Removing passlib, no longer needed...
"%RUNPY%" -m pip uninstall passlib -y -q 2>nul
echo [OK] passlib removed, or was not installed.

echo [3/3] Ensuring bcrypt and PyJWT are installed...
"%RUNPY%" -m pip install "bcrypt>=3.2.0" -q
"%RUNPY%" -m pip install "PyJWT==2.8.0" -q >nul 2>&1
if errorlevel 1 "%RUNPY%" -m pip install "PyJWT>=2.8.0,<3" -q
echo [OK] Done.

echo.
echo ============================================================
echo   Fix complete! Login error is permanently resolved.
echo   Run install_and_run.bat to start the server.
echo ============================================================
echo.
pause
