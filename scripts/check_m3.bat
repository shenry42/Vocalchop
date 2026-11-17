@echo off
REM Milestone 3 automatic check: renders 3 slices of every file in Documents\Vocalchop\Acapellas at all 25 shifts
REM and measures pitch (built-in tuner + pitch curve), length, formants, clicks, level and harmonicity; then times
REM a 32-slice Save and checks the render cache. Results: Documents\Vocalchop\m3_check_results.txt (no listening).
REM Renders go to Documents\Vocalchop\m3_check (your own project folders are not touched).
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\src"
if not exist ".venv\Scripts\python.exe" (
  echo Run scripts\setup.bat first.
  pause
  exit /b 1
)
echo Checking (about 1-3 minutes)...
.venv\Scripts\python.exe -m vocalchop check-m3
echo.
echo Results: %USERPROFILE%\Documents\Vocalchop\m3_check_results.txt
pause
