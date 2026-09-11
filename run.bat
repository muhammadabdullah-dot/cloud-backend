@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Setting up the virtual environment for the first time...
    python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt
)

".venv\Scripts\python.exe" -m app.main

echo.
echo Server stopped.
pause
