@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "code=%ERRORLEVEL%"
echo.
echo slife2 installer finished with exit code %code%.
pause
exit /b %code%
