@echo off
REM Vocal Chop Sampler one-time setup: Python 3.12, the project's Python environment, packages, Git.
REM Safe to run again: it skips anything already done.
setlocal
cd /d "%~dp0.."
set "ROOT=%CD%"
set "LOG=%ROOT%\setup_log.txt"
echo Vocal Chop Sampler setup - this can take 3-8 minutes. Details go to setup_log.txt
echo === setup %date% %time% === > "%LOG%"

REM ---- 1. Python 3.12 (already installed for OrchSketch, so normally skipped) ----
set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY312%" goto havepy
echo [1/5] Installing Python 3.12...
winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
:havepy
if not exist "%PY312%" goto err_py
"%PY312%" --version >> "%LOG%" 2>&1

REM ---- 2. Virtual environment ----
if exist ".venv\Scripts\python.exe" goto havevenv
echo [2/5] Creating the project's Python environment...
"%PY312%" -m venv .venv >> "%LOG%" 2>&1
:havevenv
set "VPY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%VPY%" goto err_venv

REM ---- 3. Packages ----
echo [3/5] Installing Python packages (the biggest step)...
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install -r requirements-dev.txt >> "%LOG%" 2>&1
if errorlevel 1 goto err_pip
"%VPY%" -m pip freeze > requirements.lock.txt

REM ---- 4. Git (the GitHub repo shenry42/vocalchop gets its first push from backup.bat) ----
echo [4/5] Setting up Git...
git --version >> "%LOG%" 2>&1
if errorlevel 1 goto err_git
git config --global user.name >nul 2>&1 || call :askname
git config --global user.email >nul 2>&1 || call :askemail
if exist ".git" goto havegit
git init -b main >> "%LOG%" 2>&1
git remote add origin https://github.com/shenry42/vocalchop.git >> "%LOG%" 2>&1
:havegit

echo [5/5] Checking everything...
"%VPY%" scripts\verify_setup.py >> "%LOG%" 2>&1
if errorlevel 1 goto err_verify
type "%LOG%" | findstr /b "OK FAIL Python"
echo.
echo SETUP OK. You can close this window.
goto done

:askname
set "GN=Sam"
set /p "GN=Name to show on your Git commits [press Enter for Sam]: "
git config --global user.name "%GN%"
exit /b
:askemail
set "GE=shenry42@users.noreply.github.com"
set /p "GE=Email for Git commits [press Enter for shenry42@users.noreply.github.com]: "
git config --global user.email "%GE%"
exit /b

:err_py
echo ERROR: Python 3.12 did not install. See setup_log.txt
goto done
:err_venv
echo ERROR: could not create the Python environment. See setup_log.txt
goto done
:err_pip
echo ERROR: package install failed. See setup_log.txt
goto done
:err_git
echo ERROR: Git isn't installed or isn't on the PATH. See setup_log.txt
goto done
:err_verify
type "%LOG%" | findstr /b "OK FAIL Python"
echo ERROR: final check failed (see the FAIL line above, and setup_log.txt)
goto done

:done
echo === end %date% %time% === >> "%LOG%"
pause
