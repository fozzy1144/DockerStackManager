@echo off
rem Starts the app with no console window: pythonw is Python without one, and
rem "start" lets this window close at once instead of waiting for the app.
rem Startup errors are shown in a dialog instead (see main.py).
cd /d "%~dp0"
where pythonw >nul 2>nul
if errorlevel 1 goto console
start "" pythonw main.py
exit /b 0

:console
rem No pythonw on PATH (an unusual install): run with a console after all.
python main.py
if errorlevel 1 pause
