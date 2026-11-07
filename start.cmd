@echo off
rem Double-click to start Snipmint (+ n8n). Same as: powershell -ExecutionPolicy Bypass -File start.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
echo.
pause
