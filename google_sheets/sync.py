"""Синхронизация результатов парсинга с Google Таблицей."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from google_sheets.calendar import (
    filter_availability_day_map,
    today_moscow,
    update_availability_day_map,
    update_calendar_sheet,
)
from google_sheets.parse_day import filter_booking_prices
from google_sheets.client import bootstrap_google_sheet_mode, canon_url, is_google_sheet_enabled, open_spreadsheet
from google_sheets.constants import REMOVED_TITLE_MARKERS
from google_sheets.detail import (
    detail_title_by_url,
    title_needs_parse,
    upsert_detail_row,
    write_not_found_detail_row,
)
from google_sheets.iterations import (
    begin_iteration,
    complete_iteration,
    ensure_logs_sheet,
    save_iteration_progress,
    slice_queue_for_resume,
    write_log_entry,
)
from google_sheets.links import build_parse_queue
from google_sheets.settings import ParserSettings, load_settings


def is_listing_removed(title: str, record: dict[str, object] | None = None) -> bool:
    t = (title or "").strip().lower()
    for marker in REMOVED_TITLE_MARKERS:
        if marker in t:
            return True
    if record:
        name = str(record.get("название") or "").strip().lower()
        if "не посмотреть" in name:
            return True
    return False


def _should_write_detail(sh: Any, settings: ParserSettings, listing_url: str) -> bool:
    if not settings.run_detail:
        return False
    if settings.detail_fill_mode in ("primary", "rebuild"):
        return True
    if not settings.detail_only_empty_title:
        return True
    titles = detail_title_by_url(sh, settings)
    return title_needs_parse(titles.get(canon_url(listing_url), ""))


def sync_after_listing(
    sh: Any,
    settings: ParserSettings,
    record: dict[str, object],
    columns: list[str],
    booking_prices: dict[str, str],
    listing_url: str,
    *,
    removed: bool = False,
    queue_next_index: int | None = None,  # номер следующей ссылки (1-based)
    availability_days: dict[date, str] | None = None,
    today: date | None = None,
) -> tuple[bool, str]:
    """Синхронизация после карточки.

    Если передан availability_days — сдаваемость из datepicker (day_map только >= today).
    Иначе — карусель «ближайшие даты» (запасной парсер all_info_carousel).
    """
    sheet_today = today if today is not None else today_moscow()
    if availability_days is not None:
        availability_days = filter_availability_day_map(availability_days, sheet_today)
    booking_prices = filter_booking_prices(booking_prices, sheet_today)
    write_detail = _should_write_detail(sh, settings, listing_url)
    ok = True
    log_status = "ок"
    use_datepicker = availability_days is not None

    if removed:
        if write_detail:
            write_not_found_detail_row(sh, settings, columns, listing_url)
        if settings.run_calendar:
            update_calendar_sheet(
                sh, settings, settings.sheet_availability, listing_url, {}, sheet_today,
                "availability", listing_removed=True,
            )
            update_calendar_sheet(
                sh, settings, settings.sheet_prices, listing_url, {}, sheet_today,
                "prices", listing_removed=True,
            )
        log_status = "нет на сайте"
    else:
        if write_detail:
            upsert_detail_row(sh, settings, record, columns, listing_url)
        if settings.run_calendar:
            if use_datepicker:
                av_n, _ = update_availability_day_map(
                    sh, settings, listing_url, availability_days or {}, sheet_today
                )
            else:
                av_n, _ = update_calendar_sheet(
                    sh,
                    settings,
                    settings.sheet_availability,
                    listing_url,
                    booking_prices,
                    sheet_today,
                    "availability",
                )
            pr_n, _ = update_calendar_sheet(
                sh, settings, settings.sheet_prices, listing_url, booking_prices, sheet_today, "prices",
            )
            ok = av_n > 0 or pr_n > 0 or bool(booking_prices) or bool(availability_days)
            if not ok:
                log_status = "фейл"

    if settings.run_calendar:
        write_log_entry(sh, settings, listing_url, status=log_status, ok=ok)

    if queue_next_index is not None:
        from google_sheets.iterations import current_queue_len

        save_iteration_progress(
            sh, settings, queue_next_index, total_urls=current_queue_len()
        )

    parts = []
    if write_detail:
        parts.append("деталь")
    if settings.run_calendar:
        parts.append("календарь")
    return ok, "+".join(parts) or "—"


def sync_after_listing_calendar(
    sh: Any,
    settings: ParserSettings,
    listing_url: str,
    availability_days: dict[date, str],
    booking_prices: dict[str, str],
    *,
    removed: bool = False,
    queue_next_index: int | None = None,  # номер следующей ссылки (1-based)
    today: date | None = None,
) -> tuple[bool, str]:
    """Сдаваемость из datepicker (2 мес.), цены — из карусели «ближайшие даты»."""
    sheet_today = today if today is not None else today_moscow()
    availability_days = filter_availability_day_map(availability_days, sheet_today)
    booking_prices = filter_booking_prices(booking_prices, sheet_today)
    ok = True
    log_status = "ок"

    if removed:
        if settings.run_calendar:
            update_calendar_sheet(
                sh, settings, settings.sheet_availability, listing_url, {}, sheet_today,
                "availability", listing_removed=True,
            )
            update_calendar_sheet(
                sh, settings, settings.sheet_prices, listing_url, {}, sheet_today,
                "prices", listing_removed=True,
            )
        log_status = "нет на сайте"
    elif settings.run_calendar:
        av_n, _ = update_availability_day_map(
            sh, settings, listing_url, availability_days, sheet_today
        )
        pr_n, _ = update_calendar_sheet(
            sh, settings, settings.sheet_prices, listing_url, booking_prices, sheet_today, "prices",
        )
        ok = av_n > 0 or pr_n > 0 or bool(booking_prices) or bool(availability_days)
        if not ok:
            log_status = "фейл"

    if settings.run_calendar:
        write_log_entry(sh, settings, listing_url, status=log_status, ok=ok)

    if queue_next_index is not None:
        from google_sheets.iterations import current_queue_len

        save_iteration_progress(
            sh, settings, queue_next_index, total_urls=current_queue_len()
        )

    return ok, "календарь+цены" if settings.run_calendar else "—"


def prepare_parse_session(
    base_dir: Path,
    export_columns: list[str],
) -> tuple[Any, ParserSettings, list[str], int, int]:
    sh = open_spreadsheet(base_dir)
    settings = load_settings(base_dir, sh)
    sh = open_spreadsheet(base_dir)
    settings = load_settings(base_dir, sh)

    sh, full_queue = build_parse_queue(base_dir, settings, export_columns)
    settings = load_settings(base_dir, sh)

    if settings.run_calendar and full_queue:
        ensure_logs_sheet(sh, settings, full_queue)

    settings = begin_iteration(sh, settings, urls=full_queue if full_queue else None)
    if settings.run_calendar and full_queue:
        ensure_logs_sheet(sh, settings, full_queue)

    from google_sheets.iterations import set_queue_len

    set_queue_len(len(full_queue))
    queue, start_offset, settings = slice_queue_for_resume(sh, full_queue, settings)

    return sh, settings, queue, start_offset, len(full_queue)


def finish_parse_session(
    sh: Any,
    settings: ParserSettings,
    *,
    full_queue_len: int,
    final_progress: int,
) -> None:
    """После прохода всей очереди — final_progress = следующая строка листа."""
    from google_sheets.link_index import last_data_row

    if full_queue_len > 0 and final_progress > last_data_row(full_queue_len):
        complete_iteration(sh, settings, total_links=full_queue_len)


__all__ = [
    "bootstrap_google_sheet_mode",
    "finish_parse_session",
    "is_google_sheet_enabled",
    "is_listing_removed",
    "prepare_parse_session",
    "sync_after_listing",
]
