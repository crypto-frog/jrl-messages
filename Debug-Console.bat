@echo off
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call install.bat
echo Running with a visible console. Errors will print here.
.venv\Scripts\python.exe run.py
pause
