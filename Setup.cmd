@echo off
REM First run on a new PC: checks (and offers to fix) Windows Sandbox, winget, the Graph module,
REM IntuneWinAppUtil, config.yaml and network access, then starts Pack-A-Lot.
title Pack-A-Lot setup
cd /d "%~dp0"
REM Files copied from a downloaded zip carry a "from the internet" mark that can block the scripts
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0.' -Recurse -File | Unblock-File" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Check-Prerequisites.ps1" -Interactive
echo.
choice /c YN /m "Start Pack-A-Lot now"
if errorlevel 2 exit /b 0
if exist "%~dp0Pack-A-Lot.exe" (
  start "" "%~dp0Pack-A-Lot.exe"
) else (
  call "%~dp0start.cmd"
)
