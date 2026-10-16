from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
DB_PATH = BASE_DIR / "avito.db"
COOKIES_PATH = BASE_DIR / "cookies.json"

DEFAULT_SEARCH_URL = "https://www.avito.ru/omsk/dlya_doma_i_dachi?q=ikea"
DEFAULT_POLL_INTERVAL = 15 * 60
MIN_POLL_INTERVAL = 60
MAX_POLL_INTERVAL = 24 * 3600

DEFAULT_SOURCE_NAME = "ikea Омск"
DEFAULT_CITY_SLUG = "omsk"

CITIES = {
    "omsk": "Омск",
    "msk": "Москва",
    "moskva": "Москва",
    "sankt-peterburg": "Санкт-Петербург",
    "ekaterinburg": "Екатеринбург",
    "kazan": "Казань",
    "novosibirsk": "Новосибирск",
    "chelyabinsk": "Челябинск",
    "samara": "Самара",
    "ufa": "Уфа",
    "perm": "Пермь",
    "voronezh": "Воронеж",
    "krasnodar": "Краснодар",
    "volgograd": "Волгоград",
    "krasnoyarsk": "Красноярск",
    "tyumen": "Тюмень",
    "surgut": "Сургут",
    "orenburg": "Оренбург",
    "kaliningrad": "Калининград",
    "tomsk": "Томск",
    "irkutsk": "Иркутск",
    "stavropol": "Ставрополь",
    "sochi": "Сочи",
    "rostov": "Ростов-на-Дону",
    "barnaul": "Барнаул",
    "izhevsk": "Ижевск",
    "vladivostok": "Владивосток",
    "elektrostal": "Электросталь",
}

SOURCE_SCAN_GAP_SECONDS = (5.0, 10.0)

REQUEST_TIMEOUT = 30
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 5.0
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "ru-RU,ru;q=0.9"

SETTING_POLL_INTERVAL = "poll_interval"

MAX_LISTINGS_LIMIT = 200
MAX_NOTIFICATIONS_LIMIT = 200
