"""Константы и форматы Google Таблицы."""

from __future__ import annotations

import re
from datetime import date

DEFAULT_SPREADSHEET_ID = "1vRF638f9LOKc8qkwTnjevUNYzT3VTKa4TY_kbULxjOg"
DEFAULT_WORKSHEET_DETAIL = "детальная информация"
DEFAULT_WORKSHEET_AVAILABILITY = "сдаваемость по дням"
DEFAULT_WORKSHEET_PRICES_DAYS = "цены по дням"
DEFAULT_WORKSHEET_BOOKING_DATES = "даты бронирования"
DEFAULT_WORKSHEET_LINKS = "ссылки"
DEFAULT_WORKSHEET_SETTINGS = "настройки"
DEFAULT_WORKSHEET_LOGS = "логи ежедневного парсинга"
DEFAULT_WORKSHEET_KADASTR = "детальная информация с кадастрами"

NOT_FOUND_ON_SITE = "нету на сайте"
# Снято с сайта: окно datepicker = от today до конца следующего месяца (см. removed_listing_dates).
# Маркер в booking[период]: слот на сайте без цены (занято / есть бронь)
BOOKED_SLOT_MARKER = "__booked__"
LOG_STATUS_OK = "ок"
LOG_STATUS_FAIL = "фейл"
LOG_STATUS_NOT_FOUND = "нет на сайте"

RE_PERIOD = re.compile(r"^(\d{1,2})-(\d{1,2})\s+(.+)$", re.UNICODE)
RE_PERIOD_CROSS_MONTH = re.compile(
    r"^(\d{1,2})\.(\d{1,2})-(\d{1,2})\.(\d{1,2})$", re.UNICODE
)
RE_DATA_ID_PERIOD = re.compile(r"^(\d{4}-\d{2}-\d{2})--(\d{4}-\d{2}-\d{2})$")
RE_HEADER_DAY = re.compile(r"^(\d{1,2})\.(\d{1,2})$")

DETAIL_SHEET_EXCLUDE_COLUMNS = frozenset({"характеристики json", "цены по датам", "ссылка"})

RE_DETAIL_M2 = re.compile(r"(\d+(?:[.,]\d+)?)\s*м[²2]?", re.UNICODE)
RE_DETAIL_SOTKI = re.compile(r"(\d+(?:[.,]\d+)?)\s*сот", re.UNICODE)
RE_DETAIL_RUB = re.compile(r"([\d\s\u00a0]+)\s*₽", re.UNICODE)
RE_ADDR_TRAIL_KM = re.compile(r",\s*(\d+)\s*км\s*$", re.UNICODE | re.IGNORECASE)
RE_ADDR_HIGHWAY = re.compile(
    r"\s+((?:[А-ЯЁа-яёA-Za-z][\w\u0400-\u04ff\.\-]*\s+)+шоссе)\s*$",
    re.UNICODE | re.IGNORECASE,
)

DETAIL_NUMERIC_COLUMNS = frozenset(
    {"площадь дома", "площадь участка", "этажей", "залог", "кол-во гостей"}
)

REMOVED_TITLE_MARKERS = (
    "объявление не посмотреть",
    "объявление снято",
    "объявление не найдено",
    "объявление истекло",
    "объявление удалено",
    "пользователь его удалил",
    "страница не найдена",
)


def format_header_date(d: date) -> str:
    return f"{d.day:02d}.{d.month:02d}"


def parse_header_date(cell: str, year: int) -> date | None:
    m = RE_HEADER_DAY.match((cell or "").strip())
    if not m:
        return None
    day, month = int(m.group(1)), int(m.group(2))
    try:
        return date(year, month, day)
    except ValueError:
        return None
