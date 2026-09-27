@echo off
cd /d "%~dp0"

if exist "venv\Scripts\python.exe" (
    set PY=venv\Scripts\python.exe
) else (
    set PY=python
)

echo Gagamitin: %PY%
echo Magbubukas ng browser - mag-sign in sa mototyre0505@gmail.com at i-Allow.
echo.
"%PY%" regenerate_gmail_token.py
if errorlevel 1 (
    echo.
    echo May error sa itaas. I-screenshot mo ito at ipadala.
)
echo.
echo ^(I-scroll pataas kung kailangan, kopyahin yung mahabang code sa pagitan ng "====" na linya^)
pause
