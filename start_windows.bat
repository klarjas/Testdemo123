@echo off
setlocal
cd /d "%~dp0"

echo ==========================================
echo       Khmer Auto-Dub Telegram Bot
echo ==========================================

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python virtual environment...
    py -3.13 -m venv .venv 2>nul
    if errorlevel 1 (
        py -3.11 -m venv .venv
        if errorlevel 1 (
            python -m venv .venv
        )
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo ERROR: Could not create the virtual environment.
    echo Install Python 3.11 or 3.13 and try again.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"

python -m pip install --upgrade pip
if errorlevel 1 goto :failed

pip install -r requirements.txt
if errorlevel 1 goto :failed

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo Created .env from .env.example.
    echo Edit .env and add BOT_TOKEN and OPENAI_API_KEY, then run this file again.
    notepad ".env"
    pause
    exit /b 0
)

python main.py
if errorlevel 1 goto :failed
goto :end

:failed
echo.
echo ERROR: Setup or startup failed. Read the message above.
pause
exit /b 1

:end
pause
