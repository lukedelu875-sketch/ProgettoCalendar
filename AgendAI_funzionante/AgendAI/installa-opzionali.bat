@echo off
cd /d "%~dp0"
set "VENV=%~dp0.venv"
if not exist "%VENV%\Scripts\python.exe" set "VENV=%~dp0..\.venv"
echo Installo i pacchetti facoltativi uno alla volta (se uno non c'e' per la tua versione di Python, passo al prossimo)...
for %%P in (pillow pytesseract pymupdf) do (
  echo.
  echo --- %%P
  "%VENV%\Scripts\python.exe" -m pip install -q %%P || echo [non installato: %%P]
)
echo.
echo Per l'OCR sul computer serve anche il programma Tesseract per Windows con la lingua Italian.
echo Non e' obbligatorio: gli screenshot si leggono comunque nel browser.
pause
