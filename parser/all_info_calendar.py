"""
Тестовый прогон calendar + Google Sheet (те же 3 тестовые ссылки).

Основной парсер (очередь из таблицы, деталь + телефон + calendar):
  python -m parser

Запасная версия (карусель брони):
  python -m parser.all_info_carousel
"""

from __future__ import annotations

import re
import sys
import time
import traceback
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from parser.all_info import (
    _launch_browser,
    _new_page,
    _process_one_url,
    _setup_playwright_env,
)
from parser.paths import project_root

try:
    from google_sheets import is_listing_removed
except ImportError:

    def is_listing_removed(title: str, record: dict | None = None) -> bool:  # type: ignore[misc]
        return "не посмотреть" in (title or "").lower()


TEST_URLS: list[str] = [
    "https://www.avito.ru/mozhaysk/doma_dachi_kottedzhi/4-k._dom_95_m_7895273150"
    "?guestsDetailed=%7B%22version%22%3A1%2C%22totalCount%22%3A2%2C%22adultsCount%22%3A2%2C%22children%22%3A%5B%5D%7D"
    "&calendar=true",
    "https://www.avito.ru/mozhaysk/doma_dachi_kottedzhi/3-k._dom_80_m_7949696746"
    "?guestsDetailed=%7B%22version%22%3A1%2C%22totalCount%22%3A2%2C%22adultsCount%22%3A2%2C%22children%22%3A%5B%5D%7D"
    "&calendar=true",
    "https://www.avito.ru/mozhaysk/doma_dachi_kottedzhi/3-k._dom_95_m_3273004270"
    "?guestsDetailed=%7B%22version%22%3A1%2C%22totalCount%22%3A2%2C%22adultsCount%22%3A2%2C%22children%22%3A%5B%5D%7D"
    "&calendar=true",
]

def _console_utf8() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _extract_id_from_url(url: str) -> str:
    m = re.search(r"_(\d{8,})(?:\?|$)", url)
    if m:
        return m.group(1)
    m = re.search(r"/(\d{8,})(?:\?|$)", url)
    if m:
        return m.group(1)
    return "unknown"


_MONTH_TITLE = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)

_REPORT_LINE = "─" * 56


def _fmt_day(d: date) -> str:
    return d.strftime("%d.%m")


def _fmt_day_range(start: date, end: date) -> str:
    if start == end:
        return _fmt_day(start)
    return f"{_fmt_day(start)} – {_fmt_day(end)}"


def _merge_consecutive_days(dates: list[date]) -> list[tuple[date, date]]:
    if not dates:
        return []
    sorted_days = sorted(dates)
    ranges: list[tuple[date, date]] = []
    start = end = sorted_days[0]
    for d in sorted_days[1:]:
        if (d - end).days == 1:
            end = d
        else:
            ranges.append((start, end))
            start = end = d
    ranges.append((start, end))
    return ranges


def _parse_iso_period_label(period: str) -> str:
    if "--" not in period or not period[:4].isdigit():
        return period
    left, right = period.split("--", 1)
    try:
        d1 = datetime.strptime(left.strip(), "%Y-%m-%d").date()
        d2 = datetime.strptime(right.strip(), "%Y-%m-%d").date()
        return _fmt_day_range(d1, d2)
    except ValueError:
        return period


def _format_price_value(raw: str) -> str:
    try:
        from google_sheets.constants import BOOKED_SLOT_MARKER
    except ImportError:
        BOOKED_SLOT_MARKER = "__booked__"
    if raw == BOOKED_SLOT_MARKER:
        return "занято"
    return raw


def _lines_availability(availability_days: dict[date, str]) -> list[str]:
    if not availability_days:
        return ["  (нет данных — календарь не открыт или пусто)"]

    by_month: dict[tuple[int, int], dict[str, list[date]]] = defaultdict(
        lambda: {"0": [], "1": []}
    )
    for d, state in availability_days.items():
        if state in ("0", "1"):
            by_month[(d.year, d.month)][state].append(d)

    lines: list[str] = []
    free_total = booked_total = 0
    for year, month in sorted(by_month):
        title = _MONTH_TITLE[month] if month < len(_MONTH_TITLE) else str(month)
        lines.append(f"  {title} {year}:")
        block = by_month[(year, month)]
        for label, state, marker in (
            ("свободно", "0", "○"),
            ("занято", "1", "●"),
        ):
            days = block[state]
            if not days:
                continue
            if state == "0":
                free_total += len(days)
            else:
                booked_total += len(days)
            ranges = _merge_consecutive_days(days)
            chunks = [_fmt_day_range(s, e) for s, e in ranges]
            lines.append(f"    {marker} {label} ({len(days)} дн.): {', '.join(chunks)}")
    lines.append(f"  Итого: свободно {free_total}, занято {booked_total}")
    return lines


def _lines_prices(booking_prices: dict[str, str]) -> list[str]:
    if not booking_prices:
        return ["  (нет данных — карусель «ближайшие даты» пуста)"]

    def sort_key(item: tuple[str, str]) -> str:
        period, _ = item
        if "--" in period and period[:4].isdigit():
            return period.split("--", 1)[0]
        return period

    lines: list[str] = []
    for period, price in sorted(booking_prices.items(), key=sort_key):
        label = _parse_iso_period_label(period)
        value = _format_price_value(price)
        lines.append(f"  {label:<22}  {value}")
    lines.append(f"  Итого периодов: {len(booking_prices)}")
    return lines


def _print_listing_report(
    *,
    idx: int,
    total: int,
    item_id: str,
    url: str,
    availability_days: dict[date, str],
    booking_prices: dict[str, str],
) -> None:
    print()
    print(_REPORT_LINE)
    print(f"[{idx}/{total}] id={item_id}")
    print()
    print("Ссылка:")
    print(f"  {url}")
    print()
    print("Брони (календарь, 2 месяца):")
    for line in _lines_availability(availability_days):
        print(line)
    print()
    print("Цены (карусель «ближайшие даты»):")
    for line in _lines_prices(booking_prices):
        print(line)
    print(_REPORT_LINE)
    print()


def _sync_sheet(
    sh: Any,
    settings: Any,
    url: str,
    availability_days: dict,
    booking_prices: dict[str, str],
    *,
    removed: bool,
    queue_next_index: int | None,
) -> None:
    from google_sheets.sync import sync_after_listing_calendar

    sync_after_listing_calendar(
        sh,
        settings,
        url,
        availability_days,
        booking_prices,
        removed=removed,
        queue_next_index=queue_next_index,
    )




def main(urls: list[str] | None = None) -> None:
    _setup_playwright_env()
    base_dir = project_root()
    queue = list(urls or TEST_URLS)
    if not queue:
        print("Список URL пуст.")
        return

    use_sheet = False
    sh = None
    settings = None
    start_index = 0
    full_queue_len = len(queue)
    try:
        from google_sheets import bootstrap_google_sheet_mode, is_google_sheet_enabled
        from google_sheets.sync import prepare_parse_session

        if is_google_sheet_enabled() and bootstrap_google_sheet_mode(base_dir) == "sheet":
            sh, settings, queue_from_sheet, start_index, full_queue_len = prepare_parse_session(
                base_dir, []
            )
            if queue_from_sheet:
                queue = queue_from_sheet
            use_sheet = True
            from google_sheets.link_index import index_to_row, last_data_row

            print(
                f"Режим calendar + Google Sheet: {len(queue)} ссылок "
                f"(итерация {settings.parse_iteration}, со строки "
                f"{index_to_row(start_index)} из {last_data_row(full_queue_len)})."
            )
        else:
            print(f"Режим calendar (тест): {len(queue)} ссылок, без таблицы.")
    except ImportError:
        print(f"Режим calendar (тест): {len(queue)} ссылок.")

    from google_sheets.calendar import today_moscow
    from google_sheets.parse_day import ParseDaySession

    day_session = ParseDaySession(iteration_day=today_moscow())
    print(f"День начала прогона (MSK): {day_session.iteration_day.isoformat()}")

    ok = 0
    with sync_playwright() as p:
        browser = _launch_browser(p)
        context, page = _new_page(browser)
        try:
            from google_sheets.link_index import index_to_row, last_data_row

            last_row = last_data_row(full_queue_len)
            for batch_pos, url in enumerate(queue, start=1):
                sheet_row = index_to_row(start_index + batch_pos - 1)
                try:
                    rec = _process_one_url(
                        page,
                        base_dir,
                        url,
                        sheet_row,
                        last_row,
                        sh=sh if use_sheet else None,
                        settings=settings if use_sheet else None,
                        queue_next_row=sheet_row + 1 if use_sheet else None,
                        day_session=day_session,
                    )
                    if rec is not None:
                        ok += 1
                except Exception as exc:
                    print(f"  ошибка: {exc}")
                    traceback.print_exc()
            if use_sheet and sh is not None and settings is not None:
                try:
                    from google_sheets.sync import finish_parse_session

                    final = index_to_row(start_index + ok)
                    finish_parse_session(
                        sh,
                        settings,
                        full_queue_len=full_queue_len,
                        final_progress=final,
                    )
                except Exception:
                    pass
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass

    print(f"Готово: {ok}/{len(queue)}.")


if __name__ == "__main__":
    _console_utf8()
    try:
        main()
    except Exception:
        print("Произошла ошибка:")
        traceback.print_exc()
        sys.exit(1)
