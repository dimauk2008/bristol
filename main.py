"""Точка входа для PaaS-хостингов: uvicorn main:app / python main.py.

Запускает панель Bristol (web_coupons).
"""
import os

from web_coupons.main import app

if __name__ == '__main__':
    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
