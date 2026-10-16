@echo off
chcp 65001 >nul
cd /d D:\avito-call\web_coupons
python -m uvicorn main:app --host 0.0.0.0 --port 8001
pause