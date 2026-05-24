"""Итерации парсинга: возобновление, кольцо из 3 слотов, логи."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from google_sheets.calendar import today_moscow, tz_moscow
from google_sheets.client import (
    api_retry,
    canon_url,
    ensure_worksheet,
    find_row_by_url_col_a,
    get_worksheet,
)
from google_sheets.constants import (
    LOG_STATUS_FAIL,
    LOG_STATUS_NOT_FOUND,
    LOG_STATUS_OK,
)
from google_sheets.settings import ParserSettings, save_settings_values

COLS_PER_SLOT = 4  # итерация | дата | время | статус
SLOT_STRIDE = COLS_PER_SLOT + 1  # после каждого блока — пустой столбец-отступ
LOG_FIRST_SLOT_COL = 3  # A=URL, B=отступ, C=ит.1


def iteration_slot_index(iteration_number: int) -> int:
    n = max(1, iteration_number)
    return (n - 1) % 3


def slot_iteration_ids(settings: ParserSettings) -> list[int]:
    return [
        max(0, settings.iteration_slot_0),
        max(0, settings.iteration_slot_1),
        max(0, settings.iteration_slot_2),
    ]


def build_logs_header(settings: ParserSettings) -> list[str]:
    ids = slot_iteration_ids(settings)
    header = ["Объявление", ""]
    for slot_i, iter_id in enumerate(ids):
        label = f"ит.{iter_id}" if iter_id else f"слот {slot_i + 1}"
        header.extend([label, "дата", "время", "статус"])
        if slot_i < len(ids) - 1:
            header.append("")
    return header


def slot_base_column(slot: int) -> int:
    """1-based: колонка «ит.N» для слота 0..2."""
    return LOG_FIRST_SLOT_COL + slot * SLOT_STRIDE


def assign_iteration_to_slot(settings: ParserSettings, iteration_number: int) -> ParserSettings:
    """При старте итерации N записать N в слот (N-1)%3."""
    from dataclasses import replace

    slot = iteration_slot_index(iteration_number)
    ids = slot_iteration_ids(settings)
    ids[slot] = iteration_number
    return replace(
        settings,
        iteration_slot_0=ids[0],
        iteration_slot_1=ids[1],
        iteration_slot_2=ids[2],
    )


def rebuild_logs_sheet(base_dir: Path, settings: ParserSettings, sh: Any, urls: list[str]) -> Any:
    """Полная пересборка листа логов: новый заголовок итераций + столбец URL."""
    from gspread.utils import rowcol_to_a1

    from google_sheets.links import load_url_list

    if not urls:
        urls = load_url_list(base_dir, settings, sh)
    if not urls:
        raise RuntimeError("Нет URL для листа логов (проверьте «ссылки» или «сдаваемость по дням»).")

    ws = ensure_worksheet(sh, settings.sheet_logs, rows=max(3000, len(urls) + 5), cols=20)
    header = build_logs_header(settings)
    body = [header]
    for url in urls:
        body.append([url.strip()] + [""] * (len(header) - 1))
    end = rowcol_to_a1(len(body), len(header))

    def _write() -> None:
        ws.clear()
        ws.update(f"A1:{end}", body, value_input_option="USER_ENTERED")

    api_retry(_write)
    print(
        f"Лист «{settings.sheet_logs}» пересобран: {len(urls)} URL, "
        f"блоки итераций {slot_iteration_ids(settings)} "
        f"(текущая итерация {settings.parse_iteration})."
    )
    return ws


def ensure_logs_sheet(sh: Any, settings: ParserSettings, urls: list[str]) -> Any:
    from gspread.utils import rowcol_to_a1

    ws = ensure_worksheet(sh, settings.sheet_logs, rows=max(3000, len(urls) + 5), cols=20)
    header = build_logs_header(settings)
    rows = ws.get_all_values()

    if not rows or (rows[0][0] if rows[0] else "") != "Объявление":
        body = [header]
        for url in urls:
            body.append([url.strip()] + [""] * (len(header) - 1))
        end = rowcol_to_a1(len(body), len(header))

        def _init() -> None:
            ws.clear()
            ws.update(f"A1:{end}", body, value_input_option="USER_ENTERED")

        api_retry(_init)
        print(f"Лист «{settings.sheet_logs}»: заголовки итераций {slot_iteration_ids(settings)}.")
        return ws

    if rows[0] != header:
        old_rows = rows[1:]
        body = [header]
        for row in old_rows:
            url = row[0] if row else ""
            padded = [url] + [""] * (len(header) - 1)
            for j in range(1, min(len(row), len(padded))):
                padded[j] = row[j]
            body.append(padded)
        end = rowcol_to_a1(len(body), len(header))

        def _hdr() -> None:
            ws.update(f"A1:{end}", body, value_input_option="USER_ENTERED")

        api_retry(_hdr)

    col_a = ws.col_values(1)
    existing = {canon_url(c) for c in col_a[1:] if c}
    to_add = [[u.strip()] + [""] * (len(header) - 1) for u in urls if canon_url(u) not in existing]
    if to_add:
        def _append() -> None:
            ws.append_rows(to_add, value_input_option="USER_ENTERED")

        api_retry(_append)
    return ws


def _slot_columns(settings: ParserSettings, iteration_number: int) -> tuple[int, int, int]:
    """1-based: колонки дата, время, статус для данной итерации."""
    slot = iteration_slot_index(iteration_number)
    base = slot_base_column(slot)
    return base + 1, base + 2, base + 3


def write_log_entry(
    sh: Any,
    settings: ParserSettings,
    listing_url: str,
    *,
    status: str,
    ok: bool = True,
) -> None:
    from gspread.utils import rowcol_to_a1

    ws = ensure_worksheet(sh, settings.sheet_logs, rows=3000, cols=20)
    ensure_logs_sheet(sh, settings, [listing_url])

    it = max(1, settings.parse_iteration)
    col_date, col_time, col_status = _slot_columns(settings, it)
    row = find_row_by_url_col_a(ws, listing_url)
    now = datetime.now(tz_moscow())
    date_str = now.strftime("%d.%m.%Y")
    time_str = now.strftime("%H:%M:%S")

    if ok and status not in (LOG_STATUS_FAIL, LOG_STATUS_NOT_FOUND):
        status = LOG_STATUS_OK

    def _write() -> None:
        ws.batch_update(
            [
                {"range": rowcol_to_a1(row, col_date), "values": [[date_str]]},
                {"range": rowcol_to_a1(row, col_time), "values": [[time_str]]},
                {"range": rowcol_to_a1(row, col_status), "values": [[status]]},
            ],
            value_input_option="USER_ENTERED",
        )

    api_retry(_write)


def _log_status_by_url(sh: Any, settings: ParserSettings) -> dict[str, str]:
    """canon URL → статус в колонках текущей итерации (пусто = ещё не парсили)."""
    ws = get_worksheet(sh, settings.sheet_logs)
    if ws is None:
        return {}
    rows = ws.get_all_values()
    if len(rows) < 2:
        return {}

    it = max(1, settings.parse_iteration)
    _, _, col_status = _slot_columns(settings, it)
    idx_status = col_status - 1

    out: dict[str, str] = {}
    for row in rows[1:]:
        if not row or not (row[0] or "").strip():
            continue
        status = row[idx_status] if len(row) > idx_status else ""
        out[canon_url(row[0])] = (status or "").strip()
    return out


def resolve_start_index_from_logs(
    sh: Any,
    settings: ParserSettings,
    queue: list[str],
) -> int:
    """
    Первая ссылка в очереди без статуса в логах для parse_iteration.
    Если логи стёрты — снова с 0, даже при iteration_progress > 0.
    """
    if not queue:
        return 0
    if not settings.run_calendar:
        return max(0, settings.iteration_progress)

    status_by_url = _log_status_by_url(sh, settings)
    for i, url in enumerate(queue):
        if not status_by_url.get(canon_url(url), ""):
            return i
    return len(queue)


def slice_queue_for_resume(
    sh: Any,
    queue: list[str],
    settings: ParserSettings,
) -> tuple[list[str], int]:
    stored = max(0, settings.iteration_progress)
    from_logs = resolve_start_index_from_logs(sh, settings, queue)
    start = from_logs

    if start != stored:
        save_settings_values(
            sh,
            settings,
            {"iteration_progress": str(start)},
        )
        if stored > 0 and start < stored:
            print(
                f"Итерация {settings.parse_iteration}: в настройках прогресс {stored}, "
                f"в логах заполнено {start} — начинаем с ссылки {start + 1}."
            )
        elif stored > 0 and start > stored:
            print(
                f"Итерация {settings.parse_iteration}: прогресс в настройках "
                f"обновлён {stored} → {start} по логам."
            )

    if start >= len(queue):
        return [], start
    if start > 0:
        print(
            f"Итерация {settings.parse_iteration}: продолжение с ссылки "
            f"{start + 1}/{len(queue)} (в логах уже {start})."
        )
    return queue[start:], start


def save_iteration_progress(sh: Any, settings: ParserSettings, next_index: int) -> None:
    save_settings_values(
        sh,
        settings,
        {"iteration_progress": str(next_index)},
    )


def begin_iteration(sh: Any, settings: ParserSettings) -> ParserSettings:
    it = max(1, settings.parse_iteration)
    settings = assign_iteration_to_slot(settings, it)
    save_settings_values(
        sh,
        settings,
        {
            "parse_iteration": str(it),
            "iteration_slot_0": str(settings.iteration_slot_0),
            "iteration_slot_1": str(settings.iteration_slot_1),
            "iteration_slot_2": str(settings.iteration_slot_2),
            "iteration_status": "running",
        },
    )
    return settings


def complete_iteration(sh: Any, settings: ParserSettings, *, total_links: int) -> ParserSettings:
    """Все ссылки итерации обработаны — следующая итерация, прогресс 0."""
    from dataclasses import replace

    old_it = max(1, settings.parse_iteration)
    new_it = old_it + 1
    settings = assign_iteration_to_slot(replace(settings, parse_iteration=new_it), new_it)
    save_settings_values(
        sh,
        settings,
        {
            "parse_iteration": str(new_it),
            "iteration_progress": "0",
            "iteration_status": "complete",
            "iteration_slot_0": str(settings.iteration_slot_0),
            "iteration_slot_1": str(settings.iteration_slot_1),
            "iteration_slot_2": str(settings.iteration_slot_2),
        },
    )
    print(
        f"Итерация {old_it} завершена ({total_links} ссылок). "
        f"Следующий запуск — итерация {new_it}, слоты {slot_iteration_ids(settings)}."
    )
    return replace(settings, parse_iteration=new_it, iteration_progress=0)
