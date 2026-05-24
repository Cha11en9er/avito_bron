"""Лист «детальная информация»."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google_sheets.client import (
    api_retry,
    canon_url,
    ensure_worksheet,
    find_row_by_url_col_a,
    get_worksheet,
    header_column_index,
)
from google_sheets.constants import (
    DETAIL_NUMERIC_COLUMNS,
    DETAIL_SHEET_EXCLUDE_COLUMNS,
    NOT_FOUND_ON_SITE,
    RE_ADDR_HIGHWAY,
    RE_ADDR_TRAIL_KM,
    RE_DETAIL_M2,
    RE_DETAIL_RUB,
    RE_DETAIL_SOTKI,
)
from google_sheets.settings import ParserSettings


def split_address_for_sheet(raw: str) -> tuple[str, str, str | int]:
    s = (raw or "").strip()
    if not s:
        return "", "", ""

    km_val: str | int = ""
    m_km = RE_ADDR_TRAIL_KM.search(s)
    if m_km:
        km_val = int(m_km.group(1))
        s = s[: m_km.start()].strip()

    m_hw = RE_ADDR_HIGHWAY.search(s)
    if m_hw:
        highway = m_hw.group(1).strip()
        addr = s[: m_hw.start()].strip().rstrip(",")
        return addr, highway, km_val

    return s, "", km_val


def detail_sheet_columns(columns: list[str]) -> list[str]:
    out: list[str] = []
    for col in columns:
        if col in DETAIL_SHEET_EXCLUDE_COLUMNS:
            continue
        if col == "адрес":
            out.extend(["адрес", "шоссе", "километраж"])
        else:
            out.append(col)
    return out


def _digits_only_int(text: str) -> int | None:
    import re

    digits = re.sub(r"\D", "", text or "")
    if not digits:
        return None
    return int(digits)


def _parse_detail_numeric(column: str, value: object) -> object:
    if value is None:
        return ""
    s = str(value).strip()
    if not s:
        return ""

    if column == "площадь дома":
        m = RE_DETAIL_M2.search(s)
        if m:
            return _digits_only_int(m.group(1)) or ""
    elif column == "площадь участка":
        m = RE_DETAIL_SOTKI.search(s)
        if m:
            return _digits_only_int(m.group(1)) or ""
    elif column == "залог":
        m = RE_DETAIL_RUB.search(s)
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


def _cell_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def detail_field_value(
    column: str,
    record: dict[str, object],
    *,
    addr_parts: tuple[str, str, str | int] | None = None,
) -> object:
    if column == "адрес":
        parts = addr_parts or split_address_for_sheet(str(record.get("адрес") or ""))
        return parts[0]
    if column == "шоссе":
        parts = addr_parts or split_address_for_sheet(str(record.get("адрес") or ""))
        return parts[1]
    if column == "километраж":
        parts = addr_parts or split_address_for_sheet(str(record.get("адрес") or ""))
        return parts[2]
    if column == "цена":
        return _clean_detail_price(record.get("цена"))
    if column in DETAIL_NUMERIC_COLUMNS:
        return _parse_detail_numeric(column, record.get(column))
    return _cell_value(record.get(column))


def title_needs_parse(title: str) -> bool:
    t = (title or "").strip()
    return not t or t == NOT_FOUND_ON_SITE


def detail_header_row(columns: list[str]) -> list[str]:
    return ["Объявление"] + list(columns)


def _detail_b_headers_need_write(row0: list[str], columns: list[str]) -> bool:
    for j, name in enumerate(columns):
        idx = 1 + j
        if idx >= len(row0):
            return True
        if (row0[idx] or "").strip() != (name or "").strip():
            return True
    return False


def open_detail_ws(sh: Any, settings: ParserSettings) -> Any:
    ws = get_worksheet(sh, settings.sheet_detail)
    if ws is None:
        ws = ensure_worksheet(sh, settings.sheet_detail, rows=3000, cols=50)
    return ws


def detail_title_by_url(sh: Any, settings: ParserSettings) -> dict[str, str]:
    ws = get_worksheet(sh, settings.sheet_detail)
    if ws is None:
        return {}
    rows = ws.get_all_values()
    if not rows:
        return {}
    headers = [str(h or "").strip() for h in rows[0]]
    idx = header_column_index(headers, "название")
    if idx is None:
        return {}

    out: dict[str, str] = {}
    for row in rows[1:]:
        if not row:
            continue
        canon = canon_url(row[0] if row else "")
        if not canon:
            continue
        title = row[idx].strip() if idx < len(row) else ""
        out[canon] = title
    return out


def urls_without_title_on_detail(sh: Any, settings: ParserSettings) -> list[str]:
    ws = get_worksheet(sh, settings.sheet_detail)
    if ws is None:
        return []
    rows = ws.get_all_values()
    if not rows:
        return []
    headers = [str(h or "").strip() for h in rows[0]]
    idx = header_column_index(headers, "название")
    if idx is None:
        return []

    out: list[str] = []
    seen: set[str] = set()
    for row in rows[1:]:
        url = (row[0] if row else "").strip()
        if not url.lower().startswith("http"):
            continue
        title = row[idx].strip() if idx < len(row) else ""
        if not title_needs_parse(title):
            continue
        canon = canon_url(url)
        if not canon or canon in seen:
            continue
        seen.add(canon)
        out.append(url)
    return out


def rebuild_detail_sheet(sh: Any, settings: ParserSettings, urls: list[str], columns: list[str]) -> None:
    from gspread.utils import rowcol_to_a1

    ws = ensure_worksheet(sh, settings.sheet_detail, rows=max(3000, len(urls) + 10), cols=50)
    detail_cols = detail_sheet_columns(columns)
    header = detail_header_row(detail_cols)
    body = [header]
    for url in urls:
        body.append([url.strip()] + [""] * len(detail_cols))

    def _write() -> None:
        ws.clear()
        end = rowcol_to_a1(len(body), len(header))
        ws.update(f"A1:{end}", body, value_input_option="USER_ENTERED")

    api_retry(_write)
    print(f"Лист «{settings.sheet_detail}» пересобран: {len(urls)} URL.")


def upsert_detail_row(
    sh: Any,
    settings: ParserSettings,
    record: dict[str, object],
    columns: list[str],
    listing_url: str,
) -> tuple[int, str]:
    from gspread.utils import rowcol_to_a1

    ws = open_detail_ws(sh, settings)
    detail_cols = detail_sheet_columns(columns)
    row0 = ws.row_values(1) or []
    need_header = not row0 or _detail_b_headers_need_write(row0, detail_cols)

    row = find_row_by_url_col_a(ws, listing_url)
    header = detail_header_row(detail_cols)
    addr_parts = split_address_for_sheet(str(record.get("адрес") or ""))
    values = [listing_url.strip()] + [
        detail_field_value(col, record, addr_parts=addr_parts) for col in detail_cols
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

    api_retry(_do)
    note = "заголовки обновлены" if need_header else "данные записаны"
    return row, note


def write_not_found_detail_row(
    sh: Any,
    settings: ParserSettings,
    columns: list[str],
    listing_url: str,
) -> tuple[int, str]:
    record: dict[str, object] = {}
    for col in columns:
        if col in DETAIL_SHEET_EXCLUDE_COLUMNS:
            continue
        record[col] = NOT_FOUND_ON_SITE
    record["ссылка"] = listing_url
    return upsert_detail_row(sh, settings, record, columns, listing_url)
