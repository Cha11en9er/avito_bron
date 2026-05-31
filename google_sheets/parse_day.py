"""Смена календарного дня во время длинной итерации (вечер → после полуночи)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from google_sheets.calendar import (
    filter_availability_day_map,
    nights_in_interval,
    parse_booking_interval,
    today_moscow,
)
from google_sheets.client import canon_url

__all__ = [
    "ParseDaySession",
    "filter_availability_day_map",
    "filter_booking_prices",
    "write_cutoff_for_listing",
]


def write_cutoff_for_listing(day_at_start: date) -> date:
    """
    Дата, с которой можно писать в лист для этой ссылки.
    Если обработка пересекла полночь — берётся новый день (прошлые столбцы не трогаем).
    """
    return max(day_at_start, today_moscow())


def filter_booking_prices(booking: dict[str, str], cutoff: date) -> dict[str, str]:
    """Оставить в карусели только периоды с ночами >= cutoff."""
    if not booking:
        return {}
    out: dict[str, str] = {}
    for label, price in booking.items():
        pr = parse_booking_interval(label, cutoff)
        if not pr:
            continue
        s, e = pr
        if nights_in_interval(s, e, cutoff):
            out[label] = price
    return out


@dataclass
class ParseDaySession:
    """Состояние итерации: день старта и уже записанные URL."""

    iteration_day: date
    midnight_crossed: bool = False
    urls_completed: dict[str, date] = field(default_factory=dict)

    def note_url_completed(self, listing_url: str, write_cutoff: date) -> None:
        key = canon_url(listing_url)
        self.urls_completed[key] = write_cutoff
        if write_cutoff > self.iteration_day:
            self.midnight_crossed = True

    def should_skip_repeat(self, listing_url: str) -> bool:
        """
        После полуночи не обрабатывать повторно ссылки, уже записанные до смены дня
        (защита при длинном прогоне / сбое посреди очереди).
        """
        if not self.midnight_crossed:
            return False
        key = canon_url(listing_url)
        prev = self.urls_completed.get(key)
        if prev is None:
            return False
        now = today_moscow()
        return now > self.iteration_day and prev <= self.iteration_day
