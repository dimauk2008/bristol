@echo off
chcp 65001 >nul 2>nul
cd /d "%~dp0"
start "" http://127.0.0.1:8001
python -m uvicorn main:app --host 127.0.0.1 --port 8001
pause
