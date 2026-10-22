@echo off
rem One-step launcher: creates the virtual environment on first run, installs
rem the requirements, then starts the app at http://localhost:8501
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    py -3 -m venv .venv 2>nul || python -m venv .venv
)
if not exist ".venv\Scripts\python.exe" (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again.
    pause
    exit /b 1
)

echo Installing requirements...
".venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
    echo Installing requirements failed - see the messages above.
    pause
    exit /b 1
)

if not exist "service_account.json" if not exist "credentials.json" (
    echo.
    echo WARNING: service_account.json is missing. The app will run, but Margin stays
    echo blank until you copy the Google service account key into this folder.
    echo.
)

".venv\Scripts\python.exe" -m streamlit run app.py
