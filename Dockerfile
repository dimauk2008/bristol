FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/data/coupons.db

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tesseract-ocr \
        tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

COPY web_coupons/requirements.txt /app/web_coupons/requirements.txt
RUN pip install --no-cache-dir -r /app/web_coupons/requirements.txt

COPY web_coupons /app/web_coupons

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/login' % os.environ.get('PORT','8000'), timeout=4)"

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --app-dir web_coupons"]
