@echo off
REM Starts the Vocal Chop Sampler analysis window.
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\src"
if not exist ".venv\Scripts\pythonw.exe" (
  echo Run scripts\setup.bat first.
  pause
  exit /b 1
)
start "" ".venv\Scripts\pythonw.exe" -m vocalchop
