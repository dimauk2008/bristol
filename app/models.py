from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1024))
    city_slug: Mapped[str] = mapped_column(String(64), default="omsk")
    city_name: Mapped[str] = mapped_column(String(100), default="")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    listings: Mapped[list["Listing"]] = relationship(
        back_populates="source",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    scans: Mapped[list["ScanLog"]] = relationship(
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Listing(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("source_id", "id", name="uq_listing_source_item"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("sources.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    url: Mapped[str] = mapped_column(String(1024))
    title: Mapped[str] = mapped_column(String(512))
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prev_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    image: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    location: Mapped[str | None] = mapped_column(String(256), nullable=True)
    date_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    missed_scans: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    price_changes: Mapped[int] = mapped_column(Integer, default=0)

    source: Mapped[Source | None] = relationship(back_populates="listings")
    notifications: Mapped[list["Notification"]] = relationship(
        back_populates="listing",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    listing_id: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("listings.id", ondelete="CASCADE"),
        index=True,
    )
    source_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("sources.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    type: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(512))
    price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    old_price: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str] = mapped_column(String(1024))
    image: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)

    listing: Mapped[Listing] = relationship(back_populates="notifications")


class ScanLog(Base):
    __tablename__ = "scans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("sources.id", ondelete="CASCADE"),
        index=True,
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="running")
    items_found: Mapped[int] = mapped_column(Integer, default=0)
    new_items: Mapped[int] = mapped_column(Integer, default=0)
    price_changes: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
