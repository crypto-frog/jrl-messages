@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo Run install.bat first.
  pause & exit /b 1
)
.venv\Scripts\python run_agent.py --stop
if errorlevel 1 (
  echo.
  echo The agent did not stop within the safe shutdown window.
  pause & exit /b 1
)
echo.
echo The background agent has stopped. It returns at next logon,
echo when you open the app window, or when install.bat runs.
pause
