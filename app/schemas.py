from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ListingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    source_id: int | None = None
    url: str
    title: str
    price: int | None = None
    prev_price: int | None = None
    image: str | None = None
    location: str | None = None
    date_text: str | None = None
    published_at: datetime | None = None
    first_seen: datetime
    last_seen: datetime
    is_active: bool
    price_changes: int


class ListingPage(BaseModel):
    total: int
    items: list[ListingOut]


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    listing_id: str
    source_id: int | None = None
    type: str
    title: str
    price: int | None = None
    old_price: int | None = None
    url: str
    image: str | None = None
    is_read: bool
    created_at: datetime


class NotificationPage(BaseModel):
    total: int
    unread: int
    items: list[NotificationOut]


class ReadRequest(BaseModel):
    ids: list[int] | None = None
    source_id: int | None = None


class ReadResponse(BaseModel):
    updated: int
    unread: int


class ScanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_id: int | None = None
    started_at: datetime
    finished_at: datetime | None = None
    status: str
    items_found: int
    new_items: int
    price_changes: int
    message: str | None = None


class ScanRequest(BaseModel):
    source_id: int | None = None


class ScanResponse(BaseModel):
    results: list[dict]


class StatsOut(BaseModel):
    total_listings: int
    active_listings: int
    total_notifications: int
    unread_notifications: int
    new_last_24h: int
    last_scan: ScanOut | None = None
    next_scan_at: datetime | None = None
    running: bool = False
    poll_interval: int
    source_id: int | None = None


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    url: str
    city_slug: str
    city_name: str
    is_enabled: bool
    sort_by_date: bool = False
    created_at: datetime
    active_count: int = 0
    unread_count: int = 0


class SourceCreate(BaseModel):
    name: str = Field(default="", max_length=200)
    url: str = Field(min_length=10, max_length=1024)
    city_name: str = Field(default="", max_length=100)
    city_slug: str = Field(default="", max_length=64)
    sort_by_date: bool | None = None


class SourceUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    url: str | None = Field(default=None, min_length=10, max_length=1024)
    city_name: str | None = Field(default=None, max_length=100)
    city_slug: str | None = Field(default=None, max_length=64)
    is_enabled: bool | None = None
    sort_by_date: bool | None = None


class SettingsOut(BaseModel):
    poll_interval: int


class SettingsUpdate(BaseModel):
    poll_interval: int | None = Field(default=None, ge=60, le=86400)
