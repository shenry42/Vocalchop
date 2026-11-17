@echo off
REM Milestone 2 automatic check: chops every file in Documents\Vocalchop\Acapellas and measures
REM 1) cuts on note changes, 2) clicks at slice edges, 3) Glide / low-confidence flags.
REM Results: Documents\Vocalchop\m2_check_results.txt (no listening needed).
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\src"
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  pause
  exit /b 1
)
echo Checking (about 20-40 seconds)...
.venv\Scripts\python.exe -m vocalchop check-m2
echo.
echo Results: %USERPROFILE%\Documents\Vocalchop\m2_check_results.txt
pause
