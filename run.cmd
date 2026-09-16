@echo off
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo ERROR: AppPilot setup is incomplete. Run .\setup.cmd first.
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0src\apppilot_run.py" %*
exit /b %ERRORLEVEL%
