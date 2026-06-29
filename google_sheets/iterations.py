"""Итерации парсинга: возобновление, кольцо из 3 слотов, логи."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

_queue_len: int = 0
_logs_master_urls: list[str] = []


def set_queue_len(n: int) -> None:
    global _queue_len
    _queue_len = max(0, n)


def set_logs_master_urls(urls: list[str]) -> None:
    global _logs_master_urls
    _logs_master_urls = list(urls)


def get_logs_master_urls() -> list[str]:
    return _logs_master_urls


def current_queue_len() -> int | None:
    return _queue_len if _queue_len > 0 else None

from google_sheets.calendar import today_moscow, tz_moscow
from google_sheets.link_index import (
    FIRST_DATA_ROW,
    index_to_row,
    last_data_row,
    progress_to_start_index,
    row_to_index,
)
from google_sheets.client import (
    api_retry,
    canon_url,
    ensure_worksheet,
    get_worksheet,
    trim_worksheet_rows,
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
    ids = list(slot_iteration_ids(settings))
    ids[slot] = iteration_number
    return replace(
        settings,
        iteration_slot_0=ids[0],
        iteration_slot_1=ids[1],
        iteration_slot_2=ids[2],
    )


def iteration_settings_payload(settings: ParserSettings) -> dict[str, str]:
    it = max(1, settings.parse_iteration)
    return {
        "parse_iteration": str(it),
        "iteration_progress": str(settings.iteration_progress),
        "iteration_progress_for": str(max(1, settings.iteration_progress_for)),
        "iteration_logs_cleared_for": str(settings.iteration_logs_cleared_for),
        "iteration_slot_0": str(settings.iteration_slot_0),
        "iteration_slot_1": str(settings.iteration_slot_1),
        "iteration_slot_2": str(settings.iteration_slot_2),
    }


def persist_iteration_settings(sh: Any, settings: ParserSettings) -> None:
    from google_sheets.settings import clear_settings_row_cache, save_settings_values

    clear_settings_row_cache()
    save_settings_values(sh, settings, iteration_settings_payload(settings))


def reconcile_iteration_change(
    settings: ParserSettings,
    *,
    announce: bool = False,
) -> tuple[ParserSettings, bool]:
    """
    Единственный ручной рычаг — parse_iteration.
    Если номер итерации сменился (≠ iteration_progress_for) — сброс прогресса и слотов логов.
    """
    from dataclasses import replace

    it = max(1, settings.parse_iteration)
    prog_for = max(1, settings.iteration_progress_for)
    if prog_for == it:
        return assign_iteration_to_slot(settings, it), False

    old_prog = settings.iteration_progress
    settings = assign_iteration_to_slot(
        replace(
            settings,
            parse_iteration=it,
            iteration_progress=0,
            iteration_progress_for=it,
            iteration_logs_cleared_for=0,
        ),
        it,
    )
    if announce:
        print(
            f"Смена итерации {prog_for} → {it}: iteration_progress сброшен "
            f"(было строка {old_prog or 2}); слоты логов {slot_iteration_ids(settings)}."
        )
    return settings, True


RE_LOG_DATE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$")
RE_LOG_TIME = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
_LOG_STATUSES = frozenset({"ок", "фейл", "нет на сайте"})


def _log_column_kind(col_1based: int) -> str:
    """label | date | time | status для столбца внутри блока итерации."""
    for slot in range(3):
        base = slot_base_column(slot)
        if col_1based == base:
            return "label"
        if col_1based == base + 1:
            return "date"
        if col_1based == base + 2:
            return "time"
        if col_1based == base + 3:
            return "status"
    return "other"


def _looks_like_url(val: str) -> bool:
    s = (val or "").strip().lower()
    return s.startswith("http") or "avito.ru" in s


def _sanitize_log_cell(val: str, kind: str) -> str:
    """Не переносим в лог ссылки и прочий мусор из старых битых ячеек."""
    s = (val or "").strip()
    if not s or _looks_like_url(s):
        return ""
    if kind == "label":
        return s if s.startswith("ит.") else ""
    if kind == "date":
        return s if RE_LOG_DATE.match(s) else ""
    if kind == "time":
        return s if RE_LOG_TIME.match(s) else ""
    if kind == "status":
        return s if s in _LOG_STATUSES else ""
    return ""


def _iteration_log_column_indices() -> list[int]:
    """1-based: все колонки блоков итераций (ит., дата, время, статус) × 3 слота."""
    cols: list[int] = []
    for slot in range(3):
        base = slot_base_column(slot)
        cols.extend(range(base, base + COLS_PER_SLOT))
    return cols


def _find_url_in_row(row: list[str]) -> str:
    """Канон URL только из столбца A (не из ячеек итераций)."""
    if row:
        return canon_url(row[0] or "")
    return ""


def _read_logs_rows_by_url(ws: Any) -> dict[str, list[str]]:
    """Старые строки лога по canon URL (для переноса ячеек итераций)."""
    rows = ws.get_all_values()
    out: dict[str, list[str]] = {}
    for row in rows[1:]:
        c = _find_url_in_row(row)
        if c and c not in out:
            out[c] = row
    return out


def sync_logs_sheet(sh: Any, settings: ParserSettings, urls: list[str]) -> Any:
    """
    Лист логов = лист «ссылки» построчно: строка 2 → 1-я ссылка, без append и без лишних строк.
    Столбец A — URL; блоки итераций в C–F / H–K / M–P.
    """
    from gspread.utils import rowcol_to_a1

    from google_sheets.link_index import last_data_row

    if not urls:
        return get_worksheet(sh, settings.sheet_logs)

    n_urls = len(urls)
    need_rows = last_data_row(n_urls)
    header = build_logs_header(settings)
    ncols = len(header)

    ws = ensure_worksheet(sh, settings.sheet_logs, rows=max(need_rows + 5, 3000), cols=20)
    old_by_url = _read_logs_rows_by_url(ws)
    log_cols = _iteration_log_column_indices()

    body: list[list[str]] = [header]
    for url in urls:
        c = canon_url(url)
        old = old_by_url.get(c, [])
        row = [url.strip()] + [""] * (ncols - 1)
        if old:
            for j in log_cols:
                idx = j - 1
                if idx >= len(old):
                    continue
                clean = _sanitize_log_cell(old[idx], _log_column_kind(j))
                if clean:
                    row[idx] = clean
        body.append(row)

    end = rowcol_to_a1(need_rows, ncols)

    def _write() -> None:
        ws.clear()
        ws.update(f"A1:{end}", body, value_input_option="USER_ENTERED")

    api_retry(_write)
    trimmed = trim_worksheet_rows(ws, need_rows)
    set_logs_master_urls(urls)
    msg = (
        f"Лист «{settings.sheet_logs}»: {n_urls} строк (как «{settings.sheet_links}»), "
        f"блоки {slot_iteration_ids(settings)}."
    )
    if trimmed:
        msg += f" Удалено лишних строк: {trimmed}."
    print(msg)
    return ws


def rebuild_logs_sheet(base_dir: Path, settings: ParserSettings, sh: Any, urls: list[str]) -> Any:
    """Полная пересборка листа логов (то же, что sync_logs_sheet)."""
    from google_sheets.links import load_logs_master_urls

    if not urls:
        urls = load_logs_master_urls(base_dir, settings, sh)
    if not urls:
        raise RuntimeError("Нет URL для листа логов (проверьте «ссылки»).")
    return sync_logs_sheet(sh, settings, urls)


def ensure_logs_sheet(sh: Any, settings: ParserSettings, urls: list[str]) -> Any:
    """Совместимость: синхронизация с мастер-списком ссылок."""
    master = get_logs_master_urls()
    if master:
        return sync_logs_sheet(sh, settings, master)
    return sync_logs_sheet(sh, settings, urls)


def _slot_columns(settings: ParserSettings, iteration_number: int) -> tuple[int, int, int]:
    """1-based: колонки дата, время, статус для данной итерации."""
    slot = iteration_slot_index(iteration_number)
    base = slot_base_column(slot)
    return base + 1, base + 2, base + 3


def _should_clear_logs_slot(settings: ParserSettings, iteration_number: int) -> bool:
    """Новая итерация или iteration_progress=0 — очистить блок логов слота."""
    it = max(1, iteration_number)
    if settings.iteration_logs_cleared_for != it:
        return True
    if settings.iteration_progress <= 0:
        return True
    return False


def clear_iteration_slot_logs(
    sh: Any,
    settings: ParserSettings,
    iteration_number: int,
) -> None:
    """Очистить блок ит./дата/время/статус для слота данной итерации (все строки URL)."""
    from gspread.utils import rowcol_to_a1

    ws = get_worksheet(sh, settings.sheet_logs)
    if ws is None:
        return

    it = max(1, iteration_number)
    slot = iteration_slot_index(it)
    base = slot_base_column(slot)
    cols = (base, base + 1, base + 2, base + 3)

    rows = ws.get_all_values()
    if len(rows) < 2:
        return

    n_rows = len(rows)
    body: list[dict] = [
        {
            "range": rowcol_to_a1(1, base),
            "values": [[f"ит.{it}"]],
        },
    ]
    for col in cols:
        body.append(
            {
                "range": f"{rowcol_to_a1(2, col)}:{rowcol_to_a1(n_rows, col)}",
                "values": [[""] for _ in range(n_rows - 1)],
            }
        )

    def _write() -> None:
        ws.batch_update(body, value_input_option="USER_ENTERED")

    api_retry(_write)
    slot_cols = "CDEF" if slot == 0 else ("HIJK" if slot == 1 else "MNOP")
    print(
        f"Лист «{settings.sheet_logs}»: очищен блок итерации {it} "
        f"(слот {slot + 1}, столбцы {slot_cols})."
    )


def write_log_entry(
    sh: Any,
    settings: ParserSettings,
    listing_url: str,
    *,
    status: str,
    ok: bool = True,
    sheet_row: int | None = None,
) -> None:
    from gspread.utils import rowcol_to_a1
    from google_sheets.sheet_session import get_parse_sheet_context

    ctx = get_parse_sheet_context()
    active_settings = ctx.settings if ctx is not None else settings
    if ctx is not None:
        ws = ctx.logs_ws
    else:
        ws = ensure_worksheet(sh, active_settings.sheet_logs, rows=3000, cols=20)

    row: int | None = None
    c = canon_url(listing_url)
    if ctx is not None:
        row = ctx.logs_index.row_for(listing_url)
    if row is None and c:
        master = get_logs_master_urls()
        for i, u in enumerate(master):
            if canon_url(u) == c:
                row = index_to_row(i)
                break
    if row is None:
        row = sheet_row
    if row is None or row < FIRST_DATA_ROW:
        print(
            f"  лог: пропуск (нет строки для URL, row={row}) "
            f"{listing_url[:60]}…"
        )
        return

    it = max(1, active_settings.parse_iteration)
    col_date, col_time, col_status = _slot_columns(active_settings, it)
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
    """
    if not queue:
        return 0
    if not settings.run_calendar:
        return progress_to_start_index(settings.iteration_progress)

    status_by_url = _log_status_by_url(sh, settings)
    for i, url in enumerate(queue):
        if not status_by_url.get(canon_url(url), ""):
            return i
    return len(queue)


def roll_to_next_iteration_if_queue_done(
    sh: Any,
    settings: ParserSettings,
    queue: list[str],
) -> ParserSettings:
    """
    Прошлый прогон дошёл до конца (в настройках строка за пределами списка
    или все строки с «ок» в логах) — новая итерация, iteration_progress=0.
    """
    from dataclasses import replace

    n = len(queue)
    if n == 0:
        return settings

    stored_idx = progress_to_start_index(settings.iteration_progress)
    need_roll = stored_idx >= n

    if not need_roll and settings.run_calendar:
        from_logs = resolve_start_index_from_logs(sh, settings, queue)
        if from_logs >= n and stored_idx == 0:
            need_roll = True

    if not need_roll:
        return settings

    old_it = max(1, settings.parse_iteration)
    settings = complete_iteration(sh, settings, total_links=n)
    settings = begin_iteration(sh, replace(settings, iteration_logs_cleared_for=0), urls=queue)
    print(
        f"Новый запуск: итерация {old_it} была завершена, "
        f"старт итерации {settings.parse_iteration} с iteration_progress=0 (строка 2)."
    )
    return settings


def slice_queue_for_resume(
    sh: Any,
    queue: list[str],
    settings: ParserSettings,
) -> tuple[list[str], int, ParserSettings]:
    """
    Старт очереди: max(логи, iteration_progress).
    iteration_progress: 0 = с начала; иначе номер строки листа.
    """
    settings = roll_to_next_iteration_if_queue_done(sh, settings, queue)

    stored_idx = progress_to_start_index(settings.iteration_progress)
    stored_row = index_to_row(stored_idx) if stored_idx > 0 else 0
    from_logs = (
        resolve_start_index_from_logs(sh, settings, queue)
        if settings.run_calendar
        else stored_idx
    )
    start_idx = max(from_logs, stored_idx) if settings.run_calendar else stored_idx

    if settings.run_calendar and start_idx > stored_idx:
        new_row = index_to_row(start_idx)
        save_settings_values(
            sh,
            settings,
            {
                "iteration_progress": str(new_row),
                "iteration_progress_for": str(max(1, settings.parse_iteration)),
            },
        )
        print(
            f"Итерация {settings.parse_iteration}: строка в настройках "
            f"{stored_row or 0} → {new_row} по логам."
        )

    if start_idx >= len(queue):
        return [], start_idx, settings

    if start_idx > 0:
        row = index_to_row(start_idx)
        from_row = index_to_row(from_logs)
        if stored_idx > from_logs:
            print(
                f"Итерация {settings.parse_iteration}: старт со строки {row} "
                f"(в настройках {stored_row}; в логах до {from_row})."
            )
        else:
            print(
                f"Итерация {settings.parse_iteration}: продолжение со строки {row} "
                f"(в логах до {from_row})."
            )
    return queue[start_idx:], start_idx, settings


def save_iteration_progress(
    sh: Any,
    settings: ParserSettings,
    next_sheet_row: int,
    *,
    total_urls: int | None = None,
    announce: bool = False,
) -> ParserSettings:
    """next_sheet_row — следующая строка после обработанной; при конце списка — новая итерация."""
    from dataclasses import replace

    it = max(1, settings.parse_iteration)
    if total_urls and next_sheet_row > last_data_row(total_urls):
        return complete_iteration(sh, settings, total_links=total_urls)

    if announce:
        from google_sheets.settings import clear_settings_row_cache

        clear_settings_row_cache()

    save_settings_values(
        sh,
        settings,
        {
            "iteration_progress": str(next_sheet_row),
            "iteration_progress_for": str(it),
        },
    )
    updated = replace(
        settings,
        iteration_progress=next_sheet_row,
        iteration_progress_for=it,
    )
    if announce:
        print(
            f"В настройках: iteration_progress={next_sheet_row}, "
            f"iteration_progress_for={it} (итерация {it})."
        )
    return updated


def begin_iteration(
    sh: Any,
    settings: ParserSettings,
    *,
    urls: list[str] | None = None,
) -> ParserSettings:
    from dataclasses import replace

    settings, user_changed = reconcile_iteration_change(settings, announce=True)
    it = max(1, settings.parse_iteration)

    if settings.run_calendar:
        master = get_logs_master_urls() or list(urls or [])
        if master:
            sync_logs_sheet(sh, settings, master)

    if settings.run_calendar and _should_clear_logs_slot(settings, it):
        clear_iteration_slot_logs(sh, settings, it)
        settings = replace(settings, iteration_logs_cleared_for=it)

    persist_iteration_settings(sh, settings)

    if user_changed:
        print(
            f"В настройках синхронизированы iteration_* для итерации {it} "
            f"(менять вручную нужно только parse_iteration)."
        )
    return settings


def complete_iteration(sh: Any, settings: ParserSettings, *, total_links: int) -> ParserSettings:
    """Все ссылки итерации обработаны — следующая итерация, iteration_progress=0."""
    from dataclasses import replace

    old_it = max(1, settings.parse_iteration)
    new_it = old_it + 1
    settings = assign_iteration_to_slot(replace(settings, parse_iteration=new_it), new_it)
    updated = replace(
        settings,
        parse_iteration=new_it,
        iteration_progress=0,
        iteration_progress_for=new_it,
        iteration_logs_cleared_for=0,
    )
    persist_iteration_settings(sh, updated)
    print(
        f"Итерация {old_it} завершена ({total_links} ссылок). "
        f"В настройках: parse_iteration={new_it}, iteration_progress=0 "
        f"(следующий запуск — со строки 2)."
    )
    return updated
