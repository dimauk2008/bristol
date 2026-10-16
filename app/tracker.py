import asyncio
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select

from .config import (
    DEFAULT_POLL_INTERVAL,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
    SETTING_POLL_INTERVAL,
    SOURCE_SCAN_GAP_SECONDS,
)
from .database import SessionLocal
from .events import manager
from .models import Listing, Notification, ScanLog, Setting, Source, utcnow
from .scraper import fetch_page, parse_listings, refresh_cookies
from .schemas import ScanOut

log = logging.getLogger("avito.tracker")


class ScanBusyError(RuntimeError):
    pass


class SourceNotFoundError(RuntimeError):
    pass


@dataclass
class ScanResult:
    status: str
    source_id: int
    started_at: datetime
    finished_at: datetime | None = None
    items_found: int = 0
    new_items: int = 0
    price_changes: int = 0
    message: str | None = None
    notifications: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "source_id": self.source_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "items_found": self.items_found,
            "new_items": self.new_items,
            "price_changes": self.price_changes,
            "message": self.message,
        }


def get_setting(db, key: str, default: str | None = None) -> str | None:
    row = db.get(Setting, key)
    return row.value if row else default


def get_poll_interval(db) -> int:
    raw = get_setting(db, SETTING_POLL_INTERVAL)
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL
    return max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, value))


def _upsert_setting(db, key: str, value: str) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value


def save_settings(db, *, poll_interval: int | None = None) -> None:
    if poll_interval is not None:
        interval = max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, poll_interval))
        _upsert_setting(db, SETTING_POLL_INTERVAL, str(interval))
    db.commit()


def notification_payload(notification: Notification) -> dict:
    return {
        "type": "notification",
        "source_id": notification.source_id,
        "notification": {
            "id": notification.id,
            "listing_id": notification.listing_id,
            "source_id": notification.source_id,
            "type": notification.type,
            "title": notification.title,
            "price": notification.price,
            "old_price": notification.old_price,
            "url": notification.url,
            "image": notification.image,
            "is_read": notification.is_read,
            "created_at": notification.created_at.isoformat(),
        },
    }


def build_stats(
    db,
    source_id: int | None,
    next_scan_at: datetime | None = None,
    running: bool = False,
) -> dict:
    since = utcnow() - timedelta(hours=24)
    source_filter = [] if source_id is None else [Listing.source_id == source_id]
    notif_filter = [] if source_id is None else [Notification.source_id == source_id]
    scan_filter = [] if source_id is None else [ScanLog.source_id == source_id]

    last_scan = db.scalar(
        select(ScanLog).where(*scan_filter).order_by(ScanLog.id.desc()).limit(1)
    )
    return {
        "total_listings": db.scalar(select(func.count(Listing.id)).where(*source_filter)) or 0,
        "active_listings": db.scalar(
            select(func.count(Listing.id)).where(*source_filter, Listing.is_active.is_(True))
        )
        or 0,
        "total_notifications": db.scalar(
            select(func.count(Notification.id)).where(*notif_filter)
        )
        or 0,
        "unread_notifications": db.scalar(
            select(func.count(Notification.id)).where(*notif_filter, Notification.is_read.is_(False))
        )
        or 0,
        "new_last_24h": db.scalar(
            select(func.count(Notification.id)).where(*notif_filter, Notification.created_at >= since)
        )
        or 0,
        "last_scan": last_scan,
        "next_scan_at": next_scan_at,
        "running": running,
        "poll_interval": get_poll_interval(db),
    }


def stats_payload(
    db,
    source_id: int | None,
    next_scan_at: datetime | None = None,
    running: bool = False,
    last_scan_override: dict | None = None,
) -> dict:
    stats = build_stats(db, source_id, next_scan_at, running)
    last_scan = last_scan_override if last_scan_override is not None else stats.get("last_scan")
    payload = {key: value for key, value in stats.items() if key != "last_scan"}
    if isinstance(last_scan, ScanLog):
        payload["last_scan"] = ScanOut.model_validate(last_scan).model_dump(mode="json")
    else:
        payload["last_scan"] = last_scan
    payload["source_id"] = source_id
    next_scan = payload.get("next_scan_at")
    payload["next_scan_at"] = next_scan.isoformat() if isinstance(next_scan, datetime) else None
    return payload


def _run_scan(source_id: int) -> ScanResult:
    db = SessionLocal()
    started = utcnow()
    source = db.get(Source, source_id)
    if source is None:
        db.close()
        return ScanResult(status="error", source_id=source_id, started_at=started, message="источник не найден")

    scan = ScanLog(started_at=started, status="running", source_id=source.id)
    db.add(scan)
    db.commit()
    db.refresh(scan)

    try:
        refresh_cookies()
        html = fetch_page(source.url)
        items = parse_listings(html, city_slug=source.city_slug, city_name=source.city_name or None)
        if not items:
            raise RuntimeError(
                f"в выдаче нет объявлений города «{source.city_name or source.city_slug}» "
                "(возможна блокировка Avito или пустая выдача)"
            )

        now = utcnow()
        seen: set[str] = set()
        payloads: list[dict] = []
        new_count = 0
        price_count = 0

        for raw in items:
            seen.add(raw.id)
            listing = db.scalar(
                select(Listing).where(Listing.id == raw.id, Listing.source_id == source.id)
            )
            if listing is None:
                if db.get(Listing, raw.id) is not None:
                    continue
                listing = Listing(
                    id=raw.id,
                    source_id=source.id,
                    url=raw.url,
                    title=raw.title,
                    price=raw.price,
                    image=raw.image,
                    location=raw.location,
                    date_text=raw.date_text,
                    published_at=raw.published_at,
                    first_seen=now,
                    last_seen=now,
                    is_active=True,
                )
                db.add(listing)
                db.flush()
                new_count += 1
                notification = Notification(
                    listing_id=listing.id,
                    source_id=source.id,
                    type="new_listing",
                    title=raw.title,
                    price=raw.price,
                    url=raw.url,
                    image=raw.image,
                )
                db.add(notification)
                db.flush()
                payloads.append(notification_payload(notification))
                continue

            price_changed = (
                raw.price is not None
                and listing.price is not None
                and raw.price != listing.price
            )
            if price_changed:
                price_count += 1
                notification = Notification(
                    listing_id=listing.id,
                    source_id=source.id,
                    type="price_change",
                    title=raw.title,
                    price=raw.price,
                    old_price=listing.price,
                    url=raw.url,
                    image=raw.image,
                )
                db.add(notification)
                db.flush()
                payloads.append(notification_payload(notification))
                listing.prev_price = listing.price
                listing.price_changes = (listing.price_changes or 0) + 1

            listing.url = raw.url
            if raw.title:
                listing.title = raw.title
            if raw.price is not None:
                listing.price = raw.price
            listing.image = raw.image or listing.image
            listing.location = raw.location or listing.location
            listing.date_text = raw.date_text or listing.date_text
            listing.published_at = raw.published_at or listing.published_at
            listing.last_seen = now
            listing.is_active = True
            listing.missed_scans = 0

        stale = db.scalars(
            select(Listing).where(
                Listing.source_id == source.id,
                Listing.is_active.is_(True),
                Listing.id.notin_(seen),
            )
        ).all()
        for row in stale:
            row.missed_scans = (row.missed_scans or 0) + 1
            if row.missed_scans >= 2:
                row.is_active = False

        scan.status = "ok"
        scan.items_found = len(items)
        scan.new_items = new_count
        scan.price_changes = price_count
        scan.message = None
        scan.finished_at = utcnow()
        db.commit()

        return ScanResult(
            status="ok",
            source_id=source.id,
            started_at=started,
            finished_at=scan.finished_at,
            items_found=len(items),
            new_items=new_count,
            price_changes=price_count,
            notifications=payloads,
        )
    except Exception as exc:
        log.warning("скан источника %s завершился ошибкой: %s", source.id, exc)
        db.rollback()
        scan.status = "error"
        scan.message = str(exc)[:2000]
        scan.finished_at = utcnow()
        db.commit()
        return ScanResult(
            status="error",
            source_id=source.id,
            started_at=started,
            finished_at=scan.finished_at,
            message=str(exc),
        )
    finally:
        db.close()


class Tracker:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._running = False
        self._next_scan_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def next_scan_at(self) -> datetime | None:
        return self._next_scan_at

    def schedule_next(self, interval: int) -> None:
        self._next_scan_at = utcnow() + timedelta(seconds=interval)

    def _sources(self, source_id: int | None) -> list[Source]:
        db = SessionLocal()
        try:
            if source_id is not None:
                source = db.get(Source, source_id)
                if source is None:
                    raise SourceNotFoundError(f"источник {source_id} не найден")
                return [source]
            return list(
                db.scalars(
                    select(Source).where(Source.is_enabled.is_(True)).order_by(Source.id)
                ).all()
            )
        finally:
            db.close()

    async def scan_once(self, reason: str = "manual", source_id: int | None = None) -> list[dict]:
        if self._running or self._lock.locked():
            raise ScanBusyError("скан уже выполняется")

        sources = self._sources(source_id)
        if not sources:
            return []

        results: list[dict] = []
        async with self._lock:
            self._running = True
            try:
                total = len(sources)
                for index, source in enumerate(sources, start=1):
                    await manager.broadcast(
                        {
                            "type": "scan_status",
                            "state": "running",
                            "reason": reason,
                            "source_id": source.id,
                            "current": index,
                            "total": total,
                        }
                    )
                    result = await asyncio.to_thread(_run_scan, source.id)
                    for payload in result.notifications:
                        await manager.broadcast(payload)

                    stats = self._stats_for(source.id, result.as_dict())
                    await manager.broadcast(
                        {
                            "type": "scan_finished",
                            "state": result.status,
                            "source_id": source.id,
                            "result": result.as_dict(),
                            "stats": stats,
                        }
                    )
                    results.append({"source_id": source.id, **result.as_dict()})

                    if index < total:
                        await asyncio.sleep(random.uniform(*SOURCE_SCAN_GAP_SECONDS))
            finally:
                self._running = False

        return results

    def _stats_for(self, source_id: int, last_scan: dict | None = None) -> dict:
        db = SessionLocal()
        try:
            return stats_payload(
                db,
                source_id,
                self._next_scan_at,
                self._running,
                last_scan_override=last_scan,
            )
        finally:
            db.close()


tracker = Tracker()


async def tracker_loop() -> None:
    await asyncio.sleep(2)
    while True:
        try:
            await tracker.scan_once(reason="interval")
        except ScanBusyError:
            pass
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("неожиданная ошибка фонового скана")

        db = SessionLocal()
        try:
            interval = get_poll_interval(db)
        finally:
            db.close()
        tracker.schedule_next(interval)
        await asyncio.sleep(interval)
