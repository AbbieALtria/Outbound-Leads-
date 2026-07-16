@echo off
REM ============================================================
REM  Double-click this file to start the SEO Leads app.
REM  It opens your web browser automatically.
REM  To stop the app, just close this black window.
REM ============================================================
cd /d "%~dp0"
title SEO Leads

REM --- find Python (prefer the 'py' launcher, fall back to 'python') ---
set "PY=python"
where py >nul 2>nul
if %errorlevel%==0 set "PY=py"

REM --- confirm Python is available ---
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo.
  echo  Python was not found on this computer.
  echo  Please install Python from https://www.python.org/downloads/
  echo  and be sure to tick "Add Python to PATH" during setup.
  echo.
  pause
  exit /b 1
)

REM --- install required packages the first time only ---
%PY% -c "import flask, requests" >nul 2>nul
if errorlevel 1 (
  echo.
  echo  First-time setup: installing required packages, please wait...
  echo.
  %PY% -m pip install flask requests
)

echo.
echo  Starting SEO Leads...
echo.
%PY% app.py

echo.
echo  SEO Leads has stopped. You can close this window.
pause
