import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import CITIES, MAX_LISTINGS_LIMIT, MAX_NOTIFICATIONS_LIMIT, STATIC_DIR
from .database import SessionLocal, get_db, init_db
from .events import manager
from .models import Listing, Notification, ScanLog, Source, utcnow
from .schemas import (
    ListingOut,
    ListingPage,
    NotificationOut,
    NotificationPage,
    ReadRequest,
    ReadResponse,
    ScanOut,
    ScanRequest,
    ScanResponse,
    SettingsOut,
    SettingsUpdate,
    SourceCreate,
    SourceOut,
    SourceUpdate,
    StatsOut,
)
from .tracker import (
    ScanBusyError,
    SourceNotFoundError,
    get_poll_interval,
    save_settings,
    stats_payload,
    tracker,
    tracker_loop,
)
from .urls import has_date_sort, set_date_sort

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("avito.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    loop_task = asyncio.create_task(tracker_loop())
    log.info("трекер запущен")
    try:
        yield
    finally:
        loop_task.cancel()
        with suppress(asyncio.CancelledError):
            await loop_task
        log.info("трекер остановлен")


app = FastAPI(title="Avito Tracker", version="2.0.0", lifespan=lifespan)


def _current_stats(db: Session, source_id: int | None) -> StatsOut:
    return StatsOut.model_validate(
        stats_payload(db, source_id, tracker.next_scan_at, tracker.running)
    )


def _validate_url(url: str, date_sort: bool = True) -> tuple[str, str]:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="Ссылка должна начинаться с http:// или https://")
    if "avito.ru" not in url:
        raise HTTPException(status_code=422, detail="Поддерживаются только ссылки на avito.ru")
    segments = [part for part in urlsplit(url).path.split("/") if part]
    if not segments:
        raise HTTPException(status_code=422, detail="В ссылке нет города, например /omsk/...")
    return set_date_sort(url, date_sort), segments[0].lower()


def _source_out(db: Session, source: Source) -> SourceOut:
    active = db.scalar(
        select(func.count(Listing.id)).where(
            Listing.source_id == source.id, Listing.is_active.is_(True)
        )
    ) or 0
    unread = db.scalar(
        select(func.count(Notification.id)).where(
            Notification.source_id == source.id, Notification.is_read.is_(False)
        )
    ) or 0
    data = SourceOut.model_validate(source)
    data.active_count = active
    data.unread_count = unread
    data.sort_by_date = has_date_sort(source.url)
    return data


@app.get("/api/sources", response_model=list[SourceOut])
def list_sources(db: Session = Depends(get_db)) -> list[SourceOut]:
    rows = db.scalars(select(Source).order_by(Source.id)).all()
    return [_source_out(db, row) for row in rows]


@app.post("/api/sources", response_model=SourceOut)
def create_source(payload: SourceCreate, db: Session = Depends(get_db)) -> SourceOut:
    date_sort = True if payload.sort_by_date is None else payload.sort_by_date
    url, derived_slug = _validate_url(payload.url, date_sort)
    slug = (payload.city_slug or "").strip().lower() or derived_slug
    name = payload.name.strip() or CITIES.get(slug, slug)
    source = Source(
        name=name,
        url=url,
        city_slug=slug,
        city_name=(payload.city_name.strip() or CITIES.get(slug, "")),
        is_enabled=True,
    )
    db.add(source)
    db.commit()
    db.refresh(source)
    log.info("создан источник %s (%s)", source.id, source.url)
    return _source_out(db, source)


@app.put("/api/sources/{source_id}", response_model=SourceOut)
def update_source(
    source_id: int,
    payload: SourceUpdate,
    db: Session = Depends(get_db),
) -> SourceOut:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")

    if payload.url is not None:
        sort_flag = (
            payload.sort_by_date if payload.sort_by_date is not None else has_date_sort(source.url)
        )
        url, derived_slug = _validate_url(payload.url, sort_flag)
        source.url = url
        if payload.city_slug is None:
            source.city_slug = derived_slug
    elif payload.sort_by_date is not None:
        source.url = set_date_sort(source.url, payload.sort_by_date)
    if payload.city_slug is not None and payload.city_slug.strip():
        source.city_slug = payload.city_slug.strip().lower()
    if payload.name is not None:
        source.name = payload.name.strip() or source.name
    if payload.city_name is not None:
        source.city_name = payload.city_name.strip()
    if payload.is_enabled is not None:
        source.is_enabled = payload.is_enabled

    db.commit()
    db.refresh(source)
    return _source_out(db, source)


@app.delete("/api/sources/{source_id}")
def delete_source(source_id: int, db: Session = Depends(get_db)) -> dict:
    source = db.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Источник не найден")
    name = source.name
    db.delete(source)
    db.commit()
    log.info("удалён источник %s (%s)", source_id, name)
    return {"deleted": source_id}


FRESH_WINDOWS: dict[str, int] = {
    "24h": 24 * 3600,
    "3d": 3 * 24 * 3600,
    "7d": 7 * 24 * 3600,
}


@app.get("/api/listings", response_model=ListingPage)
def list_listings(
    db: Session = Depends(get_db),
    source_id: int | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    active: bool = Query(default=True),
    fresh: str = Query(default="24h", pattern="^(all|24h|3d|7d)$"),
    sort: str = Query(default="recent", pattern="^(recent|published|price_asc|price_desc|title)$"),
    limit: int = Query(default=100, ge=1, le=MAX_LISTINGS_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> ListingPage:
    query = select(Listing)
    if source_id is not None:
        query = query.where(Listing.source_id == source_id)
    if active:
        query = query.where(Listing.is_active.is_(True))
    if q:
        query = query.where(Listing.title.ilike(f"%{q.strip()}%"))
    if fresh in FRESH_WINDOWS:
        cutoff = utcnow() - timedelta(seconds=FRESH_WINDOWS[fresh])
        query = query.where(func.coalesce(Listing.published_at, Listing.first_seen) >= cutoff)

    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0

    order = {
        "recent": Listing.first_seen.desc(),
        "published": Listing.published_at.desc().nullslast(),
        "price_asc": Listing.price.asc().nulls_last(),
        "price_desc": Listing.price.desc().nulls_last(),
        "title": Listing.title.asc(),
    }[sort]
    rows = db.scalars(query.order_by(order, Listing.id).offset(offset).limit(limit)).all()
    return ListingPage(total=total, items=[ListingOut.model_validate(row) for row in rows])


@app.get("/api/notifications", response_model=NotificationPage)
def list_notifications(
    db: Session = Depends(get_db),
    source_id: int | None = Query(default=None),
    unread: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=MAX_NOTIFICATIONS_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> NotificationPage:
    query = select(Notification)
    if source_id is not None:
        query = query.where(Notification.source_id == source_id)
    if unread:
        query = query.where(Notification.is_read.is_(False))
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0

    unread_query = select(func.count(Notification.id)).where(Notification.is_read.is_(False))
    if source_id is not None:
        unread_query = unread_query.where(Notification.source_id == source_id)
    unread_count = db.scalar(unread_query) or 0

    rows = db.scalars(
        query.order_by(Notification.created_at.desc(), Notification.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return NotificationPage(
        total=total,
        unread=unread_count,
        items=[NotificationOut.model_validate(row) for row in rows],
    )


@app.post("/api/notifications/read", response_model=ReadResponse)
def mark_notifications_read(payload: ReadRequest, db: Session = Depends(get_db)) -> ReadResponse:
    statement = select(Notification).where(Notification.is_read.is_(False))
    if payload.ids:
        statement = statement.where(Notification.id.in_(payload.ids))
    if payload.source_id is not None:
        statement = statement.where(Notification.source_id == payload.source_id)
    rows = db.scalars(statement).all()
    for row in rows:
        row.is_read = True
    db.commit()

    unread_query = select(func.count(Notification.id)).where(Notification.is_read.is_(False))
    if payload.source_id is not None:
        unread_query = unread_query.where(Notification.source_id == payload.source_id)
    return ReadResponse(updated=len(rows), unread=db.scalar(unread_query) or 0)


@app.get("/api/scans", response_model=list[ScanOut])
def list_scans(
    db: Session = Depends(get_db),
    source_id: int | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
) -> list[ScanOut]:
    query = select(ScanLog)
    if source_id is not None:
        query = query.where(ScanLog.source_id == source_id)
    rows = db.scalars(query.order_by(ScanLog.id.desc()).limit(limit)).all()
    return [ScanOut.model_validate(row) for row in rows]


@app.get("/api/stats", response_model=StatsOut)
def get_stats(
    db: Session = Depends(get_db),
    source_id: int | None = Query(default=None),
) -> StatsOut:
    return _current_stats(db, source_id)


@app.get("/api/settings", response_model=SettingsOut)
def read_settings(db: Session = Depends(get_db)) -> SettingsOut:
    return SettingsOut(poll_interval=get_poll_interval(db))


@app.put("/api/settings", response_model=SettingsOut)
def update_settings(payload: SettingsUpdate, db: Session = Depends(get_db)) -> SettingsOut:
    save_settings(db, poll_interval=payload.poll_interval)
    return SettingsOut(poll_interval=get_poll_interval(db))


@app.post("/api/scan", response_model=ScanResponse)
async def trigger_scan(payload: ScanRequest | None = None) -> ScanResponse:
    source_id = payload.source_id if payload else None
    try:
        results = await tracker.scan_once(reason="manual", source_id=source_id)
    except ScanBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ScanResponse(results=results)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    db = next(get_db())
    try:
        await websocket.send_json(
            {
                "type": "hello",
                "stats": stats_payload(db, None, tracker.next_scan_at, tracker.running),
                "clients": manager.count,
            }
        )
        while True:
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_json({"type": "pong", "ts": utcnow().isoformat()})
    except WebSocketDisconnect:
        pass
    finally:
        manager.disconnect(websocket)
        db.close()


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
