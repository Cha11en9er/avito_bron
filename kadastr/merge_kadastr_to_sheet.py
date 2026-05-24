"""
Запись результатов кадастрового парсинга на лист «детальная информация с кадастрами».

Сопоставление строк по колонке «адрес» (как в adresa.txt / JSON). Строки-заглушки
(«нету на сайте», пустой адрес, служебные URL) не заполняются.

Запуск из корня репозитория:
  python kadastr/merge_kadastr_to_sheet.py
  python kadastr/merge_kadastr_to_sheet.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KADASTR_DIR = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google_sheets.client import (  # noqa: E402
    api_retry,
    header_column_index,
    load_dotenv,
    open_spreadsheet,
)
from kadastr.parser_common import is_skippable_address  # noqa: E402

DEFAULT_SHEET = "детальная информация с кадастрами"
GEOINF_JSON = KADASTR_DIR / "results_geoinf_portal.json"
KADASTOR_JSON = KADASTR_DIR / "results_kadastor.json"
NO_CADASTR = "кадастра нету"

KADASTR_COLUMNS = (
    "Геоинф_ссылка",
    "Геоинф_номер",
    "Кадастор_номер_первый",
    "Кадастровый_номер_максимальный",
    "Кадастор_ссылка_первый",
    "Кадастор_ссылка_максимальный",
)


def _sheet_title() -> str:
    import os

    return (os.getenv("AVITO_GOOGLE_SHEET_TAB_KADASTR") or DEFAULT_SHEET).strip()


def _kadastor_rank(entry: dict) -> int:
    status = entry.get("статус")
    if status == "ok":
        score = 3
        if entry.get("макс_площадь"):
            score += 1
        if entry.get("первая_строка"):
            score += 1
        return score
    if status == "empty":
        return 1
    return 0


def _geoinf_rank(entry: dict) -> int:
    buildings = entry.get("здания") or {}
    if entry.get("результат") == "найдено" and (buildings.get("ссылка") or "").strip():
        score = 3
        if buildings.get("кадастровый_номер"):
            score += 1
        return score
    if entry.get("результат") == "найдено":
        return 2
    if entry.get("результат") == "нету объектов":
        return 1
    return 0


def _load_results(path: Path, *, source: str) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rank_fn = _kadastor_rank if source == "kadastor" else _geoinf_rank
    out: dict[str, dict] = {}
    for row in data.get("результаты", []):
        addr = (row.get("адрес") or "").strip()
        if not addr:
            continue
        prev = out.get(addr)
        if prev is None or rank_fn(row) >= rank_fn(prev):
            out[addr] = row
    return out


def _geoinf_values(entry: dict) -> tuple[str, str]:
    if entry.get("результат") != "найдено":
        return NO_CADASTR, NO_CADASTR
    buildings = entry.get("здания") or {}
    link = (buildings.get("ссылка") or "").strip()
    number = (buildings.get("кадастровый_номер") or "").strip()
    return link or NO_CADASTR, number or NO_CADASTR


def _kadastor_values(entry: dict) -> tuple[str, str, str, str]:
    if entry.get("статус") != "ok":
        return (NO_CADASTR,) * 4
    first = entry.get("первая_строка") or {}
    max_row = entry.get("макс_площадь") or first
    num_first = (first.get("кадастровый_номер") or "").strip()
    link_first = (first.get("ссылка") or "").strip()
    num_max = (max_row.get("кадастровый_номер") or "").strip()
    link_max = (max_row.get("ссылка") or "").strip()
    return (
        num_first or NO_CADASTR,
        num_max or NO_CADASTR,
        link_first or NO_CADASTR,
        link_max or NO_CADASTR,
    )


def _row_address(row: list[str], addr_idx: int) -> str:
    if addr_idx is None or addr_idx >= len(row):
        return ""
    return (row[addr_idx] or "").strip()


def _should_fill_row(address: str) -> bool:
    return bool(address) and not is_skippable_address(address)


def merge(*, base_dir: Path, dry_run: bool = False) -> None:
    load_dotenv(base_dir)
    geoinf_by_addr = _load_results(GEOINF_JSON, source="geoinf")
    kadastor_by_addr = _load_results(KADASTOR_JSON, source="kadastor")

    sh = open_spreadsheet(base_dir)
    ws = sh.worksheet(_sheet_title())
    rows = ws.get_all_values()
    if not rows:
        raise RuntimeError(f'Лист «{_sheet_title()}» пуст')

    headers = [str(h or "").strip() for h in rows[0]]
    addr_idx = header_column_index(headers, "адрес")
    if addr_idx is None:
        raise RuntimeError('На листе нет колонки «адрес»')

    existing_cols = [
        header_column_index(headers, name) for name in KADASTR_COLUMNS
    ]
    existing_cols = [i for i in existing_cols if i is not None]
    if existing_cols:
        start_col = min(existing_cols)
    else:
        start_col = next(
            (i for i, h in enumerate(headers) if not (h or "").strip()),
            len(headers),
        )

    filled = 0
    skipped = 0
    missing_geo = 0
    missing_kad = 0
    data_rows: list[list[str]] = []

    for row in rows[1:]:
        address = _row_address(row, addr_idx)
        if not _should_fill_row(address):
            skipped += 1
            data_rows.append([""] * len(KADASTR_COLUMNS))
            continue

        geo = geoinf_by_addr.get(address)
        kad = kadastor_by_addr.get(address)
        if geo is None:
            missing_geo += 1
        if kad is None:
            missing_kad += 1

        g_link, g_num = _geoinf_values(geo or {})
        k_num1, k_num_max, k_link1, k_link_max = _kadastor_values(kad or {})
        data_rows.append([g_link, g_num, k_num1, k_num_max, k_link1, k_link_max])
        filled += 1

    print(f'Лист: «{_sheet_title()}»')
    print(f"Строк данных: {len(data_rows)}, заполнено: {filled}, пропущено: {skipped}")
    if missing_geo or missing_kad:
        print(f"Без совпадения в JSON — geoinf: {missing_geo}, kadastor: {missing_kad}")

    if dry_run:
        print("Режим --dry-run: в таблицу не писали.")
        return

    from gspread.utils import rowcol_to_a1

    body: list[dict] = []
    end_header_col = start_col + len(KADASTR_COLUMNS)
    h1 = rowcol_to_a1(1, start_col + 1)
    h2 = rowcol_to_a1(1, end_header_col)
    body.append({"range": f"{h1}:{h2}", "values": [list(KADASTR_COLUMNS)]})

    if data_rows:
        d1 = rowcol_to_a1(2, start_col + 1)
        d2 = rowcol_to_a1(1 + len(data_rows), end_header_col)
        body.append({"range": f"{d1}:{d2}", "values": data_rows})

    def _do() -> None:
        ws.batch_update(body, value_input_option="USER_ENTERED")

    api_retry(_do)
    print(f"Записано столбцов {len(KADASTR_COLUMNS)} с позиции {start_col + 1}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Кадастр → Google Таблица")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="только статистика, без записи в таблицу",
    )
    args = parser.parse_args()
    try:
        merge(base_dir=ROOT, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
