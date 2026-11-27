@echo off
cd /d "%~dp0"
set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\python.exe" set "VENV=%~dp0..\.venv"
if not exist "%VENV%\Scripts\python.exe" (
  echo Non trovo la cartella .venv.
  pause
  exit /b
)
echo Controllo in corso (usa pochissimi token)...
echo.
"%VENV%\Scripts\python.exe" -m app.diagnose
echo.
pause
