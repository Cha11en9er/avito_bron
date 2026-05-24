"""Синхронизация результатов парсинга с Google Таблицей."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google_sheets.calendar import today_moscow, update_calendar_sheet
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
    queue_next_index: int | None = None,
) -> tuple[bool, str]:
    today = today_moscow()
    write_detail = _should_write_detail(sh, settings, listing_url)
    ok = True
    log_status = "ок"

    if removed:
        if write_detail:
            write_not_found_detail_row(sh, settings, columns, listing_url)
        if settings.run_calendar:
            update_calendar_sheet(
                sh, settings, settings.sheet_availability, listing_url, {}, today,
                "availability", listing_removed=True,
            )
            update_calendar_sheet(
                sh, settings, settings.sheet_prices, listing_url, {}, today,
                "prices", listing_removed=True,
            )
        log_status = "нет на сайте"
    else:
        if write_detail:
            upsert_detail_row(sh, settings, record, columns, listing_url)
        if settings.run_calendar:
            av_n, _ = update_calendar_sheet(
                sh, settings, settings.sheet_availability, listing_url, booking_prices, today, "availability",
            )
            pr_n, _ = update_calendar_sheet(
                sh, settings, settings.sheet_prices, listing_url, booking_prices, today, "prices",
            )
            ok = av_n > 0 or pr_n > 0 or bool(booking_prices)
            if not ok:
                log_status = "фейл"

    if settings.run_calendar:
        write_log_entry(sh, settings, listing_url, status=log_status, ok=ok)

    if queue_next_index is not None:
        save_iteration_progress(sh, settings, queue_next_index)

    parts = []
    if write_detail:
        parts.append("деталь")
    if settings.run_calendar:
        parts.append("календарь")
    return ok, "+".join(parts) or "—"


def prepare_parse_session(
    base_dir: Path,
    export_columns: list[str],
) -> tuple[Any, ParserSettings, list[str], int, int]:
    sh = open_spreadsheet(base_dir)
    settings = load_settings(base_dir, sh)
    sh = open_spreadsheet(base_dir, sheet_id=settings.spreadsheet_id)
    settings = load_settings(base_dir, sh)

    sh, full_queue = build_parse_queue(base_dir, settings, export_columns)
    settings = load_settings(base_dir, sh)

    if settings.run_calendar and full_queue:
        ensure_logs_sheet(sh, settings, full_queue)

    settings = begin_iteration(sh, settings)
    queue, start_offset = slice_queue_for_resume(sh, full_queue, settings)

    return sh, settings, queue, start_offset, len(full_queue)


def finish_parse_session(
    sh: Any,
    settings: ParserSettings,
    *,
    full_queue_len: int,
    final_progress: int,
) -> None:
    """После прохода всей очереди итерации — увеличить номер итерации."""
    if full_queue_len > 0 and final_progress >= full_queue_len:
        complete_iteration(sh, settings, total_links=full_queue_len)


__all__ = [
    "bootstrap_google_sheet_mode",
    "finish_parse_session",
    "is_google_sheet_enabled",
    "is_listing_removed",
    "prepare_parse_session",
    "sync_after_listing",
]
