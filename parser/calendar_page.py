"""Открытие/закрытие datepicker Avito на карточке объявления."""

from __future__ import annotations

import random

from parser.calendar_availability import wait_datepicker_ready

CALENDAR_TRIGGER_SELECTORS: tuple[str, ...] = (
    'label:has(input[name="date"]) span._4bb4300b95c2563e',
    'span._4bb4300b95c2563e:has(svg[data-icon-name="calendar"])',
    'label:has(input[name="date"])',
    'input[name="date"][readonly]',
    'input[name="date"]',
)


def _pause(page, min_s: float = 0.1, max_s: float = 0.25) -> None:
    page.wait_for_timeout(int(random.uniform(min_s, max_s) * 1000))


def has_calendar_on_page(page) -> bool:
    """Поле дат / триггер календаря есть в DOM (без долгого ожидания)."""
    for sel in CALENDAR_TRIGGER_SELECTORS:
        try:
            if page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


def scroll_to_calendar(page) -> bool:
    """Прокрутка к полю дат / триггеру календаря."""
    for sel in CALENDAR_TRIGGER_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0:
                loc.scroll_into_view_if_needed(timeout=3000)
                return True
        except Exception:
            continue
    return False


def click_calendar_trigger(page) -> str | None:
    for sel in CALENDAR_TRIGGER_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0 or not loc.is_visible(timeout=800):
                continue
            loc.scroll_into_view_if_needed(timeout=3500)
            _pause(page)
            loc.click(timeout=3500)
            return sel
        except Exception:
            continue
    return None


def _datepicker_ready(
    page,
    timeout_ms: int,
    *,
    min_day_labels: int = 14,
    min_panels: int = 2,
) -> bool:
    return wait_datepicker_ready(
        page,
        timeout_ms=timeout_ms,
        min_day_labels=min_day_labels,
        min_panels=min_panels,
    )


def open_calendar_popup(
    page,
    sheet_row: int,
    item_id: str,
    *,
    after_open_wait_s: float = 1.0,
    ready_timeout_ms: int = 3500,
    quiet: bool = False,
) -> str | None:
    """Открыть datepicker. Возвращает селектор только если панели с днями догрузились."""
    _ = sheet_row
    _ = item_id

    def _log_fail() -> None:
        if quiet:
            return
        try:
            panels = page.locator('[data-marker^="datepicker/calendar("]').count()
            days = page.locator(
                '[data-marker="datepicker-day-available"], [data-marker="datepicker-day-disabled"]'
            ).count()
            print(f"  календарь не догрузился (панелей={panels}, дней={days})")
        except Exception:
            pass

    if _datepicker_ready(page, timeout_ms=min(2000, ready_timeout_ms)):
        trigger = find_visible_calendar_trigger(page)
        page.wait_for_timeout(int(after_open_wait_s * 1000))
        if _datepicker_ready(page, timeout_ms=ready_timeout_ms):
            return trigger or CALENDAR_TRIGGER_SELECTORS[0]
        _log_fail()
        return None

    for attempt in range(2):
        trigger = click_calendar_trigger(page)
        if not trigger:
            continue
        page.wait_for_timeout(int(after_open_wait_s * 1000))
        if _datepicker_ready(page, timeout_ms=ready_timeout_ms):
            return trigger
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(350)
        except Exception:
            pass

    _log_fail()
    return None


def find_visible_calendar_trigger(page) -> str | None:
    for sel in CALENDAR_TRIGGER_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.count() > 0 and loc.is_visible(timeout=600):
                return sel
        except Exception:
            continue
    return None


def close_calendar_popup(
    page,
    trigger_sel: str,
    *,
    quiet: bool = False,
) -> None:
    _ = quiet
    try:
        for attempt in range(2):
            sel = (
                trigger_sel
                if attempt == 0
                else (find_visible_calendar_trigger(page) or trigger_sel)
            )
            loc = page.locator(sel).first
            if loc.count() == 0 or not loc.is_visible(timeout=1500):
                continue
            loc.scroll_into_view_if_needed(timeout=4000)
            _pause(page)
            loc.click(timeout=8000)
            page.wait_for_timeout(300)
            if not wait_datepicker_ready(page, timeout_ms=1500, min_day_labels=7):
                return
    except Exception:
        pass
