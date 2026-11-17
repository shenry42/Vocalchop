@echo off
REM Runs the automated tests. Full output goes to test_results.txt.
cd /d "%~dp0.."
set "QT_QPA_PLATFORM=offscreen"
set "PYTHONPATH=%CD%\src"
echo Running tests (about 2-3 minutes)...
.venv\Scripts\python.exe -m pytest -v > test_results.txt 2>&1
if errorlevel 1 (
  echo.
  echo TESTS FAILED - see test_results.txt
) else (
  echo.
  echo TESTS PASSED
)
findstr /r /c:"passed" /c:"failed" test_results.txt | findstr /r /c:"=====" 
pause
