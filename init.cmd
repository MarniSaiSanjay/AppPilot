@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0init.ps1" %*
exit /b %ERRORLEVEL%
