"""Кэш строк и заголовков листов на время прогона (меньше read-запросов к API)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from google_sheets.client import api_retry, canon_url, ensure_worksheet, get_worksheet
from google_sheets.calendar import (
    _last_date_column,
    _trim_trailing_empty_headers,
    _worksheet_resize_cols,
    build_header_date_map,
    format_header_date,
    today_moscow,
)
from google_sheets.iterations import get_logs_master_urls, sync_logs_sheet
from google_sheets.settings import ParserSettings


@dataclass
class UrlRowIndex:
    canon_to_row: dict[str, int] = field(default_factory=dict)
    max_row: int = 1

    @classmethod
    def from_master_urls(cls, urls: list[str]) -> UrlRowIndex:
        from google_sheets.link_index import FIRST_DATA_ROW

        idx = cls()
        for i, url in enumerate(urls):
            c = canon_url(url)
            if c:
                idx.canon_to_row[c] = i + FIRST_DATA_ROW
        idx.max_row = len(urls) + 1 if urls else 1
        return idx

    @classmethod
    def from_col_a(cls, col_a: list[str]) -> UrlRowIndex:
        idx = cls()
        for i, cell in enumerate(col_a, start=1):
            if i == 1:
                continue
            c = canon_url(cell)
            if c:
                idx.canon_to_row[c] = i
        idx.max_row = max(len(col_a), 1)
        return idx

    def row_for(self, url: str) -> int | None:
        return self.canon_to_row.get(canon_url(url))

    def find_row(self, url: str) -> int:
        c = canon_url(url)
        if c in self.canon_to_row:
            return self.canon_to_row[c]
        row = max(self.max_row, 1) + 1
        if self.max_row <= 1 and not self.canon_to_row:
            row = 2
        self.max_row = row
        if c:
            self.canon_to_row[c] = row
        return row


@dataclass
class CalendarWsCache:
    ws: Any
    settings: ParserSettings
    headers: list[str]
    col_by: dict[date, int]
    url_index: UrlRowIndex

    @classmethod
    def load(cls, sh: Any, settings: ParserSettings, *, availability: bool) -> CalendarWsCache:
        title = settings.sheet_availability if availability else settings.sheet_prices
        return cls.load_for_title(sh, settings, title)

    @classmethod
    def load_for_title(cls, sh: Any, settings: ParserSettings, title: str) -> CalendarWsCache:
        ws = get_worksheet(sh, title)
        if ws is None:
            ws = ensure_worksheet(sh, title, rows=3000, cols=26)

        def _headers() -> list[str]:
            return ws.row_values(1) or ["Объявление"]

        def _col_a() -> list[str]:
            return ws.col_values(1)

        headers = _trim_trailing_empty_headers(api_retry(_headers))
        if not headers or not headers[0].strip():
            headers = ["Объявление"]
        today = today_moscow()
        col_by = build_header_date_map(headers, today, settings.calendar_start_year)
        url_index = UrlRowIndex.from_col_a(api_retry(_col_a))
        return cls(ws=ws, settings=settings, headers=headers, col_by=col_by, url_index=url_index)

    def ensure_dates(self, needed_dates: set[date], today: date) -> dict[date, int]:
        from gspread.utils import rowcol_to_a1

        col_by = dict(self.col_by)
        existing_dates = set(col_by.keys())
        to_add = sorted(d for d in needed_dates if d not in existing_dates)
        if not to_add:
            return col_by

        new_headers = [format_header_date(d) for d in to_add]
        start_col = _last_date_column(col_by) + 1
        end_col = start_col + len(new_headers) - 1
        _worksheet_resize_cols(self.ws, end_col)
        h1 = rowcol_to_a1(1, start_col)
        h2 = rowcol_to_a1(1, end_col)

        def _add_cols() -> None:
            self.ws.update(f"{h1}:{h2}", [new_headers], value_input_option="USER_ENTERED")

        api_retry(_add_cols)
        self.headers = self.headers + new_headers
        self.col_by = build_header_date_map(
            self.headers, today, self.settings.calendar_start_year
        )
        return self.col_by

    def find_row(self, url: str) -> int:
        return self.url_index.find_row(url)


@dataclass
class ParseSheetContext:
    availability: CalendarWsCache
    prices: CalendarWsCache
    booking_dates: CalendarWsCache | None
    logs_ws: Any
    logs_index: UrlRowIndex
    settings: ParserSettings


_tls = threading.local()


def get_parse_sheet_context() -> ParseSheetContext | None:
    return getattr(_tls, "ctx", None)


def init_parse_sheet_context(
    sh: Any,
    settings: ParserSettings,
    *,
    log_urls: list[str] | None = None,
    skip_logs_ensure: bool = False,
) -> ParseSheetContext:
    urls = log_urls or []
    master = get_logs_master_urls() or urls
    if not skip_logs_ensure and master:
        sync_logs_sheet(sh, settings, master)
    logs_ws = ensure_worksheet(sh, settings.sheet_logs, rows=3000, cols=20)
    logs_index = (
        UrlRowIndex.from_master_urls(master)
        if master
        else UrlRowIndex.from_col_a(api_retry(lambda: logs_ws.col_values(1)))
    )
    availability = CalendarWsCache.load(sh, settings, availability=True)
    prices = CalendarWsCache.load(sh, settings, availability=False)
    booking_dates: CalendarWsCache | None = None
    if settings.run_calendar:
        booking_dates = CalendarWsCache.load_for_title(sh, settings, settings.sheet_booking_dates)
    ctx = ParseSheetContext(
        availability=availability,
        prices=prices,
        booking_dates=booking_dates,
        logs_ws=logs_ws,
        logs_index=logs_index,
        settings=settings,
    )
    _tls.ctx = ctx
    return ctx


def clear_parse_sheet_context() -> None:
    if hasattr(_tls, "ctx"):
        del _tls.ctx
