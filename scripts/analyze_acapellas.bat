@echo off
REM Analyzes every audio file in Documents\Vocalchop\Acapellas with the range saved for each file,
REM writes each file's project folder and Documents\Vocalchop\analysis_summary.txt.
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\src"
.venv\Scripts\python.exe -m vocalchop analyze %*
echo.
echo Summary: %USERPROFILE%\Documents\Vocalchop\analysis_summary.txt
pause
