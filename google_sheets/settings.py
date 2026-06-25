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
    sheet_sync_min_interval_s: float = 1.0
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
    sheet_logs: str = DEFAULT_WORKSHEET_LOGS
    sheet_settings: str = DEFAULT_WORKSHEET_SETTINGS

    @classmethod
    def field_names(cls) -> set[str]:
        return {f.name for f in fields(cls)}


SETTINGS_DYNAMIC: list[tuple[str, str, str]] = [
    (
        "sync_from_links_sheet",
        "1",
        "Откуда брать список URL для парсинга. 1 — из листа «ссылки» (столбец A); при запуске "
        "новые URL дописываются в конец рабочих листов, а строки с URL, которых уже нет в «ссылки», "
        "удаляются (со смещением) на «сдаваемость», «цены», «логи» и «деталь» (если включены). "
        "0 — очередь только из «сдаваемость»; лист «ссылки» не читается, автоудаления нет.",
    ),
    (
        "detail_range_from",
        "0",
        "С какой строки листа начать (строка 2 = первая ссылка). 0+0 — весь список.",
    ),
    (
        "detail_range_to",
        "0",
        "До какой строки включительно. Примеры: 2 и 11 → строки 2–11; 2188 и 2188 — одна строка; "
        "2188 и 0 — с 2188 до конца; 0 и 0 — весь список.",
    ),
    (
        "browser_restart_every",
        "10",
        "После скольких успешно обработанных объявлений перезапустить браузер (снижает сбои и утечки памяти).",
    ),
    (
        "sheet_sync_min_interval_s",
        "1",
        "Минимальная пауза (сек.) между записями в Google Таблицу в фоновом потоке — снижает лимит read requests per minute.",
    ),
    (
        "run_detail",
        "0",
        "1 — дополнительно парсить лист «детальная информация» (телефон, описание, фото, характеристики). "
        "0 — только бронь и цены (если run_calendar=1). Обычно для ежедневного прогона: 0.",
    ),
    (
        "detail_fill_mode",
        "append",
        "Только если run_detail=1. primary — полностью пересоздать лист «детальная информация» "
        "по текущему списку «ссылки» (все старые строки и данные удаляются), затем парсить. "
        "rebuild — очистить лист и заново заполнить только столбец URL из «ссылки», без парсинга карточек. "
        "append — не удалять лист; парсить только строки, где в колонке «название» пусто (см. detail_only_empty_title).",
    ),
    (
        "detail_only_empty_title",
        "1",
        "Только при detail_fill_mode=append. 1 — пропускать строки, у которых «название» уже заполнено "
        "(удобно догонять новые ссылки). 0 — при append обрабатывать все URL из диапазона, даже с заполненным названием.",
    ),
    (
        "run_calendar",
        "1",
        "1 — парсить «сдаваемость по дням» и «цены по дням» с Avito (основной режим). "
        "0 — календарь не трогать (имеет смысл только при отладке).",
    ),
    (
        "calendar_mode",
        "daily",
        "Только если run_calendar=1. daily — оставить существующие строки и даты; дописать новые URL "
        "в конец; обновлять ячейки по мере парсинга. primary — один раз пересоздать листы «сдаваемость» "
        "и «цены» только по текущему списку «ссылки» (старые URL и все 0/1/цены удаляются; нужен повторный прогон парсера). "
        "Нужен sync_from_links_sheet=1.",
    ),
    (
        "calendar_start_date",
        "03.05",
        "Только при calendar_mode=primary: с какой даты начать столбцы календаря (формат ДД.ММ).",
    ),
    (
        "calendar_start_year",
        "2026",
        "Год для подписей дат в заголовках (ДД.ММ).",
    ),
    (
        "calendar_horizon_days",
        "90",
        "Только при calendar_mode=primary: сколько дней вперёд заложить в шапку при пересоздании листа.",
    ),
    (
        "debug_dump_html",
        "0",
        "1 — сохранять HTML каждой карточки в папку debug_html_dir (для отладки). 0 — не сохранять.",
    ),
    ("debug_html_dir", "debug_html", "Папка для HTML-файлов (от корня проекта avito_bron)."),
    (
        "parse_iteration",
        "1",
        "Номер текущего «прогона» по всему списку. После полного прохода всех ссылок парсер "
        "сам увеличивает на 1 и останавливается (второй прогон в том же запуске не начинается). "
        "Следующий раз — только вручную: python -m parser. Можно вручную сменить номер итерации.",
    ),
    (
        "iteration_progress",
        "0",
        "С какой строки продолжить: 0 — с начала (строка 2); 2188 — со строки 2188. "
        "После полного прохода парсер сам ставит 0 и увеличивает parse_iteration.",
    ),
    (
        "iteration_slot_0",
        "1",
        "Служебное: номер итерации в 1-м блоке логов (колонки C–F: ит.N, дата, время, статус). Обновляется парсером.",
    ),
    (
        "iteration_slot_1",
        "2",
        "Служебное: номер итерации во 2-м блоке (H–K).",
    ),
    (
        "iteration_slot_2",
        "3",
        "Служебное: номер итерации в 3-м блоке (M–P). Кольцо из трёх последних прогонов.",
    ),
    (
        "iteration_progress_for",
        "1",
        "Служебное: для какой итерации записан iteration_progress. При смене parse_iteration вручную "
        "парсер обнуляет прогресс. Можно не трогать.",
    ),
    (
        "iteration_logs_cleared_for",
        "0",
        "Служебное: для какой итерации уже очищен блок логов (ит./дата/время/статус). "
        "При новой итерации парсер сам очистит свой слот. Можно не трогать.",
    ),
]

SETTINGS_SHEET_NAMES: list[tuple[str, str, str]] = [
    ("sheet_links", DEFAULT_WORKSHEET_LINKS, "Имя листа со списком URL (столбец A)."),
    ("sheet_detail", DEFAULT_WORKSHEET_DETAIL, "Имя листа детальной информации."),
    ("sheet_availability", DEFAULT_WORKSHEET_AVAILABILITY, "Имя листа сдаваемости по дням (0/1)."),
    ("sheet_prices", DEFAULT_WORKSHEET_PRICES_DAYS, "Имя листа цен по дням."),
    ("sheet_logs", DEFAULT_WORKSHEET_LOGS, "Имя листа логов парсинга."),
    ("sheet_settings", DEFAULT_WORKSHEET_SETTINGS, "Имя этого листа настроек."),
]

SETTINGS_SHEET_GAP_ROWS = 5


def _build_settings_body() -> list[list[str]]:
    header = ["ключ", "значение", "описание"]
    body: list[list[str]] = [header]
    for key, val, hint in SETTINGS_DYNAMIC:
        body.append([key, val, hint])
    body.append(
        [
            "",
            "",
            "——— Ниже только имена листов в Google Таблице (не настройки парсера) ———",
        ]
    )
    for _ in range(SETTINGS_SHEET_GAP_ROWS):
        body.append(["", "", ""])
    for key, val, hint in SETTINGS_SHEET_NAMES:
        body.append([key, val, hint])
    return body


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


def seed_settings_sheet(ws: Any, *, force: bool = False) -> None:
    rows = ws.get_all_values()
    if not force and _sheet_has_settings_rows(rows):
        existing = {(row[0] or "").strip() for row in rows[1:] if row}
        missing = [
            (k, v, h)
            for k, v, h in SETTINGS_DYNAMIC + SETTINGS_SHEET_NAMES
            if k and k not in existing
        ]
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

    body = _build_settings_body()

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

    settings = ParserSettings(**data)
    return _reset_progress_if_iteration_changed(sh, settings, kv)


def _reset_progress_if_iteration_changed(
    sh: Any, settings: ParserSettings, kv: dict[str, str]
) -> ParserSettings:
    """Ручная смена parse_iteration → iteration_progress = 1."""
    from dataclasses import replace

    it = max(1, settings.parse_iteration)
    if "iteration_progress_for" in kv:
        prog_for = max(1, settings.iteration_progress_for)
    else:
        prog_for = it

    if prog_for == it:
        return settings

    print(
        f"Смена итерации {prog_for} → {it}: iteration_progress сброшен на №1 "
        f"(было №{settings.iteration_progress})."
    )
    updated = replace(
        settings,
        iteration_progress=0,
        iteration_progress_for=it,
        iteration_logs_cleared_for=0,
    )
    save_settings_values(
        sh,
        updated,
        {
            "iteration_progress": "0",
            "iteration_progress_for": str(it),
            "iteration_logs_cleared_for": "0",
        },
    )
    return updated


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
