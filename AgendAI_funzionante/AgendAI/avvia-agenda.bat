@echo off
cd /d "%~dp0"

rem Cerca l'ambiente Python (.venv) in questa cartella oppure in quella sopra
set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\python.exe" set "VENV=%~dp0..\.venv"

if not exist "%VENV%\Scripts\python.exe" (
  echo Non trovo la cartella .venv ne qui ne nella cartella sopra.
  echo Rifai i passaggi di installazione del README.
  pause
  exit /b
)
if not exist "app\main.py" (
  echo Non trovo app\main.py: copia questo file nella cartella che contiene "app" e "static".
  pause
  exit /b
)
if not exist ".env" (
  echo Manca il file .env con le tue impostazioni.
  pause
  exit /b
)

rem Installa da solo i pacchetti che mancano (serve solo la prima volta dopo un aggiornamento)
"%VENV%\Scripts\python.exe" -c "import pypdf, httpx" 2>nul
if errorlevel 1 (
  echo Installo i pacchetti mancanti...
  "%VENV%\Scripts\python.exe" -m pip install -q -r requirements.txt
)

echo Avvio del server...
rem 127.0.0.1: il sito e' raggiungibile solo dal tunnel e da questo computer, non da altri della rete
start "Agenda - server" cmd /k ""%VENV%\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000"
timeout /t 5 /nobreak >nul
echo Avvio del tunnel...
start "Agenda - tunnel" cmd /k "cloudflared tunnel --url http://127.0.0.1:8000"
start "" http://localhost:8000
echo.
echo Fatto. Nella finestra "Agenda - tunnel" trovi l'indirizzo https://...trycloudflare.com
echo Se qualcosa non va: apri Impostazioni nel sito e premi "Controlla che tutto funzioni",
echo oppure fai doppio clic su diagnosi.bat
timeout /t 10 >nul
