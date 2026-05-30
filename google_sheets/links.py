"""Лист «ссылки» и построение очереди парсинга."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google_sheets.client import (
    api_retry,
    canon_url,
    delete_worksheet_rows,
    ensure_worksheet,
    get_worksheet,
    open_spreadsheet,
    orphan_url_row_indices,
)
from google_sheets.detail import (
    detail_title_by_url,
    rebuild_detail_sheet,
    title_needs_parse,
)
from google_sheets.calendar import init_calendar_sheet
from google_sheets.settings import ParserSettings


def _read_urls_col_a(ws: Any, sheet_title: str) -> list[str]:
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
        canon = canon_url(line)
        if canon in seen:
            continue
        seen.add(canon)
        out.append(line)
    print(f"URL из «{sheet_title}»: {len(out)} шт.")
    return out


def load_urls_from_links_sheet(base_dir: Path, settings: ParserSettings, sh: Any | None = None) -> list[str]:
    workbook = sh or open_spreadsheet(base_dir)
    ws = get_worksheet(workbook, settings.sheet_links)
    if ws is None:
        raise RuntimeError(
            f'В таблице нет листа «{settings.sheet_links}». Создайте его с URL в столбце A.'
        )
    return _read_urls_col_a(ws, settings.sheet_links)


def load_urls_from_calendar_sheet(sh: Any, settings: ParserSettings) -> list[str]:
    ws = get_worksheet(sh, settings.sheet_availability)
    if ws is None:
        raise RuntimeError(
            f'Нет листа «{settings.sheet_availability}». '
            "Создайте его или включите sync_from_links_sheet=1."
        )
    return _read_urls_col_a(ws, settings.sheet_availability)


def apply_link_range(urls: list[str], settings: ParserSettings) -> list[str]:
    """Диапазон индексов [from, to): 0+0 = все; 0+1 = одна ссылка; 0+10 = десять."""
    if not urls:
        return []
    start = max(0, settings.detail_range_from)
    end = settings.detail_range_to
    if start == 0 and end == 0:
        return list(urls)
    if end < 0:
        return urls[start:]
    if end <= start:
        return []
    sliced = urls[start:end]
    print(f"Диапазон ссылок [{start}:{end}) → {len(sliced)} шт.")
    return sliced


def load_url_list(base_dir: Path, settings: ParserSettings, sh: Any) -> list[str]:
    if settings.sync_from_links_sheet:
        urls = load_urls_from_links_sheet(base_dir, settings, sh=sh)
    else:
        urls = load_urls_from_calendar_sheet(sh, settings)
    return apply_link_range(urls, settings)


def remove_orphan_urls_from_worksheet(ws: Any, allowed_canon: set[str]) -> int:
    """Удалить строки с URL, которых нет в списке «ссылки» (реальное смещение строк)."""
    rows = orphan_url_row_indices(ws, allowed_canon)
    return delete_worksheet_rows(ws, rows)


def remove_orphan_urls_from_work_sheets(
    sh: Any,
    settings: ParserSettings,
    urls: list[str],
) -> int:
    """Со всех рабочих листов убрать URL, отсутствующие на листе «ссылки»."""
    allowed = {canon_url(u) for u in urls if canon_url(u)}
    if not allowed:
        return 0

    targets = (
        settings.sheet_availability,
        settings.sheet_prices,
        settings.sheet_logs,
        settings.sheet_detail,
    )

    total = 0
    seen_titles: set[str] = set()
    for title in targets:
        if title in seen_titles:
            continue
        seen_titles.add(title)
        ws = get_worksheet(sh, title)
        if ws is None:
            continue
        n = remove_orphan_urls_from_worksheet(ws, allowed)
        if n:
            print(f"  «{title}»: удалено {n} строк (нет в «{settings.sheet_links}»)")
            total += n
    return total


def ensure_urls_on_worksheet(ws: Any, urls: list[str]) -> int:
    col_a = ws.col_values(1)
    existing = {canon_url(c) for c in col_a[1:] if c}
    headers = ws.row_values(1) or ["Объявление"]
    num_cols = max(len(headers), 1)

    to_add: list[list[str]] = []
    for url in urls:
        canon = canon_url(url)
        if not canon or canon in existing:
            continue
        to_add.append([url.strip()] + [""] * (num_cols - 1))
        existing.add(canon)

    if not to_add:
        return 0

    def _do() -> None:
        ws.append_rows(to_add, value_input_option="USER_ENTERED")

    api_retry(_do)
    return len(to_add)


def ensure_urls_on_work_sheets(
    sh: Any,
    settings: ParserSettings,
    urls: list[str],
    *,
    include_detail: bool = True,
    include_calendar: bool = True,
    include_logs: bool = False,
    links_master: list[str] | None = None,
) -> None:
    if not urls and not links_master:
        return
    if settings.sync_from_links_sheet:
        master = links_master if links_master is not None else urls
        removed = remove_orphan_urls_from_work_sheets(sh, settings, master)
        if removed:
            print(
                f"Синхронизация с «{settings.sheet_links}»: всего удалено {removed} строк "
                f"на рабочих листах."
            )

    urls_to_sync = links_master if (settings.sync_from_links_sheet and links_master) else urls
    targets: list[str] = []
    if include_detail:
        targets.append(settings.sheet_detail)
    if include_calendar:
        targets.extend([settings.sheet_availability, settings.sheet_prices])
    if include_logs:
        targets.append(settings.sheet_logs)
    for title in targets:
        ws = ensure_worksheet(sh, title, rows=3000, cols=120)
        added = ensure_urls_on_worksheet(ws, urls_to_sync)
        if added:
            print(f"  «{title}»: +{added} строк с URL")


def prepare_workbook(
    base_dir: Path,
    settings: ParserSettings,
    export_columns: list[str],
    sh: Any,
    all_urls: list[str],
) -> None:
    if settings.sync_from_links_sheet:
        full_links = load_urls_from_links_sheet(base_dir, settings, sh=sh)
        if settings.detail_fill_mode in ("primary", "rebuild") and settings.run_detail:
            rebuild_detail_sheet(sh, settings, full_links, export_columns)
        if settings.calendar_mode == "primary" and settings.run_calendar:
            init_calendar_sheet(sh, settings, settings.sheet_availability, full_links)
            init_calendar_sheet(sh, settings, settings.sheet_prices, full_links)


def build_parse_queue(
    base_dir: Path,
    settings: ParserSettings,
    export_columns: list[str],
) -> tuple[Any, list[str]]:
    sh = open_spreadsheet(base_dir)
    links_master: list[str] | None = None
    if settings.sync_from_links_sheet:
        links_master = load_urls_from_links_sheet(base_dir, settings, sh=sh)
        all_urls = apply_link_range(links_master, settings)
    else:
        all_urls = load_url_list(base_dir, settings, sh)
    if not all_urls:
        print("Список URL пуст после применения диапазона.")
        return sh, []

    prepare_workbook(base_dir, settings, export_columns, sh, all_urls)

    sync_kw = {"links_master": links_master} if links_master is not None else {}

    if settings.run_calendar and not settings.run_detail:
        if settings.sync_from_links_sheet:
            ensure_urls_on_work_sheets(
                sh,
                settings,
                all_urls,
                include_detail=False,
                include_calendar=True,
                include_logs=True,
                **sync_kw,
            )
        print(f"Календарь: к парсингу {len(all_urls)}.")
        return sh, all_urls

    if settings.run_detail and settings.run_calendar:
        if settings.sync_from_links_sheet:
            ensure_urls_on_work_sheets(
                sh,
                settings,
                all_urls,
                include_detail=True,
                include_calendar=True,
                include_logs=True,
                **sync_kw,
            )
        queue = _filter_detail_queue(sh, settings, all_urls)
        print(f"Деталь + календарь: к парсингу {len(queue)}.")
        return sh, queue

    if settings.run_detail:
        if settings.sync_from_links_sheet:
            ensure_urls_on_work_sheets(
                sh,
                settings,
                all_urls,
                include_detail=True,
                include_calendar=False,
                include_logs=settings.run_calendar,
                **sync_kw,
            )
        queue = _filter_detail_queue(sh, settings, all_urls)
        print(f"Деталь: к парсингу {len(queue)}.")
        return sh, queue

    print("run_detail=0 и run_calendar=0 — нечего парсить.")
    return sh, []


def _filter_detail_queue(sh: Any, settings: ParserSettings, urls: list[str]) -> list[str]:
    mode = settings.detail_fill_mode
    if mode in ("primary", "rebuild"):
        return list(urls)
    if mode == "append" and settings.detail_only_empty_title:
        titles = detail_title_by_url(sh, settings)
        return [u for u in urls if title_needs_parse(titles.get(canon_url(u), ""))]
    return list(urls)
