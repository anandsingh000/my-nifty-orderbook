@echo off
cd /d "%~dp0"
if not exist ".venv" (
    echo Creating Python virtual environment...
    python -m venv .venv
)
call .venv\Scripts\activate
pip install -q -r requirements.txt
start "" http://127.0.0.1:8000
set HOST=127.0.0.1
python app.py
pause
