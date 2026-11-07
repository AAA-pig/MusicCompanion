@echo off
setlocal
chcp 65001 >nul
set "PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%PYTHON%" (
  echo Local Python environment not found. Create .venv and install requirements.txt first.
  pause
  exit /b 1
)
echo Start music in NetEase Cloud Music. Press Ctrl+C to stop detection.
echo Only acoustic events are logged locally; raw audio is not saved or uploaded.
"%PYTHON%" "%~dp0monitor_audio.py" --seconds 0
pause
endlocal

