@echo off
REM End-of-milestone backup: runs the tests, commits, tags vX.Y, pushes to GitHub (shenry42/vocalchop)
REM and writes releases\vocalchop-vX.Y-src.zip (plus a copy in Documents\Vocalchop\Backups).
setlocal
cd /d "%~dp0.."
for /f "delims=" %%v in ('.venv\Scripts\python.exe scripts\print_version.py') do set "VER=%%v"
if not defined VER goto noversion
echo Backing up Vocal Chop Sampler v%VER%
set "QT_QPA_PLATFORM=offscreen"
set "PYTHONPATH=%CD%\src"
echo [1/4] Running tests...
.venv\Scripts\python.exe -m pytest -q > test_results.txt 2>&1
if errorlevel 1 goto testfail
git rev-parse -q --verify "refs/tags/v%VER%" >nul && goto tagexists
echo [2/4] Committing and tagging...
git add -A
set "MSGOK="
if exist build\commit_msg.txt findstr /b /c:"v%VER%:" build\commit_msg.txt >nul && set "MSGOK=1"
if defined MSGOK (git commit -q -F build\commit_msg.txt) else (git commit -q -m "Vocal Chop Sampler v%VER%")
if exist build\commit_msg.txt del build\commit_msg.txt
git tag -a "v%VER%" -m "Vocal Chop Sampler v%VER%"
if errorlevel 1 goto gitfail
echo [3/4] Pushing to GitHub (a sign-in window may open the first time)...
git push -u origin main
if errorlevel 1 goto pushfail
git push origin "v%VER%"
if errorlevel 1 goto pushfail
echo [4/4] Writing the source zip...
if not exist releases mkdir releases
git archive --format=zip --prefix=vocalchop-v%VER%/ -o "releases\vocalchop-v%VER%-src.zip" "v%VER%"
if errorlevel 1 goto gitfail
if not exist "%USERPROFILE%\Documents\Vocalchop\Backups" mkdir "%USERPROFILE%\Documents\Vocalchop\Backups"
copy /y "releases\vocalchop-v%VER%-src.zip" "%USERPROFILE%\Documents\Vocalchop\Backups\" >nul
echo.
echo BACKUP OK: v%VER% pushed to github.com/shenry42/vocalchop, zip in releases\ and Documents\Vocalchop\Backups
goto done
:noversion
echo ERROR: couldn't read the version. Has setup.bat run?
goto done
:testfail
echo ERROR: tests failed, nothing was backed up. See test_results.txt
goto done
:tagexists
echo Tag v%VER% already exists - nothing to do.
goto done
:gitfail
echo ERROR: a git step failed. Scroll up for the message.
goto done
:pushfail
echo ERROR: push failed. Is the empty repo shenry42/vocalchop created on github.com?
echo Your commit and tag are saved locally; run backup.bat again after fixing.
:done
pause
