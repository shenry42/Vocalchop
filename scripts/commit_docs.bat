@echo off
REM Commits and pushes changes to the docs only (SPEC.md, STATUS.md, README.md, CHANGELOG.md, scripts).
cd /d "%~dp0.."
git add SPEC.md STATUS.md README.md CHANGELOG.md scripts
git commit -q -m "Docs: status update"
if errorlevel 1 echo Nothing to commit.
git push origin main
if errorlevel 1 (echo ERROR: push failed.) else (echo DOCS PUSHED.)
pause
