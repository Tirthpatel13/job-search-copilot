@echo off
rem One-click start for Windows: sets everything up on first run, then opens the app.
setlocal
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python 3.11+ is required. Install it from https://www.python.org/downloads/
  echo and tick "Add python.exe to PATH" during setup, then run this again.
  pause
  exit /b 1
)

if not exist .venv (
  echo First run: creating a private Python environment in .venv ...
  %PY% -m venv .venv || (pause & exit /b 1)
)
call .venv\Scripts\activate.bat

rem Reinstall only when requirements.txt changes.
fc /b requirements.txt .venv\installed-requirements.txt >nul 2>nul
if errorlevel 1 (
  echo Installing dependencies ^(a few minutes the first time^) ...
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt || (pause & exit /b 1)
  copy /y requirements.txt .venv\installed-requirements.txt >nul
)

if not exist .env copy .env.example .env >nul

echo Starting Job Copilot at http://localhost:8000 ^(close this window to stop^) ...
start "" cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8000"
python -m jobcopilot.cli serve --host 127.0.0.1 --port 8000
pause
