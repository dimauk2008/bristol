# Bristol Coupons & Card

Веб-панель для выдачи карты лояльности и купонов Bristol: карта с Data Matrix, купоны, ссылки-гранты, тикер и админка. Основной код — в `web_coupons/`.

## Деплой (Docker)

В корне лежит `Dockerfile`, который собирает и запускает именно `web_coupons`:

```bash
docker build -t bristol .
docker run -p 8000:8000 --env-file .env -v bristol_data:/data bristol
```

Контейнер слушает `$PORT` (по умолчанию `8000`), приложение доступно на `/login`.

Переменные окружения (задаются в панели хостинга):

| Переменная | Назначение |
|---|---|
| `PORT` | Порт приложения (обычно задаёт хостинг) |
| `ADMIN_PASSWORD` | Пароль входа в админку |
| `BRISTOL_REFRESH_TOKEN` | Refresh-токен Bristol для продакшена (без ADB) |
| `BRISTOL_TOKEN` | Необязательный access-токен |
| `BRISTOL_EXTERNAL_ID` | Guid пользователя (external_id) |
| `BRISTOL_CARD_DEVICE_ID` | Device id для Data Matrix карты |
| `BRISTOL_X_API_KEY` | API-ключ Bristol |
| `DATABASE_URL` | Строка подключения PostgreSQL (`postgresql://user:pass@host:5432/db`). Если задана — SQLite не используется |
| `DB_PATH` | Путь к SQLite (по умолчанию `/data/coupons.db` в контейнере) |

Для сохранения базы между перезапусками подключите volume к `/data`.

## Локальный запуск

```powershell
pip install -r web_coupons\requirements.txt
python -m uvicorn main:app --host 127.0.0.1 --port 8001 --app-dir web_coupons
```

Открыть http://127.0.0.1:8001 и войти с `ADMIN_PASSWORD`.

## Структура

- `web_coupons/` — FastAPI-приложение: админка, публичные страницы карты и купонов, Data Matrix, интеграция с Bristol API.
- `capture/` — скрипты захвата трафика и фарма (локально, в образ не попадают).

## Безопасность

Не коммитьте `.env`, `coupons.db`, `bristol_prefs.xml`, `capture/credentials*` и refresh-токены — они исключены через `.gitignore` / `.dockerignore`.
