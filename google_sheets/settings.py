"""Лист «настройки»: ключ / значение / описание."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from google_sheets.client import api_retry, ensure_worksheet, get_worksheet, load_dotenv
from google_sheets.constants import (
    DEFAULT_SPREADSHEET_ID,
    DEFAULT_WORKSHEET_AVAILABILITY,
    DEFAULT_WORKSHEET_DETAIL,
    DEFAULT_WORKSHEET_LINKS,
    DEFAULT_WORKSHEET_LOGS,
    DEFAULT_WORKSHEET_PRICES_DAYS,
    DEFAULT_WORKSHEET_SETTINGS,
)


@dataclass
class ParserSettings:
    # --- общее ---
    sync_from_links_sheet: bool = True
    detail_range_from: int = 0
    detail_range_to: int = 0
    browser_restart_every: int = 10
    spreadsheet_id: str = DEFAULT_SPREADSHEET_ID
    # --- детальный парсер ---
    run_detail: bool = False
    detail_fill_mode: str = "append"
    detail_only_empty_title: bool = True
    # --- календарь (сдаваемость + цены) ---
    run_calendar: bool = True
    calendar_mode: str = "daily"
    calendar_start_date: str = "03.05"
    calendar_start_year: int = 2026
    # --- итерации парсинга (3 слота в логах) ---
    parse_iteration: int = 1
    iteration_progress: int = 0
    iteration_status: str = "idle"
    iteration_slot_0: int = 1
    iteration_slot_1: int = 2
    iteration_slot_2: int = 3
    calendar_horizon_days: int = 90
    # --- отладка парсера ---
    debug_dump_html: bool = False
    debug_html_dir: str = "debug_html"
    # --- имена листов ---
    sheet_links: str = DEFAULT_WORKSHEET_LINKS
    sheet_detail: str = DEFAULT_WORKSHEET_DETAIL
    sheet_availability: str = DEFAULT_WORKSHEET_AVAILABILITY
    sheet_prices: str = DEFAULT_WORKSHEET_PRICES_DAYS
    sheet_logs: str = DEFAULT_WORKSHEET_LOGS
    sheet_settings: str = DEFAULT_WORKSHEET_SETTINGS

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}


DEFAULTS_WITH_HINTS: list[tuple[str, str, str]] = [
    (
        "sync_from_links_sheet",
        "1",
        "1 — очередь с листа «ссылки», дописать URL на рабочие листы. "
        "0 — только столбец A листа сдаваемости (без синхронизации с «ссылки»)",
    ),
    (
        "detail_range_from",
        "0",
        "Индекс первой ссылки (с 0). В паре с detail_range_to=0 означает «все ссылки»",
    ),
    (
        "detail_range_to",
        "0",
        "Индекс конца (НЕ включая): 0+1 → одна ссылка; 0+10 → десять. "
        "Оба 0 → весь список",
    ),
    ("browser_restart_every", "10", "Перезапуск браузера после N объявлений"),
    ("spreadsheet_id", DEFAULT_SPREADSHEET_ID, "ID таблицы (переопределяется через .env)"),
    ("run_detail", "0", "1 — парсить лист «детальная информация»; 0 — пропустить"),
    (
        "detail_fill_mode",
        "append",
        "primary — пересоздать лист и парсить все; append — только пустое «название»; "
        "rebuild — очистить и заново; range — устарело, диапазон задаётся индексами выше",
    ),
    (
        "detail_only_empty_title",
        "1",
        "При append: 1 — не трогать строки с заполненным «название»; 0 — парсить все в диапазоне",
    ),
    ("run_calendar", "1", "1 — парсить «сдаваемость по дням» и «цены по дням»"),
    (
        "calendar_mode",
        "daily",
        "primary — пересоздать календарные листы (нужен sync_from_links_sheet=1); "
        "daily — дополнение дат ≥ сегодня",
    ),
    ("calendar_start_date", "03.05", "Первая дата столбца при calendar_mode=primary (ДД.ММ)"),
    ("calendar_start_year", "2026", "Год для заголовков дат"),
    (
        "calendar_horizon_days",
        "90",
        "Запас столбцов вперёд при calendar_mode=primary; плюс даты из брони",
    ),
    (
        "debug_dump_html",
        "0",
        "1 — сохранять HTML страницы после прокруток и брони в debug_html_dir",
    ),
    ("debug_html_dir", "debug_html", "Папка для HTML-дампов (от корня проекта)"),
    (
        "parse_iteration",
        "1",
        "Номер текущей итерации (прогон). После полного прохода +1 автоматически",
    ),
    (
        "iteration_progress",
        "0",
        "Сколько ссылок уже обработано в этой итерации (0 = с начала). При обрыве — продолжение",
    ),
    ("iteration_status", "idle", "running | complete | idle"),
    ("iteration_slot_0", "1", "Какая итерация в 1-м блоке логов (столбцы B–E)"),
    ("iteration_slot_1", "2", "2-й блок логов (F–I)"),
    ("iteration_slot_2", "3", "3-й блок логов (J–M)"),
    ("sheet_links", DEFAULT_WORKSHEET_LINKS, "Лист со списком URL"),
    ("sheet_detail", DEFAULT_WORKSHEET_DETAIL, "Детальная информация"),
    ("sheet_availability", DEFAULT_WORKSHEET_AVAILABILITY, "Сдаваемость по дням"),
    ("sheet_prices", DEFAULT_WORKSHEET_PRICES_DAYS, "Цены по дням"),
    ("sheet_logs", DEFAULT_WORKSHEET_LOGS, "Логи ежедневного парсинга"),
    ("sheet_settings", DEFAULT_WORKSHEET_SETTINGS, "Этот лист"),
]


def _parse_bool(raw: str) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on", "да")


def _coerce_value(name: str, raw: str) -> Any:
    s = (raw or "").strip()
    bool_keys = (
        "run_detail",
        "run_calendar",
        "sync_from_links_sheet",
        "detail_only_empty_title",
        "debug_dump_html",
    )
    if name in bool_keys:
        return _parse_bool(s)
    int_keys = (
        "detail_range_from",
        "detail_range_to",
        "browser_restart_every",
        "parse_iteration",
        "iteration_progress",
        "iteration_slot_0",
        "iteration_slot_1",
        "iteration_slot_2",
        "calendar_horizon_days",
    )
    if name in int_keys:
        try:
            return int(s)
        except ValueError:
            return getattr(ParserSettings(), name)
    if name == "calendar_start_year":
        try:
            return int(s)
        except ValueError:
            return 2026
    return s


def _sheet_has_settings_rows(rows: list[list[str]]) -> bool:
    for row in rows[1:]:
        if row and (row[0] or "").strip():
            return True
    return False


def seed_settings_sheet(ws: Any, *, force: bool = False) -> None:
    rows = ws.get_all_values()
    if not force and _sheet_has_settings_rows(rows):
        existing = {(row[0] or "").strip() for row in rows[1:] if row}
        missing = [(k, v, h) for k, v, h in DEFAULTS_WITH_HINTS if k not in existing]
        if not missing:
            return
        header = rows[0] if rows and rows[0] else ["ключ", "значение", "описание"]
        if not header or not (header[0] or "").strip():
            header = ["ключ", "значение", "описание"]
        body: list[list[str]] = [header]
        for row in rows[1:]:
            key = (row[0] or "").strip() if row else ""
            if not key:
                continue
            val = row[1] if len(row) > 1 else ""
            hint = row[2] if len(row) > 2 else ""
            body.append([key, val, hint])
        for key, val, hint in missing:
            body.append([key, val, hint])

        def _append() -> None:
            ws.clear()
            ws.update("A1", body, value_input_option="USER_ENTERED")

        api_retry(_append)
        print(f"Лист «{DEFAULT_WORKSHEET_SETTINGS}»: добавлено ключей — {len(missing)}.")
        return

    header = ["ключ", "значение", "описание"]
    body = [header]
    for key, val, hint in DEFAULTS_WITH_HINTS:
        body.append([key, val, hint])

    def _write() -> None:
        ws.clear()
        ws.update("A1", body, value_input_option="USER_ENTERED")

    api_retry(_write)
    print(f"Лист «{DEFAULT_WORKSHEET_SETTINGS}» заполнен значениями по умолчанию.")


def load_settings(base_dir: Path, sh: Any) -> ParserSettings:
    load_dotenv(base_dir)
    title = DEFAULT_WORKSHEET_SETTINGS
    ws = get_worksheet(sh, title)
    if ws is None:
        ws = ensure_worksheet(sh, title, rows=100, cols=4)
        seed_settings_sheet(ws, force=True)
    else:
        rows = ws.get_all_values()
        if not _sheet_has_settings_rows(rows):
            seed_settings_sheet(ws, force=True)
        else:
            seed_settings_sheet(ws, force=False)

    rows = ws.get_all_values()
    kv: dict[str, str] = {}
    for row in rows[1:]:
        if not row:
            continue
        key = (row[0] or "").strip()
        if not key:
            continue
        kv[key] = (row[1] if len(row) > 1 else "").strip()

    data: dict[str, Any] = {}
    for name in ParserSettings.field_names():
        if name in kv:
            data[name] = _coerce_value(name, kv[name])

    return ParserSettings(**data)


def save_settings_values(sh: Any, settings: ParserSettings, updates: dict[str, str]) -> None:
    """Обновить значения ключей на листе «настройки» (колонка B)."""
    from gspread.utils import rowcol_to_a1

    ws = ensure_worksheet(sh, settings.sheet_settings, rows=100, cols=4)
    rows = ws.get_all_values()
    key_to_row: dict[str, int] = {}
    for i, row in enumerate(rows[1:], start=2):
        if row and (row[0] or "").strip():
            key_to_row[(row[0] or "").strip()] = i

    body: list[dict] = []
    for key, val in updates.items():
        r = key_to_row.get(key)
        if r is None:
            continue
        body.append({"range": rowcol_to_a1(r, 2), "values": [[val]]})

    if not body:
        return

    def _do() -> None:
        ws.batch_update(body, value_input_option="USER_ENTERED")

    api_retry(_do)


def seed_settings_workbook(base_dir: Path, *, force: bool = False) -> None:
    from google_sheets.client import open_spreadsheet

    sh = open_spreadsheet(base_dir)
    ws = ensure_worksheet(sh, DEFAULT_WORKSHEET_SETTINGS, rows=100, cols=4)
    seed_settings_sheet(ws, force=force)
