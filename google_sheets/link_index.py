"""Нумерация как в Google Таблице: в настройках — номер СТРОКИ листа (строка 1 = заголовки)."""

from __future__ import annotations

RANGE_ALL_FROM = 0
RANGE_ALL_TO = 0
HEADER_ROW = 1
FIRST_DATA_ROW = 2


def row_to_index(row: int) -> int:
    """Строка листа → индекс в очереди URL [0..]. Строка 2 → 0."""
    return max(0, row - FIRST_DATA_ROW)


def index_to_row(index: int) -> int:
    """Индекс в очереди → строка листа."""
    return index + FIRST_DATA_ROW


def normalize_row_from_settings(raw: int) -> int:
    """Значение из «настройки»: 0 = с начала (строка 2); иначе номер строки листа."""
    if raw <= 0:
        return 0
    return raw


def progress_to_start_index(progress: int) -> int:
    """iteration_progress → индекс в очереди [0..]."""
    row = normalize_row_from_settings(progress)
    if row <= 0:
        return 0
    return row_to_index(row)


def last_data_row(url_count: int) -> int:
    """Последняя строка с URL при url_count ссылках."""
    if url_count <= 0:
        return FIRST_DATA_ROW
    return url_count + HEADER_ROW


def slice_queue_by_range(urls: list[str], range_from: int, range_to: int) -> list[str]:
    """
    Диапазон — номера строк листа (включительно):
      0 + 0 — все;
      2188 + 0 — с строки 2188 до конца;
      2188 + 2200 — строки 2188…2200.
    """
    if not urls:
        return []
    f, t = range_from, range_to
    if f == RANGE_ALL_FROM and t == RANGE_ALL_TO:
        return list(urls)
    start = row_to_index(normalize_row_from_settings(f))
    if t == RANGE_ALL_TO:
        sliced = urls[start:]
        if sliced:
            print(
                f"Диапазон: строки {index_to_row(start)}–{last_data_row(len(urls))} "
                f"({len(sliced)} шт.)"
            )
        return sliced
    if f >= FIRST_DATA_ROW and t >= FIRST_DATA_ROW:
        if t < f:
            return []
        end_idx = row_to_index(t)
        sliced = urls[start : end_idx + 1]
        print(f"Диапазон: строки {f}–{t} ({len(sliced)} шт.)")
        return sliced
    # устар.: полуинтервал по индексам
    if t <= start:
        return []
    sliced = urls[start:t]
    print(f"Диапазон [{start}:{t}) → {len(sliced)} шт.")
    return sliced


def format_link_console(
    sheet_row: int,
    last_row: int,
    *,
    batch_pos: int | None = None,
    batch_total: int | None = None,
) -> str:
    if batch_pos is not None and batch_total is not None:
        return f"строка {sheet_row}/{last_row} [{batch_pos}/{batch_total}]"
    return f"строка {sheet_row}/{last_row}"
