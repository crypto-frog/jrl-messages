@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call install.bat
echo Stopping the background copy so this console instance can take over...
.venv\Scripts\python run_agent.py --stop >nul 2>nul
if errorlevel 1 (
  echo The background agent did not stop safely. Restart Windows before
  echo trying the console takeover again.
  pause & exit /b 1
)
echo Running the agent with a visible console. Errors print here.
echo Close this window to stop it; it restarts at next logon, or run
echo install.bat / the app to bring it back sooner.
.venv\Scripts\python.exe run_agent.py
pause
