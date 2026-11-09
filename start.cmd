@echo off
REM Starts Pack-A-Lot with Python (Pack-A-Lot.exe does the same without needing Python).
REM Uses the portable runtime\python from Prepare-Portable.ps1 when present, otherwise the installed Python.
cd /d "%~dp0"
if exist "%~dp0runtime\python\python.exe" (
  "%~dp0runtime\python\python.exe" "%~dp0app.py"
  exit /b
)
where python >nul 2>nul || (
  echo Python was not found. Use Pack-A-Lot.exe, or run Prepare-Portable.ps1 to download a portable Python.
  pause
  exit /b 1
)
python -c "import flask, yaml" 2>nul || python -m pip install -r requirements.txt
python app.py
