import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from .config import (
    ACCEPT_LANGUAGE,
    BACKOFF_BASE_SECONDS,
    COOKIES_PATH,
    REQUEST_TIMEOUT,
    USER_AGENT,
)

log = logging.getLogger("avito.scraper")

BASE_HOST = "https://www.avito.ru"

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": ACCEPT_LANGUAGE,
    "Accept-Encoding": "gzip, deflate",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

CITY_SUFFIX_RE = re.compile(r"\s+в\s+([А-ЯЁ][а-яё\- ]{2,40})$")
NUMBER_RE = re.compile(r"^(\d+)\s*([а-яё]+)")
PUBLISHED_RE = re.compile(r"(\d+)\s*([а-яё]+)", re.IGNORECASE)

UNIT_MAP = {
    "сек": "seconds",
    "мин": "minutes",
    "час": "hours",
    "дн": "days",
    "день": "days",
    "нед": "weeks",
    "мес": "months",
}


class ScraperError(Exception):
    pass


class RateLimitedError(ScraperError):
    pass


@dataclass
class RawListing:
    id: str
    url: str
    title: str
    price: int | None
    image: str | None
    location: str | None
    date_text: str | None
    published_at: datetime | None = None


def _response_text(response: requests.Response) -> str:
    charset = response.encoding
    if not charset or charset.lower() in {"iso-8859-1", "ascii"}:
        charset = "utf-8"
    response.encoding = charset
    return response.text


_session: requests.Session | None = None


def load_cookies(session: requests.Session) -> int:
    if not COOKIES_PATH.exists():
        return 0
    try:
        raw = json.loads(COOKIES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("не удалось прочитать cookies.json: %s", exc)
        return 0

    loaded = 0
    if isinstance(raw, dict):
        for name, value in raw.items():
            if name:
                session.cookies.set(str(name), str(value))
                loaded += 1
    elif isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            value = entry.get("value")
            if not name or value is None:
                continue
            session.cookies.set(
                str(name),
                str(value),
                domain=str(entry.get("domain") or ""),
                path=str(entry.get("path") or "/"),
            )
            loaded += 1
    return loaded


def get_session() -> requests.Session:
    global _session
    if _session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        loaded = load_cookies(session)
        if loaded:
            log.info("загружено кук из cookies.json: %s", loaded)
        _session = session
    return _session


def refresh_cookies() -> int:
    return load_cookies(get_session())


def fetch_page(url: str, attempts: int = 3) -> str:
    session = get_session()
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        time.sleep(random.uniform(1.0, 2.5))
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 2)
            log.warning("сетевая ошибка %s/%s (%s), жду %.1f с", attempt, attempts, exc, delay)
            time.sleep(delay)
            continue

        if response.status_code == 429:
            raise RateLimitedError(
                "Avito ограничил запросы с этого IP (HTTP 429, капча). "
                "Следующая попытка будет по расписанию опроса."
            )

        if response.status_code >= 500:
            last_error = ScraperError(f"HTTP {response.status_code}")
            if attempt == attempts:
                break
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)) + random.uniform(0, 2)
            log.warning("сервер вернул %s, попытка %s/%s, жду %.1f с", response.status_code, attempt, attempts, delay)
            time.sleep(delay)
            continue

        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ScraperError(str(exc)) from exc

        text = _response_text(response)
        if len(text) < 50_000:
            last_error = ScraperError(f"слишком маленький ответ ({len(text)} символов)")
            if attempt == attempts:
                break
            time.sleep(BACKOFF_BASE_SECONDS)
            continue
        return text

    raise ScraperError(f"не удалось получить страницу {url}: {last_error}")


def _meta_price(item) -> int | None:
    for attrs in ({"itemprop": "price"}, {"itemprop": "price", "content": True}):
        try:
            tag = item.find("meta", attrs=attrs)
        except Exception:
            tag = None
        if tag and tag.get("content"):
            digits = re.sub(r"\D", "", tag["content"])
            if digits:
                return int(digits)
    strong = item.select_one('[data-marker="item-price-value"]')
    if strong:
        digits = re.sub(r"\D", "", strong.get_text())
        if digits:
            return int(digits)
    return None


def _image(item) -> str | None:
    img = item.select_one('[data-marker="item-image"] img') or item.select_one("img[src]")
    if img:
        src = img.get("src") or img.get("data-src")
        if not src:
            srcset = img.get("srcset") or img.get("data-srcset")
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]
        if src and not src.startswith("data:"):
            return urljoin(BASE_HOST, src)

    slider = item.select_one('[data-marker^="slider-image/image-"]')
    if slider:
        marker = slider.get("data-marker") or ""
        if "slider-image/image-" in marker:
            candidate = marker.split("slider-image/image-", 1)[1]
            if candidate.startswith("http"):
                return candidate

    for node in item.select('[data-marker="item-image"] [style*="background-image"]'):
        match = re.search(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", node.get("style") or "")
        if match and match.group(1).startswith("http"):
            return match.group(1)

    return None


def _clean_link(href: str | None) -> str | None:
    if not href:
        return None
    absolute = urljoin(BASE_HOST, href)
    parts = urlsplit(absolute)
    if not parts.path:
        return None
    return f"{parts.scheme}://{parts.netloc}{parts.path}"


def _location(item, anchor_title: str | None, city_name: str | None) -> str | None:
    node = item.select_one('[data-marker="item-location"]')
    text = node.get_text(" ", strip=True) if node else ""
    if text:
        return text
    if city_name:
        return city_name
    if anchor_title:
        match = CITY_SUFFIX_RE.search(anchor_title)
        if match:
            return match.group(1).strip()
    return None


def _unit_to_seconds(value: str, unit: str) -> int | None:
    key = unit[:4]
    for prefix, name in UNIT_MAP.items():
        if key.startswith(prefix) or unit.startswith(prefix):
            amount = int(value)
            if name == "seconds":
                return amount
            if name == "minutes":
                return amount * 60
            if name == "hours":
                return amount * 3600
            if name == "days":
                return amount * 86400
            if name == "weeks":
                return amount * 7 * 86400
            if name == "months":
                return amount * 30 * 86400
    return None


def parse_published(text: str | None, now: datetime | None = None) -> datetime | None:
    if not text:
        return None
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    normalized = text.strip().lower().replace("назад", "").strip()
    if normalized in {"сегодня", "только что", "сейчас"}:
        return current
    if normalized == "вчера":
        return current - timedelta(days=1)
    match = PUBLISHED_RE.search(normalized)
    if not match:
        return None
    seconds = _unit_to_seconds(match.group(1), match.group(2))
    if seconds is None:
        return None
    return current - timedelta(seconds=seconds)


def _city_of(link: str) -> str | None:
    segments = [part for part in urlsplit(link).path.split("/") if part]
    return segments[0].lower() if segments else None


def parse_listings(
    html: str,
    base_url: str = BASE_HOST,
    city_slug: str | None = None,
    city_name: str | None = None,
) -> list[RawListing]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.select_one('[data-marker="catalog-serp"]') or soup
    listings: list[RawListing] = []
    expected_city = city_slug.strip().lower() if city_slug else None

    for item in container.select('[data-marker="item"][data-item-id]'):
        item_id = item.get("data-item-id")
        anchor = item.select_one('[data-marker="item-title"]')
        name_node = item.select_one('h2[itemprop="name"]') or anchor
        title = name_node.get_text(" ", strip=True) if name_node else ""
        anchor_title = anchor.get("title") if anchor else None
        if not title and anchor_title:
            title = anchor_title
        link = _clean_link(anchor.get("href") if anchor else None)
        if not item_id or not title or not link:
            continue

        if expected_city:
            item_city = _city_of(link)
            if item_city != expected_city:
                continue

        date_node = item.select_one('[data-marker="item-date"]')
        date_text = date_node.get_text(" ", strip=True) if date_node else None
        if date_text:
            date_text = date_text.replace("Назад", "назад")

        listings.append(
            RawListing(
                id=item_id,
                url=link,
                title=title,
                price=_meta_price(item),
                image=_image(item),
                location=_location(item, anchor_title, city_name),
                date_text=date_text,
                published_at=parse_published(date_text),
            )
        )

    return listings
