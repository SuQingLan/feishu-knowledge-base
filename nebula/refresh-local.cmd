@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0local-service.ps1" refresh
pause
