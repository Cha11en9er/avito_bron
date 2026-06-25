"""Пакетная запись в Google Таблицу после N ссылок (пауза — без лишних запросов к Avito)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any


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


@dataclass
class SheetSyncBuffer:
    sh: Any
    settings: Any
    batch_size: int = 10
    pause_s: float = 8.0
    _pending: list[PendingListingSync] = field(default_factory=list)
    last_add_flushed: bool = False

    def __len__(self) -> int:
        return len(self._pending)

    def add(self, item: PendingListingSync) -> None:
        self.last_add_flushed = False
        self._pending.append(item)
        if len(self._pending) >= self.batch_size:
            self.flush(force=True)
            self.last_add_flushed = True

    def flush(self, *, force: bool = False) -> int:
        if not self._pending:
            return 0
        if not force and len(self._pending) < self.batch_size:
            return 0

        from google_sheets.sync import sync_after_listing

        n = len(self._pending)
        print(f"Пауза {self.pause_s:.0f} с → запись в таблицу ({n} ссылок)…")
        time.sleep(self.pause_s)

        ok_count = 0
        for i, item in enumerate(self._pending):
            is_last = i == n - 1
            try:
                ok, _ = sync_after_listing(
                    self.sh,
                    self.settings,
                    item.record,
                    item.columns,
                    item.booking_prices,
                    item.listing_url,
                    removed=item.removed,
                    queue_next_index=item.queue_next_row if is_last else None,
                    availability_days=item.availability_days if not item.removed else None,
                    today=item.sheet_today,
                    save_progress=is_last,
                )
                if ok:
                    ok_count += 1
            except Exception as exc:
                print(f"  ошибка записи {item.listing_url[:60]}…: {exc}")

        print(f"  таблица: {ok_count}/{n} ок")
        self._pending.clear()
        return n
