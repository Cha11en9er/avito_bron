"""Данные для синхронизации одного объявления с Google Таблицей."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass
class PendingListingSync:
    record: dict[str, object]
    columns: list[str]
    booking_prices: dict[str, str]
    listing_url: str
    removed: bool = False
    queue_next_row: int | None = None
    availability_days: dict[date, str] | None = None
    sheet_today: date | None = None
    attempts: int = 0
