@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher not found. Install Python 3.12 from python.org first.
  pause & exit /b 1
)

rem Stop the old process before upgrading the interpreter environment it is
rem executing from.  --stop now waits for the agent endpoint and lock to go.
if exist .venv\Scripts\python.exe (
  echo Stopping the previous background agent before the upgrade...
  .venv\Scripts\python.exe run_agent.py --stop >nul 2>nul
  if errorlevel 1 (
    echo The old agent did not stop safely. Restart Windows, then run this
    echo installer again before replacing the environment.
    pause & exit /b 1
  )
)

py -3.12 -m venv .venv
if errorlevel 1 (
  echo Python 3.12 was not found. Install it from python.org, then run this again.
  pause & exit /b 1
)
.venv\Scripts\python -m pip install --upgrade pip
.venv\Scripts\python -m pip install -r requirements.txt
if errorlevel 1 ( echo Install failed. See messages above. & pause & exit /b 1 )

rem A ZIP extracted to a new folder has no old local venv, but an earlier
rem version may still own the per-user pipe from its previous folder. Use the
rem newly installed client to stop that copy too and wait for full shutdown.
echo Checking for a background agent from another installation folder...
.venv\Scripts\python.exe run_agent.py --stop >nul 2>nul
if errorlevel 1 (
  echo A previous agent did not stop safely. Restart Windows, then run this
  echo installer again. This is especially important when upgrading 3.0.0.
  pause & exit /b 1
)

echo Registering the background agent to start at every logon...
.venv\Scripts\python tools\make_startup_launcher.py
if errorlevel 1 ( echo Could not create the Startup entry. & pause & exit /b 1 )

rem Best-effort hourly failsafe: if the agent is ever stopped and the window
rem is closed, this relaunches the supervisor within the hour. Duplicate
rem launches are harmless: agent and supervisor exit when one already runs.
rem Some systems restrict schtasks; the Startup entry alone is sufficient.
schtasks /Create /F /TN "JRL Messages Agent Failsafe" /SC HOURLY ^
  /TR "wscript.exe \"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JRL-Messages-Agent.vbs\"" >nul 2>nul
if errorlevel 1 (
  echo Note: the hourly failsafe task could not be created. That is fine;
  echo the Startup entry and the app itself both restart the agent.
)

echo Starting the background agent now...
wscript "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\JRL-Messages-Agent.vbs"
.venv\Scripts\python.exe run_agent.py --wait-ready
if errorlevel 1 (
  echo The agent did not start with the installed version.
  echo Run Agent-Console.bat and send the visible error for diagnosis.
  pause & exit /b 1
)

echo.
echo Setup complete.
echo   - Messages are now collected in the background from logon, even
echo     while the app window is closed.
echo   - Start the app with JRL-Messages.bat
echo   - Agent-Console.bat runs the agent with visible output for debugging
echo   - Stop-Agent.bat stops background collection until next logon
pause
