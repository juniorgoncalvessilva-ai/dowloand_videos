@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -r requirements.txt
echo.
echo Abra http://127.0.0.1:8765
echo.
python -m uvicorn app:app --host 127.0.0.1 --port 8765
