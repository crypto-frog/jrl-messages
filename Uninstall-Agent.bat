@echo off
cd /d "%~dp0"
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python run_agent.py --stop >nul 2>nul
  if errorlevel 1 (
    echo The agent did not stop safely, so registration was not removed.
    echo Restart Windows and run this uninstaller again.
    pause & exit /b 1
  )
  .venv\Scripts\python tools\make_startup_launcher.py --remove
)
schtasks /Delete /F /TN "JRL Messages Agent Failsafe" >nul 2>nul
echo.
echo Background collection is fully unregistered. The app window still works
echo when open, but nothing is collected while it is closed. Run install.bat
echo to bring the agent back.
pause
