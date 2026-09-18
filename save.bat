@echo off
cd /d "%~dp0"
echo Saving changes to GitHub...
git add -A
git commit -m "update"
git push origin main
echo.
echo Done!
pause
