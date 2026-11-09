@echo off
REM Builds Pack-A-Lot.exe (single file) in this folder.
REM The exe embeds Python + Flask; it uses the static\, scripts\, "PSADT Template\" folders
REM and config.yaml that sit next to it, so keep it in the project folder.
cd /d "%~dp0"
python -m pip install --quiet -r requirements.txt pyinstaller || goto :error
python -m PyInstaller --noconfirm --clean --onefile --console ^
  --name Pack-A-Lot --icon "%~dp0assets\packalot.ico" ^
  --distpath . --workpath "%TEMP%\PackALot-build" --specpath "%TEMP%\PackALot-build" ^
  app.py || goto :error
REM Explorer caches icons per path; refresh it so the replaced exe shows its icon
"%windir%\System32\ie4uinit.exe" -show >nul 2>&1
echo.
echo Built: %~dp0Pack-A-Lot.exe
exit /b 0
:error
echo Build failed.
exit /b 1
