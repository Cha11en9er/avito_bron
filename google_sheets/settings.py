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
    DEFAULT_WORKSHEET_BOOKING_DATES,
    DEFAULT_WORKSHEET_SETTINGS,
)


@dataclass
class ParserSettings:
    # --- общее ---
    sync_from_links_sheet: bool = True
    detail_range_from: int = 0
    detail_range_to: int = 0
    browser_restart_every: int = 10
    sheet_sync_min_interval_s: float = 3.5
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
    iteration_progress_for: int = 1
    iteration_logs_cleared_for: int = 0
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
    sheet_booking_dates: str = DEFAULT_WORKSHEET_BOOKING_DATES
    sheet_logs: str = DEFAULT_WORKSHEET_LOGS
    sheet_settings: str = DEFAULT_WORKSHEET_SETTINGS

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}


SETTINGS_USER: list[tuple[str, str, str]] = [
    (
        "iteration_progress",
        "0",
        "С какой строки продолжить: 0 — с начала (строка 2); 2188 — со строки 2188. "
        "После полного прохода парсер ставит 0.",
    ),
    (
        "parse_iteration",
        "1",
        "Номер текущего прогона. Меняйте только этот ключ для новой итерации — "
        "остальные iteration_* подстроятся при запуске.",
    ),
    (
        "detail_range_from",
        "0",
        "С какой строки листа начать (строка 2 = первая ссылка). 0 и 0 — весь список.",
    ),
    (
        "detail_range_to",
        "0",
        "До какой строки включительно. Примеры: 2 и 11 → строки 2–11; 2188 и 2188 — одна строка; "
        "2188 и 0 — с 2188 до конца.",
    ),
    (
        "sync_from_links_sheet",
        "1",
        "Откуда брать список URL. 1 — из листа «ссылки» (столбец A); новые URL дописываются в конец "
        "рабочих листов, удалённые из «ссылки» убираются со смещением. "
        "0 — очередь только из «сдаваемость»; лист «ссылки» не читается.",
    ),
    (
        "run_calendar",
        "1",
        "1 — парсить «сдаваемость по дням» (datepicker: 2 месяца — текущий и следующий, без листания) "
        "и «цены по дням» (карусель «ближайшие даты», не дальше 2 мес.). "
        "0 — календарь не трогать.",
    ),
    (
        "calendar_mode",
        "daily",
        "daily — не удалять строки; столбцы дат добавляются при парсинге; пишутся дни ≥ сегодня (MSK). "
        "primary — один раз пересоздать листы «сдаваемость» и «цены» по списку «ссылки» "
        "(нужен sync_from_links_sheet=1), затем вернуть daily.",
    ),
    (
        "run_detail",
        "0",
        "1 — дополнительно парсить «детальная информация» (телефон, описание, фото). "
        "0 — только календарь (обычно для ежедневного прогона).",
    ),
    (
        "detail_fill_mode",
        "append",
        "Только при run_detail=1. primary — пересоздать лист «детальная информация» по «ссылки». "
        "rebuild — очистить лист и заново заполнить только URL. "
        "append — парсить строки с пустым «название» (см. detail_only_empty_title).",
    ),
    (
        "detail_only_empty_title",
        "1",
        "При detail_fill_mode=append: 1 — пропускать строки с заполненным «название»; "
        "0 — парсить все URL из диапазона.",
    ),
    (
        "calendar_start_date",
        "03.05",
        "Только при calendar_mode=primary: с какой даты начать столбцы (ДД.ММ). В daily не используется.",
    ),
    (
        "calendar_start_year",
        "2026",
        "Год для заголовков столбцов дат (ДД.ММ).",
    ),
]

SETTINGS_SHEET_GAP_ROWS = 5

SETTINGS_SERVICE: list[tuple[str, str, str]] = [
    (
        "sheet_sync_min_interval_s",
        "3.5",
        "Служебное: минимальная пауза (сек.) между запросами к Google Sheets API в фоновом потоке.",
    ),
    (
        "browser_restart_every",
        "10",
        "Служебное: после скольких успешно обработанных объявлений перезапустить браузер.",
    ),
    (
        "debug_dump_html",
        "0",
        "Служебное: 1 — сохранять HTML карточки в debug_html_dir для отладки; 0 — не сохранять.",
    ),
    (
        "debug_html_dir",
        "debug_html",
        "Служебное: папка для HTML-дампов (от корня проекта), если debug_dump_html=1.",
    ),
    (
        "calendar_horizon_days",
        "90",
        "Служебное: зарезервировано (столбцы в daily добавляются при парсинге). На работу парсера не влияет.",
    ),
    ("sheet_links", DEFAULT_WORKSHEET_LINKS, "Служебное: имя листа со списком URL (столбец A)."),
    ("sheet_detail", DEFAULT_WORKSHEET_DETAIL, "Служебное: имя листа детальной информации."),
    (
        "sheet_availability",
        DEFAULT_WORKSHEET_AVAILABILITY,
        "Служебное: имя листа сдаваемости по дням (0/1).",
    ),
    ("sheet_prices", DEFAULT_WORKSHEET_PRICES_DAYS, "Служебное: имя листа цен по дням."),
    (
        "sheet_booking_dates",
        DEFAULT_WORKSHEET_BOOKING_DATES,
        "Служебное: лист дат появления брони (0→1 в сдаваемости → дата парсинга).",
    ),
    ("sheet_logs", DEFAULT_WORKSHEET_LOGS, "Служебное: имя листа логов парсинга."),
    ("sheet_settings", DEFAULT_WORKSHEET_SETTINGS, "Служебное: имя этого листа настроек."),
    (
        "iteration_progress_for",
        "1",
        "Служебное: для какой итерации записан iteration_progress. Не менять — только parse_iteration.",
    ),
    (
        "iteration_logs_cleared_for",
        "0",
        "Служебное: для какой итерации очищен блок в логах. Не менять — только parse_iteration.",
    ),
    (
        "iteration_slot_0",
        "1",
        "Служебное: номер итерации в 1-м блоке логов (C–F). Не менять — только parse_iteration.",
    ),
    (
        "iteration_slot_1",
        "2",
        "Служебное: номер итерации во 2-м блоке (H–K). Не менять — только parse_iteration.",
    ),
    (
        "iteration_slot_2",
        "3",
        "Служебное: номер итерации в 3-м блоке (M–P). Не менять — только parse_iteration.",
    ),
]

ALL_SETTINGS_ENTRIES = SETTINGS_USER + SETTINGS_SERVICE


def _settings_value(key: str, default: str, kv: dict[str, str] | None) -> str:
    if kv is None:
        return default
    return kv.get(key, default)


def _reset_settings_format(ws: Any, num_rows: int) -> None:
    n = max(num_rows, 40)
    cell_range = f"A1:C{n}"
    no_border = {
        "top": {"style": "NONE"},
        "bottom": {"style": "NONE"},
        "left": {"style": "NONE"},
        "right": {"style": "NONE"},
    }
    plain = {
        "backgroundColor": {"red": 1, "green": 1, "blue": 1},
        "textFormat": {"bold": False, "foregroundColor": {"red": 0, "green": 0, "blue": 0}},
        "horizontalAlignment": "LEFT",
        "borders": no_border,
    }
    header = {
        **plain,
        "textFormat": {"bold": True, "foregroundColor": {"red": 0, "green": 0, "blue": 0}},
    }

    def _fmt() -> None:
        ws.format(cell_range, plain)
        ws.format("A1:C1", header)

    api_retry(_fmt)


def _settings_layout_ok(rows: list[list[str]]) -> bool:
    """Проверка: первая настройка — iteration_progress, без старого разделителя сверху."""
    if not rows or len(rows) < 2:
        return False
    for row in rows[1:]:
        key = (row[0] or "").strip()
        if not key:
            continue
        if key == "sheet_sync_min_interval_s" and row == rows[1]:
            return False
        return key == SETTINGS_USER[0][0]
    return False


def _build_settings_body(kv: dict[str, str] | None = None) -> list[list[str]]:
    header = ["ключ", "значение", "описание"]
    body: list[list[str]] = [header]
    for key, default, hint in SETTINGS_USER:
        body.append([key, _settings_value(key, default, kv), hint])
    for _ in range(SETTINGS_SHEET_GAP_ROWS):
        body.append(["", "", ""])
    for key, default, hint in SETTINGS_SERVICE:
        body.append([key, _settings_value(key, default, kv), hint])
    return body


def _read_settings_kv(rows: list[list[str]]) -> dict[str, str]:
    kv: dict[str, str] = {}
    for row in rows[1:]:
        if not row:
            continue
        key = (row[0] or "").strip()
        if not key:
            continue
        kv[key] = (row[1] if len(row) > 1 else "").strip()
    return kv


def _write_settings_body(ws: Any, body: list[list[str]]) -> None:
    def _write() -> None:
        ws.clear()
        ws.update("A1", body, value_input_option="USER_ENTERED")

    api_retry(_write)
    _reset_settings_format(ws, len(body))
    clear_settings_row_cache()


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
        "iteration_progress_for",
        "iteration_logs_cleared_for",
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
    if name == "sheet_sync_min_interval_s":
        try:
            return float(s.replace(",", "."))
        except ValueError:
            return getattr(ParserSettings(), name)
    return s


def _sheet_has_settings_rows(rows: list[list[str]]) -> bool:
    for row in rows[1:]:
        if row and (row[0] or "").strip():
            return True
    return False


def seed_settings_sheet(ws: Any, *, force: bool = False, refresh: bool = False) -> None:
    rows = ws.get_all_values()
    if refresh or force:
        kv = None if force else _read_settings_kv(rows)
        body = _build_settings_body(kv)
        _write_settings_body(ws, body)
        if force:
            print(f"Лист «{DEFAULT_WORKSHEET_SETTINGS}» перезаписан значениями по умолчанию.")
        else:
            print(
                f"Лист «{DEFAULT_WORKSHEET_SETTINGS}» обновлён: новая структура и описания, "
                "значения сохранены."
            )
        return

    if not _sheet_has_settings_rows(rows):
        body = _build_settings_body()
        _write_settings_body(ws, body)
        print(f"Лист «{DEFAULT_WORKSHEET_SETTINGS}» заполнен значениями по умолчанию.")
        return

    existing = {(row[0] or "").strip() for row in rows[1:] if row}
    missing = [
        (k, v, h)
        for k, v, h in ALL_SETTINGS_ENTRIES
        if k and k not in existing
    ]
    if not missing:
        return

    kv = _read_settings_kv(rows)
    for key, default, hint in missing:
        kv[key] = kv.get(key, default)
    body = _build_settings_body(kv)
    _write_settings_body(ws, body)
    print(f"Лист «{DEFAULT_WORKSHEET_SETTINGS}»: добавлено ключей — {len(missing)}.")


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
        elif not _settings_layout_ok(rows):
            seed_settings_sheet(ws, refresh=True)
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

    settings = ParserSettings(**data)
    from google_sheets.iterations import reconcile_iteration_change

    settings, _ = reconcile_iteration_change(settings)
    return settings


def save_settings_values(sh: Any, settings: ParserSettings, updates: dict[str, str]) -> None:
    """Обновить значения ключей на листе «настройки» (колонка B)."""
    from gspread.utils import rowcol_to_a1

    clear_settings_row_cache()
    ws = ensure_worksheet(sh, settings.sheet_settings, rows=100, cols=4)

    def _body_for(keys: dict[str, int]) -> list[dict]:
        body: list[dict] = []
        for key, val in updates.items():
            r = keys.get(key)
            if r is None:
                continue
            body.append({"range": rowcol_to_a1(r, 2), "values": [[val]]})
        return body

    key_to_row = _settings_key_row_map(ws)
    body = _body_for(key_to_row)
    if not body and updates:
        clear_settings_row_cache()
        key_to_row = _settings_key_row_map(ws)
        body = _body_for(key_to_row)

    if not body:
        if updates:
            print(
                "Предупреждение: не записано в настройки — ключи не найдены на листе: "
                + ", ".join(sorted(updates))
            )
        return

    def _do() -> None:
        ws.batch_update(body, value_input_option="USER_ENTERED")

    api_retry(_do)


_settings_key_cache: dict[str, int] | None = None


def warm_settings_row_cache(sh: Any, settings: ParserSettings) -> None:
    """Один раз за прогон: карта ключ → строка на листе «настройки» (без read на каждую запись)."""
    global _settings_key_cache
    ws = ensure_worksheet(sh, settings.sheet_settings, rows=100, cols=4)
    _settings_key_cache = _settings_key_row_map(ws)


def clear_settings_row_cache() -> None:
    global _settings_key_cache
    _settings_key_cache = None


def _settings_key_row_map(ws: Any) -> dict[str, int]:
    global _settings_key_cache
    if _settings_key_cache is not None:
        return _settings_key_cache

    def _read() -> list[list[str]]:
        return ws.get_all_values()

    rows = api_retry(_read)
    key_to_row: dict[str, int] = {}
    for i, row in enumerate(rows[1:], start=2):
        if row and (row[0] or "").strip():
            key_to_row[(row[0] or "").strip()] = i
    _settings_key_cache = key_to_row
    return key_to_row


def seed_settings_workbook(base_dir: Path, *, force: bool = False, refresh: bool = False) -> None:
    from google_sheets.client import open_spreadsheet

    sh = open_spreadsheet(base_dir)
    ws = ensure_worksheet(sh, DEFAULT_WORKSHEET_SETTINGS, rows=100, cols=4)
    seed_settings_sheet(ws, force=force, refresh=refresh)
