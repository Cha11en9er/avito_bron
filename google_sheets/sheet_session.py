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
    open_calendar_ws,
    today_moscow,
)
from google_sheets.settings import ParserSettings


@dataclass
class UrlRowIndex:
    canon_to_row: dict[str, int] = field(default_factory=dict)
    max_row: int = 1

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
        ws = open_calendar_ws(sh, settings, availability=availability)
        headers = _trim_trailing_empty_headers(ws.row_values(1) or ["Объявление"])
        if not headers or not headers[0].strip():
            headers = ["Объявление"]
        today = today_moscow()
        col_by = build_header_date_map(headers, today, settings.calendar_start_year)
        url_index = UrlRowIndex.from_col_a(ws.col_values(1))
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
) -> ParseSheetContext:
    urls = log_urls or []
    ensure_logs_sheet(sh, settings, urls)
    logs_ws = ensure_worksheet(sh, settings.sheet_logs, rows=3000, cols=20)
    ctx = ParseSheetContext(
        availability=CalendarWsCache.load(sh, settings, availability=True),
        prices=CalendarWsCache.load(sh, settings, availability=False),
        logs_ws=logs_ws,
        logs_index=UrlRowIndex.from_col_a(logs_ws.col_values(1)),
        settings=settings,
    )
    _tls.ctx = ctx
    return ctx


def clear_parse_sheet_context() -> None:
    if hasattr(_tls, "ctx"):
        del _tls.ctx
