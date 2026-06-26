"""Листы «сдаваемость по дням» и «цены по дням»."""

from __future__ import annotations

import calendar as cal_mod
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google_sheets.client import api_retry, ensure_worksheet, find_row_by_url_col_a, get_worksheet
from google_sheets.constants import (
    BOOKED_SLOT_MARKER,
    NOT_FOUND_ON_SITE,
    RE_DATA_ID_PERIOD,
    RE_PERIOD,
    RE_PERIOD_CROSS_MONTH,
    format_header_date,
    parse_header_date,
)
from google_sheets.settings import ParserSettings


def tz_moscow() -> ZoneInfo:
    try:
        return ZoneInfo("Europe/Moscow")
    except Exception as exc:
        raise RuntimeError(
            "Не найдена зона Europe/Moscow. На Windows: pip install tzdata"
        ) from exc


def today_moscow() -> date:
    return datetime.now(tz_moscow()).date()


def calendar_window_months(today: date) -> set[tuple[int, int]]:
    """Два месяца datepicker: текущий и следующий (без листания дальше)."""
    y, m = today.year, today.month
    if m < 12:
        return {(y, m), (y, m + 1)}
    return {(y, m), (y + 1, 1)}


def calendar_window_end(today: date) -> date:
    """Последний день следующего месяца от today."""
    y, m = today.year, today.month
    if m < 12:
        ny, nm = y, m + 1
    else:
        ny, nm = y + 1, 1
    return date(ny, nm, cal_mod.monthrange(ny, nm)[1])


def filter_availability_day_map(day_map: dict[date, str], cutoff: date) -> dict[date, str]:
    """Только даты >= cutoff (месяцы — из datepicker на карточке, не фильтруем здесь)."""
    return {d: v for d, v in (day_map or {}).items() if d >= cutoff}


def month_from_russian(word: str) -> int | None:
    w = (word or "").lower().strip()
    pairs = (
        ("январ", 1),
        ("феврал", 2),
        ("марта", 3),
        ("март", 3),
        ("апрел", 4),
        ("мая", 5),
        ("май", 5),
        ("июня", 6),
        ("июн", 6),
        ("июля", 7),
        ("июл", 7),
        ("август", 8),
        ("сентяб", 9),
        ("октяб", 10),
        ("нояб", 11),
        ("декаб", 12),
    )
    for pref, num in pairs:
        if w.startswith(pref):
            return num
    return None


def parse_period_dates(label: str, year: int) -> tuple[date, date] | None:
    text = (label or "").strip()
    m = RE_DATA_ID_PERIOD.match(text)
    if m:
        try:
            return date.fromisoformat(m.group(1)), date.fromisoformat(m.group(2))
        except ValueError:
            return None

    m = RE_PERIOD_CROSS_MONTH.match(text)
    if m:
        d1, mon1, d2, mon2 = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        try:
            return date(year, mon1, d1), date(year, mon2, d2)
        except ValueError:
            return None

    m = RE_PERIOD.match(text)
    if not m:
        return None
    d1, d2 = int(m.group(1)), int(m.group(2))
    mon = month_from_russian(m.group(3))
    if mon is None:
        return None
    try:
        return date(year, mon, d1), date(year, mon, d2)
    except ValueError:
        return None


def resolve_period_year(label: str, today: date) -> int:
    candidates: list[tuple[int, date]] = []
    for y in (today.year - 1, today.year, today.year + 1):
        pr = parse_period_dates(label, y)
        if pr:
            candidates.append((y, pr[0]))
    if not candidates:
        return today.year
    recent = [(y, s) for y, s in candidates if s >= today - timedelta(days=21)]
    if recent:
        return min(recent, key=lambda x: abs((x[1] - today).days))[0]
    future = [c for c in candidates if c[1] >= today]
    if future:
        return min(future, key=lambda x: x[1])[0]
    return max(candidates, key=lambda x: x[1])[0]


def parse_booking_interval(label: str, today: date) -> tuple[date, date] | None:
    y = resolve_period_year(label, today)
    pr = parse_period_dates(label, y)
    if not pr:
        return None
    s, e = pr
    if e <= s:
        return None
    return s, e


def header_year_for_calendar(today: date, header_samples: list[date], fallback_year: int) -> int:
    y = today.year
    if not header_samples:
        return fallback_year
    for hd in header_samples[:3]:
        try:
            dt = date(y, hd.month, hd.day)
        except ValueError:
            continue
        if dt < today - timedelta(days=200):
            return today.year + 1
    return y


def build_header_date_map(headers: list[str], today: date, fallback_year: int) -> dict[date, int]:
    samples: list[date] = []
    for h in headers[1:]:
        d0 = parse_header_date(h, today.year)
        if d0:
            samples.append(d0)
    year = header_year_for_calendar(today, samples, fallback_year)
    col_by: dict[date, int] = {}
    for j, h in enumerate(headers):
        if j == 0:
            continue
        d = parse_header_date(h, year)
        if d:
            col_by[d] = j + 1
    return col_by


def collect_booking_slots(
    booking: dict[str, str], today: date
) -> list[tuple[date, date, str, bool]]:
    """
    Интервалы с сайта: (начало, конец, цена, занято).
    В таблице: 0 — нет брони (свободно), 1 — есть бронь (занято).
    """
    out: list[tuple[date, date, str, bool]] = []
    for label, price in (booking or {}).items():
        pr = parse_booking_interval(label, today)
        if not pr:
            continue
        s, e = pr
        raw = str(price or "").strip()
        booked = raw == BOOKED_SLOT_MARKER
        p = "" if booked else raw
        out.append((s, e, p, booked))
    out.sort(key=lambda x: x[0])
    return out


def nights_in_interval(s: date, e: date, today: date) -> list[date]:
    last_in = e - timedelta(days=1)
    d = s
    out: list[date] = []
    while d <= last_in:
        if d >= today:
            out.append(d)
        d += timedelta(days=1)
    return out


def build_day_updates_from_slots(
    slots: list[tuple[date, date, str, bool]], today: date
) -> tuple[dict[date, str], dict[date, str]]:
    """
    0 — свободно (слот с ценой), 1 — занято (слот без цены или пропуск до более поздних дат).
    Цена — только односуточный интервал, в ячейку дня заезда.
    """
    free_days: set[date] = set()
    booked_days: set[date] = set()

    for s, e, price, booked in slots:
        nights = nights_in_interval(s, e, today)
        if booked:
            booked_days.update(nights)
        else:
            free_days.update(nights)

    explicit = free_days | booked_days
    if not explicit:
        return {}, {}

    max_d = max(explicit)
    availability: dict[date, str] = {}
    d = today
    while d <= max_d:
        if d in free_days:
            availability[d] = "0"
        elif d in booked_days:
            availability[d] = "1"
        elif any(x > d for x in explicit):
            availability[d] = "1"
        d += timedelta(days=1)

    prices: dict[date, str] = {}
    for s, e, price, booked in slots:
        if booked or not price or s < today:
            continue
        if (e - s).days != 1:
            continue
        prices[s] = price

    return availability, prices


def dates_from_slots(slots: list[tuple[date, date, str, bool]], today: date) -> set[date]:
    av, pr = build_day_updates_from_slots(slots, today)
    return set(av.keys()) | set(pr.keys())


def _trim_trailing_empty_headers(headers: list[str]) -> list[str]:
    h = list(headers)
    while len(h) > 1 and not (h[-1] or "").strip():
        h.pop()
    return h


def _last_date_column(col_by: dict[date, int]) -> int:
    return max(col_by.values()) if col_by else 1


def _ensure_row_with_url(ws: Any, row: int, url: str, num_cols: int) -> None:
    row_vals = ws.row_values(row)
    from google_sheets.client import canon_url

    if len(row_vals) < 1 or canon_url(row_vals[0]) != canon_url(url):
        pad = max(0, num_cols - 1)
        ws.update(f"A{row}", [[url] + [""] * pad], value_input_option="USER_ENTERED")


def _worksheet_resize_cols(ws: Any, min_cols: int) -> None:
    cur = getattr(ws, "col_count", 0) or 0
    if cur >= min_cols:
        return
    rows = getattr(ws, "row_count", 3000) or 3000

    def _resize() -> None:
        ws.resize(rows=rows, cols=min_cols)

    api_retry(_resize)


def ensure_calendar_headers(
    ws: Any,
    needed_dates: set[date],
    today: date,
    settings: ParserSettings,
) -> dict[date, int]:
    """Добавить столбцы только для новых дат (после последнего существующего)."""
    from gspread.utils import rowcol_to_a1

    rows = ws.get_all_values()
    headers = _trim_trailing_empty_headers(rows[0] if rows else ["Объявление"])
    if not headers or not headers[0].strip():
        headers = ["Объявление"]

    col_by = build_header_date_map(headers, today, settings.calendar_start_year)
    existing_dates = set(col_by.keys())

    to_add = sorted(d for d in needed_dates if d not in existing_dates)
    if to_add:
        new_headers = [format_header_date(d) for d in to_add]
        start_col = _last_date_column(col_by) + 1
        end_col = start_col + len(new_headers) - 1
        _worksheet_resize_cols(ws, end_col)
        h1 = rowcol_to_a1(1, start_col)
        h2 = rowcol_to_a1(1, end_col)

        def _add_cols() -> None:
            ws.update(f"{h1}:{h2}", [new_headers], value_input_option="USER_ENTERED")

        api_retry(_add_cols)
        headers = headers + new_headers
        col_by = build_header_date_map(headers, today, settings.calendar_start_year)

    return col_by


def init_calendar_sheet(
    sh: Any,
    settings: ParserSettings,
    sheet_title: str,
    urls: list[str],
) -> None:
    from gspread.utils import rowcol_to_a1

    ws = ensure_worksheet(sh, sheet_title, rows=max(3000, len(urls) + 10), cols=26)
    header = ["Объявление"]
    body = [header]
    for url in urls:
        body.append([url.strip()])

    def _write() -> None:
        ws.clear()
        end_cell = rowcol_to_a1(len(body), len(header))
        ws.update(f"A1:{end_cell}", body, value_input_option="USER_ENTERED")

    api_retry(_write)
    print(
        f"Лист «{sheet_title}» создан: {len(urls)} строк; "
        "столбцы дат добавляются при парсинге."
    )


def open_calendar_ws(sh: Any, settings: ParserSettings, *, availability: bool) -> Any:
    title = settings.sheet_availability if availability else settings.sheet_prices
    ws = get_worksheet(sh, title)
    if ws is None:
        ws = ensure_worksheet(sh, title, rows=3000, cols=26)
    return ws


def removed_listing_dates(today: date, forward_days: int | None = None) -> set[date]:
    """Дни для «нету на сайте»: от today до конца следующего месяца (как у живого datepicker)."""
    if forward_days is not None:
        n = max(1, forward_days)
        return {today + timedelta(days=i) for i in range(n)}
    end = calendar_window_end(today)
    out: set[date] = set()
    d = today
    while d <= end:
        out.add(d)
        d += timedelta(days=1)
    return out


def _read_row_values_by_date(
    ws: Any, row: int, col_by_date: dict[date, int]
) -> dict[date, str]:
    """Текущие значения ячеек строки по датам (из заголовка)."""

    def _read() -> list[str]:
        return ws.row_values(row)

    row_vals = api_retry(_read)
    out: dict[date, str] = {}
    for d, col in col_by_date.items():
        idx = col - 1
        if idx < len(row_vals):
            out[d] = (row_vals[idx] or "").strip()
    return out


def _extend_live_day_map_after_not_found(
    day_map: dict[date, str],
    col_by_date: dict[date, int],
    existing: dict[date, str],
    today: date,
) -> dict[date, str]:
    """
    Объявление снова на сайте: с today — 0/1/цена/пусто;
    даты < today с «нету на сайте» в day_map не попадают (сохраняются на листе).
    """
    out = dict(day_map)
    for d in col_by_date:
        if d < today:
            continue
        if existing.get(d) != NOT_FOUND_ON_SITE:
            continue
        if d not in out:
            out[d] = ""
    return out


def _batch_updates_for_row(
    row: int,
    col_by_date: dict[date, int],
    day_values: dict[date, str],
    today: date,
    *,
    allow_past: bool = False,
    existing_by_date: dict[date, str] | None = None,
) -> list[dict[str, Any]]:
    from gspread.utils import rowcol_to_a1

    body: list[dict[str, Any]] = []
    for d, val in day_values.items():
        if d < today:
            if not allow_past:
                continue
            if existing_by_date and existing_by_date.get(d) == NOT_FOUND_ON_SITE:
                continue
        col = col_by_date.get(d)
        if not col:
            continue
        a1 = rowcol_to_a1(row, col)
        body.append({"range": a1, "values": [[val]]})
    return body


def _update_availability_cached(
    cache: Any,
    listing_url: str,
    day_map: dict[date, str],
    today: date,
) -> tuple[int, str]:
    ws = cache.ws
    headers = cache.headers
    if not day_map:
        return 0, "нет дней в календаре"

    col_by = cache.ensure_dates(set(day_map.keys()), today)
    row = cache.find_row(listing_url)
    _ensure_row_with_url(ws, row, listing_url, max(_last_date_column(col_by), len(headers)))

    existing_by_date = _read_row_values_by_date(ws, row, col_by)
    values = _extend_live_day_map_after_not_found(day_map, col_by, existing_by_date, today)

    updates = _batch_updates_for_row(
        row, col_by, values, today, existing_by_date=existing_by_date
    )
    if not updates:
        return 0, "нечего писать"

    def _do_batch() -> None:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    api_retry(_do_batch)
    return len(updates), "ок"


def _update_calendar_sheet_cached(
    cache: Any,
    listing_url: str,
    booking: dict[str, str],
    today: date,
    mode: str,
    *,
    listing_removed: bool = False,
) -> tuple[int, str]:
    availability = mode == "availability"
    ws = cache.ws
    headers = cache.headers
    slots = collect_booking_slots(booking, today)

    if listing_removed:
        needed_removed = removed_listing_dates(today)
        col_by = cache.ensure_dates(needed_removed, today)
        if not col_by:
            return 0, "нет столбцов дат"
        day_map = {d: NOT_FOUND_ON_SITE for d in needed_removed if d in col_by}
    elif not slots:
        return 0, "нет слотов брони"
    else:
        av_map, pr_map = build_day_updates_from_slots(slots, today)
        day_map = av_map if availability else pr_map
        if not day_map:
            return 0, "нечего писать"
        needed = dates_from_slots(slots, today)
        col_by = cache.ensure_dates(needed, today)

    row = cache.find_row(listing_url)
    _ensure_row_with_url(ws, row, listing_url, max(_last_date_column(col_by), len(headers)))

    existing_by_date = _read_row_values_by_date(ws, row, col_by)
    if not listing_removed:
        day_map = _extend_live_day_map_after_not_found(
            day_map, col_by, existing_by_date, today
        )

    updates = _batch_updates_for_row(
        row,
        col_by,
        day_map,
        today,
        allow_past=listing_removed,
        existing_by_date=existing_by_date,
    )
    if not updates:
        return 0, "нечего писать"

    def _do_batch() -> None:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    api_retry(_do_batch)
    return len(updates), "ок"


def update_availability_day_map(
    sh: Any,
    settings: ParserSettings,
    listing_url: str,
    day_map: dict[date, str],
    today: date,
) -> tuple[int, str]:
    """
    Запись сдаваемости из datepicker (0/1).
    Обновляются только столбцы с датой >= today: прошлые дни в листе не перезаписываются
    (в календаре на сайте они выглядят «занято», но это не снимает старые 0 в таблице).
    """
    from google_sheets.sheet_session import get_parse_sheet_context

    ctx = get_parse_sheet_context()
    if ctx is not None:
        return _update_availability_cached(ctx.availability, listing_url, day_map, today)

    ws = open_calendar_ws(sh, settings, availability=True)
    rows = ws.get_all_values()
    if not rows:
        return 0, "лист пуст"

    headers = rows[0] or ["Объявление"]
    if not day_map:
        return 0, "нет дней в календаре"

    col_by = ensure_calendar_headers(ws, set(day_map.keys()), today, settings)
    row = find_row_by_url_col_a(ws, listing_url)
    _ensure_row_with_url(ws, row, listing_url, max(_last_date_column(col_by), len(headers)))

    existing_by_date = _read_row_values_by_date(ws, row, col_by)
    values = _extend_live_day_map_after_not_found(day_map, col_by, existing_by_date, today)

    updates = _batch_updates_for_row(
        row, col_by, values, today, existing_by_date=existing_by_date
    )
    if not updates:
        return 0, "нечего писать"

    def _do_batch() -> None:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    api_retry(_do_batch)
    return len(updates), "ок"


def update_calendar_sheet(
    sh: Any,
    settings: ParserSettings,
    sheet_title: str,
    listing_url: str,
    booking: dict[str, str],
    today: date,
    mode: str,
    *,
    listing_removed: bool = False,
) -> tuple[int, str]:
    availability = mode == "availability"
    from google_sheets.sheet_session import get_parse_sheet_context

    ctx = get_parse_sheet_context()
    if ctx is not None:
        cache = ctx.availability if availability else ctx.prices
        return _update_calendar_sheet_cached(
            cache,
            listing_url,
            booking,
            today,
            mode,
            listing_removed=listing_removed,
        )

    ws = open_calendar_ws(sh, settings, availability=availability)
    rows = ws.get_all_values()
    if not rows:
        return 0, "лист пуст"

    headers = rows[0] or ["Объявление"]
    slots = collect_booking_slots(booking, today)

    if listing_removed:
        needed_removed = removed_listing_dates(today)
        col_by = ensure_calendar_headers(ws, needed_removed, today, settings)
        if not col_by:
            return 0, "нет столбцов дат"
        day_map = {
            d: NOT_FOUND_ON_SITE for d in needed_removed if d in col_by
        }
    elif not slots:
        return 0, "нет слотов брони"
    else:
        av_map, pr_map = build_day_updates_from_slots(slots, today)
        day_map = av_map if availability else pr_map
        if not day_map:
            return 0, "нечего писать"

    needed = dates_from_slots(slots, today) if slots and not listing_removed else set(day_map.keys())
    col_by = ensure_calendar_headers(ws, needed, today, settings)

    row = find_row_by_url_col_a(ws, listing_url)
    _ensure_row_with_url(ws, row, listing_url, max(_last_date_column(col_by), len(headers)))

    existing_by_date = _read_row_values_by_date(ws, row, col_by)
    if not listing_removed:
        day_map = _extend_live_day_map_after_not_found(
            day_map, col_by, existing_by_date, today
        )

    updates = _batch_updates_for_row(
        row,
        col_by,
        day_map,
        today,
        allow_past=listing_removed,
        existing_by_date=existing_by_date,
    )
    if not updates:
        return 0, "нечего писать"

    def _do_batch() -> None:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    api_retry(_do_batch)
    return len(updates), "ок"
