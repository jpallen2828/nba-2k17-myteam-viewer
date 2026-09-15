@echo off
setlocal
echo Close both MyTEAM applications before continuing.
echo Paste the OLD folder containing 2K17 MyTEAM.exe, then press Enter.
set /p "previous=Old application folder: "
set "previous=%previous:"=%"
if not defined previous exit /b 1
start "" /wait "%~dp02K17 MyTEAM.exe" --migrate-from "%previous%" --report "%~dp0migration-report.json"
if errorlevel 1 (
  echo Migration failed. Your old folder was not changed. Use a fresh destination folder and check the selected path.
) else (
  echo Personal data imported. Open MyTEAM in this new folder to check your cards and lineups.
)
pause
