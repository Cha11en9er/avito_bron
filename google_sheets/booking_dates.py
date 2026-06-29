"""Лист «даты бронирования»: когда впервые увидели бронь (0→1) по дню сдаваемости."""

from __future__ import annotations

from datetime import date
from typing import Any

from google_sheets.calendar import (
    _batch_updates_for_row,
    _ensure_row_with_url,
    _last_date_column,
    _read_row_values_by_date,
    init_calendar_sheet,
)
from google_sheets.client import api_retry, get_worksheet
from google_sheets.constants import NOT_FOUND_ON_SITE
from google_sheets.settings import ParserSettings


def format_booking_stamp(d: date) -> str:
    """Дата фиксации брони: ДД.ММ.ГГГГ."""
    return f"{d.day:02d}.{d.month:02d}.{d.year}"


def _is_booked_cell(val: str | None) -> bool:
    return (val or "").strip() == "1"


def compute_booking_date_changes(
    old_availability: dict[date, str],
    new_availability: dict[date, str],
    existing_booking_dates: dict[date, str],
    today: date,
) -> dict[date, str]:
    """
    0→1: поставить дату парсинга (today).
    1→0: стереть.
    1→1: не трогать (дата сохраняется).
    «нету на сайте» не влияет на даты брони (история сохраняется).
    """
    out: dict[date, str] = {}
    stamp = format_booking_stamp(today)
    for d, new_val in new_availability.items():
        if d < today:
            continue
        old_s = (old_availability.get(d) or "").strip()
        new_s = (new_val or "").strip()
        if old_s == NOT_FOUND_ON_SITE or new_s == NOT_FOUND_ON_SITE:
            continue
        old_b = _is_booked_cell(old_s)
        new_b = _is_booked_cell(new_s)
        if not old_b and new_b:
            out[d] = stamp
        elif old_b and not new_b:
            if (existing_booking_dates.get(d) or "").strip():
                out[d] = ""
    return out


def ensure_booking_dates_sheet(
    sh: Any,
    settings: ParserSettings,
    urls: list[str],
) -> Any:
    """Создать лист при отсутствии и дописать новые URL из «ссылки»."""
    from google_sheets.client import ensure_worksheet
    from google_sheets.links import ensure_urls_on_worksheet

    title = settings.sheet_booking_dates
    ws = get_worksheet(sh, title)
    if ws is None:
        if not urls:
            ws = ensure_worksheet(sh, title, rows=3000, cols=26)
            print(
                f"Лист «{title}» создан (пустой). "
                "Добавьте ссылки на лист «ссылки» или запустите sync_from_links_sheet=1."
            )
            return ws
        init_calendar_sheet(sh, settings, title, urls)
        print(f"Лист «{title}» создан: {len(urls)} строк (только URL; даты брони появятся при 0→1).")
        return get_worksheet(sh, title)

    if urls:
        added = ensure_urls_on_worksheet(ws, urls)
        if added:
            print(f"  «{title}»: +{added} строк с URL")
    return ws


def update_booking_dates_row(
    cache: Any,
    listing_url: str,
    old_availability: dict[date, str],
    new_availability: dict[date, str],
    col_by: dict[date, int],
    today: date,
) -> int:
    """Запись на лист «даты бронирования» после обновления сдаваемости. Возвращает число ячеек."""
    if not new_availability:
        return 0

    row = cache.find_row(listing_url)
    _ensure_row_with_url(
        cache.ws, row, listing_url, max(_last_date_column(col_by), len(cache.headers))
    )
    existing_booking = _read_row_values_by_date(cache.ws, row, col_by)
    changes = compute_booking_date_changes(
        old_availability, new_availability, existing_booking, today
    )
    if not changes:
        return 0

    updates = _batch_updates_for_row(row, col_by, changes, today)
    if not updates:
        return 0

    def _do() -> None:
        cache.ws.batch_update(updates, value_input_option="USER_ENTERED")

    api_retry(_do)
    return len(updates)
