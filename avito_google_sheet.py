"""
Google Таблица: детальная информация + листы «сдаваемость по дням» и «цены по дням».

.env (рядом со скриптами):
  GOOGLE_CREDENTIALS_JSON или GOOGLE_APPLICATION_CREDENTIALS — JSON сервисного аккаунта
  AVITO_GOOGLE_SHEET_ID — id таблицы (есть значение по умолчанию)
  AVITO_GOOGLE_SHEET_TAB — лист «детальная информация» (по умолчанию)
  AVITO_GOOGLE_SHEET_AVAILABILITY — «сдаваемость по дням»
  AVITO_GOOGLE_SHEET_PRICES_DAYS — «цены по дням»
  AVITO_GOOGLE_SHEET_LINKS — лист со списком URL для обхода (по умолчанию «ссылки»)

Включение: AVITO_GOOGLE_SHEET=1 (avito-houses-parser_all_info_sheets.py).
  Ссылки — лист AVITO_GOOGLE_SHEET_LINKS; перед парсингом URL дописываются на все рабочие листы,
  в обход попадают только объявления без заполненного «название» на «детальная информация».

Лист «детальная информация»: в A — URL; строка 1 — «Объявление» + поля с B (без «характеристики json»,
  «цены по датам», «ссылка»). «адрес» → адрес + шоссе + километраж; площади, этажи, залог, гости — числа.

Сдаваемость: 0 — ночь есть в карусели брони на сайте; 1 — занято (между окнами, до первого окна
  или если на сайте нет ни одного интервала). Лист цен — одноночные интервалы (d2 = d1+1).
Имена листов должны совпадать с настройками посимвольно (скрипт не создаёт листы сам).
Строка объявления ищется по URL в столбце A со 2-й строки; если нет — строка дописывается.
В ячейки с датой >= сегодня (Europe/Moscow) пишутся только новые значения; прошлые даты не трогаем.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


def _tz_moscow() -> ZoneInfo:
    """На Windows без пакета tzdata ZoneInfo('Europe/Moscow') падает — см. requirements.txt."""
    try:
        return ZoneInfo("Europe/Moscow")
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(
            "Не найдена зона Europe/Moscow (IANA). На Windows установите: pip install tzdata"
        ) from exc

DEFAULT_SPREADSHEET_ID = "1vRF638f9LOKc8qkwTnjevUNYzT3VTKa4TY_kbULxjOg"
DEFAULT_WORKSHEET_DETAIL = "детальная информация"
DEFAULT_WORKSHEET_AVAILABILITY = "сдаваемость по дням"
DEFAULT_WORKSHEET_PRICES_DAYS = "цены по дням"
DEFAULT_WORKSHEET_LINKS = "ссылки"

NOT_FOUND_ON_SITE = "нету на сайте"

_RE_PERIOD = re.compile(r"^(\d{1,2})-(\d{1,2})\s+(.+)$", re.UNICODE)
_RE_HEADER_DAY = re.compile(r"^(\d{1,2})\.(\d{1,2})$")

# Не выводим на лист «детальная информация» (Excel/JSON без изменений).
DETAIL_SHEET_EXCLUDE_COLUMNS = frozenset({"характеристики json", "цены по датам", "ссылка"})

_RE_DETAIL_M2 = re.compile(r"(\d+(?:[.,]\d+)?)\s*м[²2]?", re.UNICODE)
_RE_DETAIL_SOTKI = re.compile(r"(\d+(?:[.,]\d+)?)\s*сот", re.UNICODE)
_RE_DETAIL_RUB = re.compile(r"([\d\s\u00a0]+)\s*₽", re.UNICODE)
_RE_ADDR_TRAIL_KM = re.compile(r",\s*(\d+)\s*км\s*$", re.UNICODE | re.IGNORECASE)
_RE_ADDR_HIGHWAY = re.compile(
    r"\s+((?:[А-ЯЁа-яёA-Za-z][\w\u0400-\u04ff\.\-]*\s+)+шоссе)\s*$",
    re.UNICODE | re.IGNORECASE,
)

_DETAIL_NUMERIC_COLUMNS = frozenset(
    {"площадь дома", "площадь участка", "этажей", "залог", "кол-во гостей"}
)


def _split_address_for_sheet(raw: str) -> tuple[str, str, str | int]:
    """Адрес → (локация без шоссе, шоссе, километраж)."""
    s = (raw or "").strip()
    if not s:
        return "", "", ""

    km_val: str | int = ""
    m_km = _RE_ADDR_TRAIL_KM.search(s)
    if m_km:
        km_val = int(m_km.group(1))
        s = s[: m_km.start()].strip()

    m_hw = _RE_ADDR_HIGHWAY.search(s)
    if m_hw:
        highway = m_hw.group(1).strip()
        addr = s[: m_hw.start()].strip().rstrip(",")
        return addr, highway, km_val

    return s, "", km_val


def _digits_only_int(text: str) -> int | None:
    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    return int(digits)


def _parse_detail_numeric(column: str, value: object) -> object:
    """Числа для листа «детальная информация»; пустая строка, если не распознано."""
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    if column == "площадь дома":
        m = _RE_DETAIL_M2.search(s)
        if m:
            return _digits_only_int(m.group(1)) or ""
    elif column == "площадь участка":
        m = _RE_DETAIL_SOTKI.search(s)
        if m:
            return _digits_only_int(m.group(1)) or ""
    elif column == "залог":
        m = _RE_DETAIL_RUB.search(s)
        if m:
            return _digits_only_int(m.group(1)) or ""
    elif column in ("этажей", "кол-во гостей"):
        n = _digits_only_int(s)
        if n is not None:
            return n

    return ""


def _clean_detail_price(value: object) -> str:
    s = str(value or "").strip()
    idx = s.find("window.")
    if idx >= 0:
        s = s[:idx].strip()
    return s


def _detail_field_value(
    column: str,
    record: dict[str, object],
    *,
    addr_parts: tuple[str, str, str | int] | None = None,
) -> object:
    if column == "адрес":
        parts = addr_parts or _split_address_for_sheet(str(record.get("адрес") or ""))
        return parts[0]
    if column == "шоссе":
        parts = addr_parts or _split_address_for_sheet(str(record.get("адрес") or ""))
        return parts[1]
    if column == "километраж":
        parts = addr_parts or _split_address_for_sheet(str(record.get("адрес") or ""))
        return parts[2]
    if column == "цена":
        return _clean_detail_price(record.get("цена"))
    if column in _DETAIL_NUMERIC_COLUMNS:
        return _parse_detail_numeric(column, record.get(column))
    return _cell_value(record.get(column))


def detail_sheet_columns(columns: list[str]) -> list[str]:
    """Колонки листа «детальная информация» без тяжёлых/дублирующих полей."""
    out: list[str] = []
    for col in columns:
        if col in DETAIL_SHEET_EXCLUDE_COLUMNS:
            continue
        if col == "адрес":
            out.extend(["адрес", "шоссе", "километраж"])
        else:
            out.append(col)
    return out


def _load_dotenv(base_dir: Path) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(base_dir / ".env")
    except ImportError:
        pass


def _env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return (v or "").strip().strip('"').strip("'")


def is_google_sheet_enabled() -> bool:
    return _env("AVITO_GOOGLE_SHEET").lower() in ("1", "true", "yes", "on")


def bootstrap_google_sheet_mode(base_dir: Path) -> str:
    """
    Включает Google Таблицу, если есть ключ сервисного аккаунта (и не задано AVITO_GOOGLE_SHEET=0).
    Возвращает «sheet» или «urls_file».
    """
    _load_dotenv(base_dir)
    flag = _env("AVITO_GOOGLE_SHEET").lower()
    if flag in ("0", "false", "no", "off"):
        return "urls_file"
    if is_google_sheet_enabled():
        return "sheet"

    for key in ("GOOGLE_CREDENTIALS_JSON", "GOOGLE_APPLICATION_CREDENTIALS"):
        p = _env(key)
        if not p:
            continue
        path = Path(p)
        if not path.is_absolute():
            path = base_dir / p
        if path.is_file():
            os.environ["AVITO_GOOGLE_SHEET"] = "1"
            return "sheet"

    default_cred = base_dir / "service_account.json"
    if default_cred.is_file():
        os.environ["AVITO_GOOGLE_SHEET"] = "1"
        if not _env("GOOGLE_CREDENTIALS_JSON"):
            os.environ["GOOGLE_CREDENTIALS_JSON"] = "service_account.json"
        return "sheet"

    return "urls_file"


def _credentials_path(base_dir: Path) -> Path:
    p = _env("GOOGLE_CREDENTIALS_JSON", _env("GOOGLE_APPLICATION_CREDENTIALS"))
    if not p:
        raise RuntimeError(
            "Нет пути к JSON ключу сервисного аккаунта. Создайте файл рядом со скриптом "
            "(например google-service-account.json) и в .env укажите:\n"
            "  GOOGLE_CREDENTIALS_JSON=google-service-account.json\n"
            "или полный путь. Альтернатива: переменная GOOGLE_APPLICATION_CREDENTIALS."
        )
    path = Path(p)
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        raise FileNotFoundError(f"Файл ключа сервисного аккаунта не найден: {path}")
    return path


def _spreadsheet_id() -> str:
    return _env("AVITO_GOOGLE_SHEET_ID", DEFAULT_SPREADSHEET_ID)


def _sheet_detail() -> str:
    return _env("AVITO_GOOGLE_SHEET_TAB", DEFAULT_WORKSHEET_DETAIL)


def _sheet_availability() -> str:
    return _env("AVITO_GOOGLE_SHEET_AVAILABILITY", DEFAULT_WORKSHEET_AVAILABILITY)


def _sheet_prices_days() -> str:
    return _env("AVITO_GOOGLE_SHEET_PRICES_DAYS", DEFAULT_WORKSHEET_PRICES_DAYS)


def _sheet_links() -> str:
    return _env("AVITO_GOOGLE_SHEET_LINKS", DEFAULT_WORKSHEET_LINKS)


def load_urls_from_links_sheet(base_dir: Path) -> list[str]:
    """URL из столбца A листа «ссылки» (A1 пропускаем — заголовок), со 2-й строки."""
    import gspread
    from gspread.exceptions import WorksheetNotFound

    _load_dotenv(base_dir)
    cred_path = _credentials_path(base_dir)
    gc = gspread.service_account(filename=str(cred_path), scopes=list(_SCOPES))
    sh = gc.open_by_key(_spreadsheet_id())
    title = _sheet_links()
    try:
        ws = sh.worksheet(title)
    except WorksheetNotFound as exc:
        raise RuntimeError(
            f'В таблице нет листа «{title}». Создайте его или задайте AVITO_GOOGLE_SHEET_LINKS.'
        ) from exc

    col_a = ws.col_values(1)
    if len(col_a) <= 1:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for cell in col_a[1:]:
        line = (cell or "").strip()
        if not line or line.startswith("#"):
            continue
        if not line.lower().startswith("http"):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    print(f"Ссылки из Google Таблицы, лист «{title}»: {len(out)} шт.")
    return out


def _open_spreadsheet(base_dir: Path) -> Any:
    import gspread

    _load_dotenv(base_dir)
    cred_path = _credentials_path(base_dir)
    gc = gspread.service_account(filename=str(cred_path), scopes=list(_SCOPES))
    return gc.open_by_key(_spreadsheet_id())


def _header_column_index(headers: list[str], name: str) -> int | None:
    target = (name or "").strip().lower()
    for i, h in enumerate(headers):
        if (h or "").strip().lower() == target:
            return i
    return None


def _detail_sheet_rows(sh: Any) -> tuple[list[list[str]], int] | None:
    """Строки листа и индекс столбца «название»; None, если лист пуст или нет колонки."""
    ws = _open_ws(sh, _sheet_detail())
    rows = ws.get_all_values()
    if not rows:
        return None
    headers = [str(h or "").strip() for h in rows[0]]
    title_idx = _header_column_index(headers, "название")
    if title_idx is None:
        return None
    return rows, title_idx


def _detail_title_by_url(sh: Any) -> dict[str, str]:
    """Канонический URL → значение столбца «название» на листе «детальная информация»."""
    parsed = _detail_sheet_rows(sh)
    if not parsed:
        return {}
    rows, idx = parsed

    out: dict[str, str] = {}
    for row in rows[1:]:
        if not row:
            continue
        canon = _canon_url(row[0] if row else "")
        if not canon:
            continue
        title = row[idx].strip() if idx < len(row) else ""
        out[canon] = title
    return out


def _urls_without_title_on_detail(sh: Any) -> list[str]:
    """URL из столбца A «детальная информация», у которых пустое «название» (срыв парса / обрыв сети)."""
    parsed = _detail_sheet_rows(sh)
    if not parsed:
        return []
    rows, idx = parsed

    out: list[str] = []
    seen: set[str] = set()
    for row in rows[1:]:
        url = (row[0] if row else "").strip()
        if not url.lower().startswith("http"):
            continue
        title = row[idx].strip() if idx < len(row) else ""
        if not _title_needs_parse(title):
            continue
        canon = _canon_url(url)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(url)
    return out


def _merge_url_lists(*groups: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for url in group:
            canon = _canon_url(url)
            if not canon or canon in seen:
                continue
            seen.add(canon)
            out.append(url.strip())
    return out


def _title_needs_parse(title: str) -> bool:
    t = (title or "").strip()
    return not t or t == NOT_FOUND_ON_SITE


def _ensure_urls_on_worksheet(ws: Any, urls: list[str]) -> int:
    """Добавляет строки только с URL в столбце A, если объявления ещё нет на листе."""
    col_a = ws.col_values(1)
    existing = {_canon_url(c) for c in col_a[1:] if c}
    headers = ws.row_values(1) or ["Объявление"]
    num_cols = max(len(headers), 1)

    to_add: list[list[str]] = []
    for url in urls:
        canon = _canon_url(url)
        if not canon or canon in existing:
            continue
        to_add.append([url.strip()] + [""] * (num_cols - 1))
        existing.add(canon)

    if not to_add:
        return 0

    def _do() -> None:
        ws.append_rows(to_add, value_input_option="USER_ENTERED")

    _api_retry(_do)
    return len(to_add)


def ensure_urls_on_all_sheets(base_dir: Path, urls: list[str], *, sh: Any | None = None) -> Any:
    """Переносит URL с листа «ссылки» на «детальная информация», «сдаваемость», «цены по дням»."""
    if not urls:
        return sh
    workbook = sh or _open_spreadsheet(base_dir)
    for title in (_sheet_detail(), _sheet_availability(), _sheet_prices_days()):
        ws = _open_ws(workbook, title)
        added = _ensure_urls_on_worksheet(ws, urls)
        if added:
            print(f"  лист «{title}»: добавлено строк с URL — {added}")
    return workbook


def filter_urls_needing_parse(base_dir: Path, urls_from_links: list[str]) -> list[str]:
    """
    Ссылки к парсингу: из «ссылки» и все строки «детальная информация» без «название»
    (в т.ч. после обрыва интернета — URL уже на листе, поля пустые).
  """
    sh = _open_spreadsheet(base_dir)
    incomplete_on_detail = _urls_without_title_on_detail(sh)
    candidates = _merge_url_lists(urls_from_links, incomplete_on_detail)

    if not candidates:
        return []

    if incomplete_on_detail:
        print(
            f"На «{_sheet_detail()}» без названия: {len(incomplete_on_detail)} "
            f"(добавлено к очереди помимо «{_sheet_links()}»)."
        )

    print("Синхронизация URL на рабочие листы таблицы…")
    sh = ensure_urls_on_all_sheets(base_dir, candidates, sh=sh)
    titles = _detail_title_by_url(sh)
    need: list[str] = []
    skipped = 0
    for url in candidates:
        if _title_needs_parse(titles.get(_canon_url(url), "")):
            need.append(url)
        else:
            skipped += 1

    print(f"К парсингу: {len(need)}. Уже с названием (пропуск): {skipped}.")
    return need


def _cell_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _canon_url(u: str) -> str:
    s = (u or "").strip()
    if not s:
        return ""
    s = s.split("?", 1)[0].strip().rstrip("/")
    return s


def _month_from_russian(word: str) -> int | None:
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


def _parse_period_dates(label: str, year: int) -> tuple[date, date] | None:
    m = _RE_PERIOD.match((label or "").strip())
    if not m:
        return None
    d1, d2 = int(m.group(1)), int(m.group(2))
    mon = _month_from_russian(m.group(3))
    if mon is None:
        return None
    try:
        s = date(year, mon, d1)
        e = date(year, mon, d2)
    except ValueError:
        return None
    return s, e


def _resolve_period_year(label: str, today: date) -> int:
    """Год для подписи периода: текущий или +1, если с датой в прошлом относительно «сезона»."""
    for y in (today.year, today.year + 1):
        pr = _parse_period_dates(label, y)
        if not pr:
            continue
        s, e = pr
        if s >= today - timedelta(days=120):
            return y
    return today.year


def _one_night_stay(start: date, end: date) -> bool:
    return (end - start).days == 1


def _parse_header_date(cell: str, year: int) -> date | None:
    m = _RE_HEADER_DAY.match((cell or "").strip())
    if not m:
        return None
    d, mo = int(m.group(1)), int(m.group(2))
    try:
        return date(year, mo, d)
    except ValueError:
        return None


def _header_year_for_calendar(today: date, header_samples: list[date]) -> int:
    y = today.year
    if not header_samples:
        return y
    for hd in header_samples[:3]:
        try:
            dt = date(y, hd.month, hd.day)
        except ValueError:
            continue
        if dt < today - timedelta(days=200):
            return today.year + 1
    return y


def _collect_valid_intervals(booking: dict[str, str], today: date) -> list[tuple[date, date, str]]:
    out: list[tuple[date, date, str]] = []
    for label, price in (booking or {}).items():
        y = _resolve_period_year(label, today)
        pr = _parse_period_dates(label, y)
        if not pr:
            continue
        s, e = pr
        if not _one_night_stay(s, e):
            continue
        out.append((s, e, str(price).strip()))
    out.sort(key=lambda x: x[0])
    return out


def _collect_all_booking_intervals(booking: dict[str, str], today: date) -> list[tuple[date, date]]:
    """Все интервалы d1–d2 из подписей брони (один месяц в подписи). Для сдаваемости, не только одна ночь."""
    out: list[tuple[date, date]] = []
    for label in (booking or {}).keys():
        y = _resolve_period_year(label, today)
        pr = _parse_period_dates(label, y)
        if not pr:
            continue
        s, e = pr
        if e <= s:
            continue
        out.append((s, e))
    out.sort(key=lambda x: x[0])
    return out


def _build_day_updates_availability(intervals: list[tuple[date, date]], today: date) -> dict[date, str]:
    """0 — ночь есть в карусели брони на сайте; 1 — занято (между окнами или до первого окна, если слота нет)."""
    zero_days: set[date] = set()
    for s, e in intervals:
        last_in = e - timedelta(days=1)
        d = s
        while d <= last_in:
            zero_days.add(d)
            d += timedelta(days=1)

    by_day: dict[date, str] = {}
    for d in zero_days:
        if d >= today:
            by_day[d] = "0"

    if intervals:
        first_start = intervals[0][0]
        d = today
        while d < first_start:
            by_day[d] = "1"
            d += timedelta(days=1)

    for i in range(len(intervals) - 1):
        e_i = intervals[i][1]
        s_next = intervals[i + 1][0]
        gap_start = e_i
        gap_end = s_next - timedelta(days=1)
        d = gap_start
        while d <= gap_end:
            if d >= today and d not in zero_days:
                by_day[d] = "1"
            d += timedelta(days=1)
    return by_day


def _build_day_updates_prices(intervals: list[tuple[date, date, str]], today: date) -> dict[date, str]:
    """Цена в колонку первого дня ночи; только однодневные интервалы."""
    by_day: dict[date, str] = {}
    for s, _, price in intervals:
        if s >= today:
            by_day[s] = price
    return by_day


def _require_worksheet(sh: Any, title: str) -> Any:
    """Открывает лист по точному имени. Не создаёт пустой лист — иначе данные оказываются «не там»."""
    import gspread
    from gspread.exceptions import WorksheetNotFound

    try:
        return sh.worksheet(title)
    except WorksheetNotFound as exc:
        names = ", ".join(repr(w.title) for w in sh.worksheets())
        raise RuntimeError(
            f"В таблице нет листа с именем {title!r}. "
            f"Переименуйте лист в Google Таблице или задайте переменную окружения "
            f"(AVITO_GOOGLE_SHEET_TAB / AVITO_GOOGLE_SHEET_AVAILABILITY / AVITO_GOOGLE_SHEET_PRICES_DAYS). "
            f"Сейчас есть листы: {names}"
        ) from exc


def _open_ws(sh: Any, title: str) -> Any:
    return _require_worksheet(sh, title)


def _find_row_by_url_col_a(ws: Any, url: str) -> int:
    """1-based номер строки с URL в столбце A; иначе следующая пустая строка."""
    canon = _canon_url(url)
    col_a = ws.col_values(1)
    for i, cell in enumerate(col_a, start=1):
        if i == 1:
            continue
        if _canon_url(cell) == canon:
            return i
    return len(col_a) + 1


def _ensure_row_with_url(ws: Any, row: int, url: str, num_cols: int) -> None:
    """В A row стоит URL; строка дополняется пустыми ячейками при необходимости."""
    if row < 1:
        return
    row_vals = ws.row_values(row)
    if len(row_vals) < 1 or _canon_url(row_vals[0]) != _canon_url(url):
        pad = max(0, num_cols - 1)
        ws.update(f"A{row}", [[url] + [""] * pad], value_input_option="USER_ENTERED")


def _build_header_date_map(headers: list[str], today: date) -> dict[date, int]:
    """date -> 1-based индекс столбца в листе."""
    samples: list[date] = []
    for h in headers[1:]:
        d0 = _parse_header_date(h, today.year)
        if d0:
            samples.append(d0)
    year = _header_year_for_calendar(today, samples)
    col_by: dict[date, int] = {}
    for j, h in enumerate(headers):
        if j == 0:
            continue
        d = _parse_header_date(h, year)
        if d:
            col_by[d] = j + 1
    return col_by


def _batch_updates_for_row(
    ws: Any,
    row: int,
    col_by_date: dict[date, int],
    day_values: dict[date, str],
    today: date,
) -> list[dict[str, Any]]:
    from gspread.utils import rowcol_to_a1

    body: list[dict[str, Any]] = []
    for d, val in day_values.items():
        if d < today:
            continue
        col = col_by_date.get(d)
        if not col:
            continue
        a1 = rowcol_to_a1(row, col)
        body.append({"range": a1, "values": [[val]]})
    return body


def _api_retry(fn: Any, waits: tuple[float, ...] = (1.5, 3.0, 8.0, 16.0)) -> Any:
    import gspread
    from gspread.exceptions import APIError

    last: Exception | None = None
    for attempt in range(len(waits) + 1):
        try:
            return fn()
        except APIError as exc:
            last = exc
            code = getattr(getattr(exc, "response", None), "status_code", None)
            if code == 429 and attempt < len(waits):
                time.sleep(waits[attempt])
                continue
            raise
    if last:
        raise last


def _detail_header_row(columns: list[str]) -> list[str]:
    """Строка 1: A — «Объявление», с B — названия полей (как ключи в JSON/record)."""
    return ["Объявление"] + list(columns)


def _detail_b_headers_need_write(row0: list[str], columns: list[str]) -> bool:
    """Нужно ли обновить заголовки с колонки B (имена полей)."""
    for j, name in enumerate(columns):
        idx = 1 + j
        if idx >= len(row0):
            return True
        if (row0[idx] or "").strip() != (name or "").strip():
            return True
    return False


def _upsert_detail_row(
    sh: Any,
    record: dict[str, object],
    columns: list[str],
    listing_url: str,
) -> tuple[int, str]:
    """Лист «детальная информация»: заголовки в 1-й строке (A + поля с B), данные в строке с URL в A."""
    from gspread.utils import rowcol_to_a1

    ws = _open_ws(sh, _sheet_detail())
    row0 = ws.row_values(1) or []
    need_header = not row0 or _detail_b_headers_need_write(row0, columns)

    row = _find_row_by_url_col_a(ws, listing_url)
    header = _detail_header_row(columns)
    addr_parts = _split_address_for_sheet(str(record.get("адрес") or ""))
    values = [listing_url.strip()] + [
        _detail_field_value(col, record, addr_parts=addr_parts) for col in columns
    ]

    body: list[dict[str, Any]] = []
    if need_header:
        a1 = rowcol_to_a1(1, len(header))
        body.append({"range": f"A1:{a1}", "values": [header]})
    r1 = rowcol_to_a1(row, 1)
    r2 = rowcol_to_a1(row, len(values))
    body.append({"range": f"{r1}:{r2}", "values": [values]})

    def _do() -> None:
        ws.batch_update(body, value_input_option="USER_ENTERED")

    _api_retry(_do)
    note = "заголовки 1-й строки обновлены" if need_header else "данные в существующей строке"
    return row, note


def append_avito_detail_row(record: dict[str, object], columns: list[str], *, base_dir: Path) -> None:
    """Только детальный лист: строка с URL в A, поля с B (как при sync_after_listing)."""
    url = str(record.get("ссылка") or "").strip()
    if not url:
        raise ValueError("В записи нет поля «ссылка» — нечем сопоставить строку на листе")

    sh = _open_spreadsheet(base_dir)
    _upsert_detail_row(sh, record, detail_sheet_columns(columns), url)


def _update_calendar_sheet(
    sh: Any,
    sheet_title: str,
    listing_url: str,
    booking: dict[str, str],
    today: date,
    mode: str,
) -> tuple[int, str]:
    """mode: availability (0/1) | prices (строка цены). Возвращает (число записанных ячеек, пояснение)."""
    ws = _open_ws(sh, sheet_title)
    rows = ws.get_all_values()
    if not rows:
        return 0, "лист пуст — добавьте строку 1 с «Объявление» и датами вида 14.05"
    headers = rows[0]
    col_by_date = _build_header_date_map(headers, today)
    if not col_by_date:
        return 0, "в строке 1 нет заголовков дат вида 14.05 (проверьте формат столбцов)"

    intervals_prices = _collect_valid_intervals(booking, today)
    if mode == "availability":
        intervals_av = _collect_all_booking_intervals(booking, today)
        day_map = _build_day_updates_availability(intervals_av, today)
        if not intervals_av:
            for d in col_by_date:
                if d >= today:
                    day_map[d] = "1"
    else:
        day_map = _build_day_updates_prices(intervals_prices, today)

    row = _find_row_by_url_col_a(ws, listing_url)
    _ensure_row_with_url(ws, row, listing_url, len(headers))

    updates = _batch_updates_for_row(ws, row, col_by_date, day_map, today)
    if not updates:
        if mode == "availability":
            if not intervals_av:
                return 0, "нет интервалов дат в блоке брони (или все даты уже в прошлом)"
        elif not intervals_prices:
            return 0, "нет однодневных интервалов для цен (или все даты в прошлом)"
        return 0, "нечего писать (все целевые даты вне диапазона столбцов или раньше сегодня)"

    def _do_batch() -> None:
        ws.batch_update(updates, value_input_option="USER_ENTERED")

    _api_retry(_do_batch)
    return len(updates), "ок"


def sync_after_listing(
    record: dict[str, object],
    columns: list[str],
    booking_prices: dict[str, str],
    listing_url: str,
    *,
    base_dir: Path,
) -> None:
    """После успешного парса: детальная строка + два календарных листа (одно подключение к таблице)."""
    sh = _open_spreadsheet(base_dir)
    for title in (_sheet_detail(), _sheet_availability(), _sheet_prices_days()):
        ws = _open_ws(sh, title)
        headers = ws.row_values(1) or ["Объявление"]
        row = _find_row_by_url_col_a(ws, listing_url)
        _ensure_row_with_url(ws, row, listing_url, max(len(headers), 1))

    detail_cols = detail_sheet_columns(columns)
    detail_row, detail_msg = _upsert_detail_row(sh, record, detail_cols, listing_url)

    today = datetime.now(_tz_moscow()).date()
    av_n, av_msg = _update_calendar_sheet(
        sh, _sheet_availability(), listing_url, booking_prices, today, "availability"
    )
    pr_n, pr_msg = _update_calendar_sheet(
        sh, _sheet_prices_days(), listing_url, booking_prices, today, "prices"
    )
    print(
        f"[Google Sheet] «{_sheet_detail()}»: строка {detail_row} — {detail_msg}. "
        f"«{_sheet_availability()}»: {av_n} яч. — {av_msg}. "
        f"«{_sheet_prices_days()}»: {pr_n} яч. — {pr_msg}."
    )
