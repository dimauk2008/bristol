from collections.abc import Iterator

from sqlalchemy import create_engine, event, inspect, select, text, update
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import (
    CITIES,
    DB_PATH,
    DEFAULT_CITY_SLUG,
    DEFAULT_SEARCH_URL,
    DEFAULT_SOURCE_NAME,
)
from .urls import has_date_sort, set_date_sort

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _add_column_if_missing(table: str, column: str, decl: str) -> None:
    inspector = inspect(engine)
    columns = {item["name"] for item in inspector.get_columns(table)}
    if column not in columns:
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {decl}"))


def init_db() -> None:
    from . import models  # noqa: F401
    from .models import Listing, Notification, ScanLog, Source

    Base.metadata.create_all(engine)

    for table in ("listings", "notifications", "scans"):
        _add_column_if_missing(table, "source_id", "INTEGER REFERENCES sources(id)")
    _add_column_if_missing("listings", "missed_scans", "INTEGER NOT NULL DEFAULT 0")

    db = SessionLocal()
    try:
        source = db.scalar(select(Source).order_by(Source.id))
        if source is None:
            source = Source(
                name=DEFAULT_SOURCE_NAME,
                url=DEFAULT_SEARCH_URL,
                city_slug=DEFAULT_CITY_SLUG,
                city_name=CITIES.get(DEFAULT_CITY_SLUG, ""),
                is_enabled=True,
            )
            db.add(source)
            db.flush()

        for existing in db.scalars(select(Source)):
            if not has_date_sort(existing.url):
                existing.url = set_date_sort(existing.url, True)

        for model in (Listing, Notification, ScanLog):
            db.execute(update(model).where(model.source_id.is_(None)).values(source_id=source.id))

        db.execute(
            update(ScanLog)
            .where(ScanLog.status == "running")
            .values(status="error", message="скан прерван перезапуском приложения")
        )
        db.commit()
    finally:
        db.close()
