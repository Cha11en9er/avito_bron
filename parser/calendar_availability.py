"""Сдаваемость из datepicker Avito: только 2 месяца (две панели в попапе, без листания вперёд)."""

from __future__ import annotations

import calendar
import time
from datetime import date
from pathlib import Path
from typing import Any, Literal

DayState = Literal["free", "booked", "skip"]

INITIAL_PANEL_COUNT = 2
TOTAL_MONTH_COUNT = 2
EXTRA_MONTH_NAV_CLICKS = 0
DATEPICKER_READY_MS = 2200
DATEPICKER_READY_AFTER_NAV_MS = 4000

NEXT_MONTH_SELECTORS: tuple[str, ...] = (
    '[data-marker="datepicker/next-button"]',
    "button._4c628a411bf08f14._5f2feca9eafe0236",
    "._4c628a411bf08f14._5f2feca9eafe0236",
)

PREV_MONTH_SELECTORS: tuple[str, ...] = (
    '[data-marker="datepicker/prev-button"]',
    '[data-marker="datepicker/previous-button"]',
)

_DATEPICKER_READY_JS = """
(minPanels) => {
  const panels = document.querySelectorAll('[data-marker^="datepicker/calendar("]');
  if (panels.length < minPanels) return 0;
  const sel = '[data-marker="datepicker-day-available"], [data-marker="datepicker-day-disabled"]';
  let n = 0;
  for (let i = 0; i < Math.min(2, panels.length); i++) {
    n += panels[i].querySelectorAll(sel).length;
  }
  return n;
}
"""

_PARSE_JS = """
() => {
  const MONTH_KEYS = [
    ["январ", 1], ["феврал", 2], ["март", 3], ["апрел", 4],
    ["май", 5], ["мая", 5], ["июн", 6], ["июл", 7],
    ["август", 8], ["сентябр", 9], ["октябр", 10], ["ноябр", 11], ["декабр", 12],
  ];
  const parseMonthName = (text) => {
    const t = (text || "").toLowerCase().replace(/\\./g, "").trim();
    for (const [key, num] of MONTH_KEYS) {
      if (t.startsWith(key)) return num;
    }
    return null;
  };
  const panelYearMonth = (panel) => {
    const marker = panel.getAttribute("data-marker") || "";
    let year = null;
    let month = null;
    let panelTitle = "";
    const mm = marker.match(/calendar\\((\\d+)-(\\d+)\\)/);
    if (mm) {
      year = parseInt(mm[1], 10);
      // Avito: месяц в data-marker 0-based (5 = июнь). Даты — 1-based.
      month = parseInt(mm[2], 10) + 1;
    }
    for (const div of panel.querySelectorAll("div")) {
      const spans = Array.from(div.querySelectorAll(":scope > span"));
      if (spans.length < 2) continue;
      const a = (spans[0].textContent || "").trim();
      const b = (spans[1].textContent || "").trim();
      const mA = parseMonthName(a);
      const yB = parseInt(b, 10);
      if (mA && yB > 2000) {
        panelTitle = a + " " + b;
        break;
      }
      const mB = parseMonthName(b);
      const yA = parseInt(a, 10);
      if (mB && yA > 2000) {
        panelTitle = b + " " + a;
        break;
      }
    }
    if (!year || !month) {
      for (const div of panel.querySelectorAll("div")) {
        const spans = Array.from(div.querySelectorAll(":scope > span"));
        if (spans.length < 2) continue;
        const a = (spans[0].textContent || "").trim();
        const b = (spans[1].textContent || "").trim();
        const mA = parseMonthName(a);
        const yB = parseInt(b, 10);
        if (mA && yB > 2000) {
          month = mA;
          year = yB;
          panelTitle = a + " " + b;
          break;
        }
        const mB = parseMonthName(b);
        const yA = parseInt(a, 10);
        if (mB && yA > 2000) {
          month = mB;
          year = yA;
          panelTitle = b + " " + a;
          break;
        }
      }
    }
    if (!panelTitle && month && year) {
      panelTitle = month + "." + year;
    }
    return { year, month, panelTitle, marker };
  };

  const panels = Array.from(
    document.querySelectorAll('[data-marker^="datepicker/calendar("]')
  ).slice(0, 2);
  const out = [];
  for (let panelIndex = 0; panelIndex < panels.length; panelIndex++) {
    const panel = panels[panelIndex];
    const { year, month, panelTitle, marker } = panelYearMonth(panel);
    if (!year || !month) continue;
    for (const td of panel.querySelectorAll('td[data-marker^="datepicker/day("]')) {
      const dm = (td.getAttribute("data-marker") || "").match(/day\\((\\d+)\\)/);
      if (!dm) continue;
      const dayNum = parseInt(dm[1], 10);
      const avail = td.querySelector('[data-marker="datepicker-day-available"]');
      const dis = td.querySelector('[data-marker="datepicker-day-disabled"]');
      let state = "skip";
      let label = "";
      if (avail) {
        state = "free";
        label = (avail.textContent || "").trim();
      } else if (dis) {
        state = "booked";
        label = (dis.textContent || "").trim();
      }
      if (state === "skip") continue;
      out.push({
        panelIndex,
        year,
        month,
        panelTitle,
        marker,
        day: dayNum,
        state,
        label,
      });
    }
  }
  return out;
}
"""


def _prev_month(year: int, month: int) -> tuple[int, int]:
    if month > 1:
        return year, month - 1
    return year - 1, 12


def _next_month(year: int, month: int) -> tuple[int, int]:
    if month < 12:
        return year, month + 1
    return year + 1, 1


def _try_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def resolve_panel_days(
    year: int, month: int, rows: list[dict[str, Any]]
) -> list[tuple[date, DayState]]:
    """Дни в панели по порядку DOM; хвост прошлого/следующего месяца в сетке."""
    py, pm = _prev_month(year, month)
    ny, nm = _next_month(year, month)
    max_cur = calendar.monthrange(year, month)[1]

    phase = "prefix"
    prev_resolved: date | None = None
    out: list[tuple[date, DayState]] = []

    for row in rows:
        state = row.get("state", "skip")
        if state not in ("free", "booked"):
            continue
        day_num = int(row["day"])

        candidates: list[tuple[int, int, int]] = []
        if phase == "prefix":
            if day_num == 1:
                phase = "main"
                candidates = [(year, month, day_num)]
            elif day_num > 20:
                candidates = [(py, pm, day_num), (year, month, day_num)]
            else:
                candidates = [(year, month, day_num), (py, pm, day_num)]
        elif phase == "main":
            wrapped = (
                day_num == 1
                and prev_resolved is not None
                and prev_resolved.year == year
                and prev_resolved.month == month
                and prev_resolved.day >= max(28, max_cur - 3)
            )
            if wrapped or day_num > max_cur:
                phase = "suffix"
                candidates = [(ny, nm, day_num)]
            else:
                candidates = [(year, month, day_num)]
        else:
            candidates = [(ny, nm, day_num)]

        resolved: date | None = None
        for y, m, d in candidates:
            resolved = _try_date(y, m, d)
            if resolved is not None:
                break
        if resolved is None:
            for y, m, d in ((ny, nm, day_num), (py, pm, day_num)):
                resolved = _try_date(y, m, d)
                if resolved is not None:
                    break
        if resolved is None:
            continue
        out.append((resolved, state))  # type: ignore[arg-type]
        prev_resolved = resolved
    return out


def parse_datepicker_rows(
    raw: list[dict[str, Any]], *, today: date | None = None
) -> list[tuple[date, DayState]]:
    if today is None:
        today = date.today()
    by_panel: dict[int, list[dict[str, Any]]] = {}
    for row in raw:
        by_panel.setdefault(int(row.get("panelIndex", 0)), []).append(row)

    out: list[tuple[date, DayState]] = []
    for panel_index in sorted(by_panel.keys()):
        rows = by_panel[panel_index]
        if not rows:
            continue
        year = int(rows[0]["year"])
        month = int(rows[0]["month"])
        for d, state in resolve_panel_days(year, month, rows):
            # Прошлые disabled на Авито — не бронь, а неактивные даты.
            if state == "booked" and d < today:
                continue
            out.append((d, state))  # type: ignore[arg-type]
    return out


def _nudge_calendar_panel(page, panel_index: int) -> None:
    try:
        page.evaluate(
            """
            (idx) => {
              const panels = document.querySelectorAll(
                '[data-marker^="datepicker/calendar("]'
              );
              if (panels.length > idx) {
                panels[idx].scrollIntoView({ block: 'nearest', inline: idx ? 'end' : 'start' });
              }
            }
            """,
            panel_index,
        )
        page.wait_for_timeout(300)
    except Exception:
        pass


def _nudge_second_panel(page) -> None:
    _nudge_calendar_panel(page, 1)


def wait_datepicker_ready(
    page,
    timeout_ms: int = 20000,
    min_day_labels: int = 7,
    *,
    min_panels: int = 1,
) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            n = page.evaluate(_DATEPICKER_READY_JS, min_panels)
            if isinstance(n, (int, float)) and n >= min_day_labels:
                return True
        except Exception:
            pass
        page.wait_for_timeout(200)
    return False


def _panel_titles_from_raw(raw: list[dict[str, Any]]) -> list[str]:
    seen: list[str] = []
    for row in sorted(raw, key=lambda r: int(r.get("panelIndex", 0))):
        title = (row.get("panelTitle") or "").strip()
        marker = (row.get("marker") or "").strip()
        label = title if title else marker
        if label and label not in seen:
            seen.append(label)
    return seen[:TOTAL_MONTH_COUNT]


def _panel_months_from_raw(raw: list[dict[str, Any]]) -> set[tuple[int, int]]:
    """Два месяца видимых панелей datepicker (по data-marker calendar(Y-M), 0-based → +1)."""
    by_panel: dict[int, tuple[int, int]] = {}
    for row in raw:
        if not row.get("year") or not row.get("month"):
            continue
        pi = int(row.get("panelIndex", 0))
        if pi not in by_panel:
            by_panel[pi] = (int(row["year"]), int(row["month"]))
    return {by_panel[i] for i in sorted(by_panel) if i < INITIAL_PANEL_COUNT}


def _expected_panel_months(today: date) -> set[tuple[int, int]]:
    """Текущий и следующий месяц — то, что должно быть в двух панелях datepicker."""
    y, m = today.year, today.month
    if m < 12:
        return {(y, m), (y, m + 1)}
    return {(y, m), (y + 1, 1)}


def _wrong_month_window(seen_months: set[tuple[int, int]], today: date) -> bool:
    """Панели сдвинуты (например июль+август вместо июня+июля)."""
    if not seen_months:
        return True
    cur = (today.year, today.month)
    if cur not in seen_months:
        return True
    if min(seen_months) > cur:
        return True
    return False


def _parse_panels_once(
    page,
    today: date,
) -> tuple[list[dict[str, Any]], set[tuple[int, int]], list[str], dict[date, str], dict[date, str]]:
    _nudge_calendar_panel(page, 0)
    _nudge_second_panel(page)
    wait_datepicker_ready(
        page,
        timeout_ms=DATEPICKER_READY_MS,
        min_day_labels=14,
        min_panels=INITIAL_PANEL_COUNT,
    )
    raw = page.evaluate(_PARSE_JS)
    if not isinstance(raw, list):
        raw = []
    seen_months = _panel_months_from_raw(raw)
    panel_titles = _panel_titles_from_raw(raw)
    all_days: dict[date, str] = {}
    future: dict[date, str] = {}
    _apply_raw_to_maps(raw, today, all_days, future)
    return raw, seen_months, panel_titles, all_days, future


def _click_prev_month(page) -> bool:
    for sel in PREV_MONTH_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0 or not loc.is_visible(timeout=2000):
                continue
            loc.click(timeout=8000)
            page.wait_for_timeout(500)
            return True
        except Exception:
            continue
    return False


def _align_calendar_to_today(page, today: date, *, max_clicks: int = 8) -> bool:
    """Листаем назад, пока в панелях не появится текущий месяц."""
    for _ in range(max_clicks):
        raw = page.evaluate(_PARSE_JS)
        if isinstance(raw, list) and (today.year, today.month) in _panel_months_from_raw(raw):
            return True
        if not _click_prev_month(page):
            return False
        page.wait_for_timeout(400)
    raw = page.evaluate(_PARSE_JS)
    return isinstance(raw, list) and (today.year, today.month) in _panel_months_from_raw(raw)


def _panels_parse_incomplete(
    raw: list[dict[str, Any]],
    seen_months: set[tuple[int, int]],
    future: dict[date, str],
    today: date,
) -> bool:
    by_panel = {
        i
        for i in {int(r.get("panelIndex", 0)) for r in raw}
        if i < INITIAL_PANEL_COUNT
    }
    if len(by_panel) < INITIAL_PANEL_COUNT or len(seen_months) < INITIAL_PANEL_COUNT:
        return True
    if not any(d >= today for d in future):
        return True
    if _wrong_month_window(seen_months, today):
        return True
    return False


def _dump_calendar_parse_debug(
    debug_dir: Path,
    item_id: str,
    *,
    today: date,
    raw: list[dict[str, Any]],
    seen_months: set[tuple[int, int]],
    future: dict[date, str],
    panel_titles: list[str],
    incomplete: bool,
) -> Path:
    import json
    from datetime import datetime

    debug_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = debug_dir / f"{item_id}_{stamp}_calendar.json"
    booked = sum(1 for r in raw if r.get("state") == "booked")
    free = sum(1 for r in raw if r.get("state") == "free")
    payload = {
        "item_id": item_id,
        "today": today.isoformat(),
        "incomplete": incomplete,
        "panel_titles": panel_titles,
        "seen_months": [f"{y}-{m:02d}" for y, m in sorted(seen_months)],
        "expected_months": [
            f"{y}-{m:02d}" for y, m in sorted(_expected_panel_months(today))
        ],
        "wrong_month_window": _wrong_month_window(seen_months, today),
        "future": {d.isoformat(): v for d, v in sorted(future.items())},
        "raw_stats": {"rows": len(raw), "booked_cells": booked, "free_cells": free},
        "raw_sample": raw[:40],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _window_end_for_panel_months(months: set[tuple[int, int]]) -> date | None:
    if not months:
        return None
    y, m = max(months)
    return date(y, m, calendar.monthrange(y, m)[1])


def _apply_raw_to_maps(
    raw: list[dict[str, Any]],
    today: date,
    all_days: dict[date, str],
    future: dict[date, str],
) -> None:
    for d, state in parse_datepicker_rows(raw, today=today):
        if state not in ("free", "booked"):
            continue
        val = "0" if state == "free" else "1"
        all_days[d] = val
        if d >= today:
            future[d] = val


def _click_next_month(page) -> bool:
    for sel in NEXT_MONTH_SELECTORS:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0 or not loc.is_visible(timeout=2000):
                continue
            loc.click(timeout=8000)
            page.wait_for_timeout(500)
            return True
        except Exception:
            continue
    return False


def _add_month_from_nav(
    page,
    today: date,
    seen_months: set[tuple[int, int]],
    panel_titles: list[str],
    all_days: dict[date, str],
    future: dict[date, str],
) -> bool:
    """Один клик вперёд — один новый месяц. Возвращает False, если листать некуда."""
    if not _click_next_month(page):
        return False
    _nudge_second_panel(page)
    wait_datepicker_ready(page, timeout_ms=DATEPICKER_READY_AFTER_NAV_MS, min_day_labels=10)
    raw_next = page.evaluate(_PARSE_JS)
    if not isinstance(raw_next, list):
        return False
    new_months = sorted(_panel_months_from_raw(raw_next) - seen_months)
    if not new_months:
        return False
    anchor = max(seen_months) if seen_months else (0, 0)
    newer = [ym for ym in new_months if ym > anchor]
    month_add = newer[0] if newer else new_months[0]
    seen_months.add(month_add)
    raw_new = [
        r for r in raw_next if (int(r["year"]), int(r["month"])) == month_add
    ]
    for title in _panel_titles_from_raw(raw_new):
        if title not in panel_titles:
            panel_titles.append(title)
    _apply_raw_to_maps(raw_new, today, all_days, future)
    return True


def read_availability_panels(
    page,
    today: date,
    *,
    debug_dir: Path | None = None,
    incomplete_debug_dir: Path | None = None,
    debug_id: str = "",
) -> tuple[dict[date, str], dict[date, str], list[str]]:
    """
    Две панели datepicker как на экране (без кнопки «следующий месяц»).
    В таблицу попадают только даты >= today.
    """
    max_attempts = 3
    raw_first: list[dict[str, Any]] = []
    seen_months: set[tuple[int, int]] = set()
    panel_titles: list[str] = []
    all_days: dict[date, str] = {}
    future: dict[date, str] = {}
    incomplete = True

    for attempt in range(max_attempts):
        raw, seen_months, panel_titles, all_days, future = _parse_panels_once(page, today)
        incomplete = _panels_parse_incomplete(raw, seen_months, future, today)
        raw_first = raw

        if incomplete and _wrong_month_window(seen_months, today):
            if _align_calendar_to_today(page, today):
                raw, seen_months, panel_titles, all_days, future = _parse_panels_once(page, today)
                incomplete = _panels_parse_incomplete(raw, seen_months, future, today)
                raw_first = raw

        if not incomplete or attempt + 1 >= max_attempts:
            break
        page.wait_for_timeout(400 * (attempt + 1))

    if incomplete:
        dump_dir = debug_dir or incomplete_debug_dir
        if dump_dir is not None and debug_id:
            path = _dump_calendar_parse_debug(
                dump_dir,
                debug_id,
                today=today,
                raw=raw_first,
                seen_months=seen_months,
                future=future,
                panel_titles=panel_titles,
                incomplete=True,
            )
            print(
                f"  календарь: неполные панели {sorted(seen_months)} "
                f"(ожидались {_expected_panel_months(today)}) → debug {path}"
            )
        return {}, {}, panel_titles[:TOTAL_MONTH_COUNT]

    if debug_dir is not None and debug_id:
        _dump_calendar_parse_debug(
            debug_dir,
            debug_id,
            today=today,
            raw=raw_first,
            seen_months=seen_months,
            future=future,
            panel_titles=panel_titles,
            incomplete=False,
        )

    for _ in range(EXTRA_MONTH_NAV_CLICKS):
        if len(seen_months) >= TOTAL_MONTH_COUNT:
            break
        _add_month_from_nav(page, today, seen_months, panel_titles, all_days, future)

    allowed = seen_months
    window_end = _window_end_for_panel_months(allowed)

    def _clip_months(m: dict[date, str]) -> dict[date, str]:
        if not allowed:
            return m
        clipped = {d: v for d, v in m.items() if (d.year, d.month) in allowed}
        if window_end is not None:
            clipped = {d: v for d, v in clipped.items() if d <= window_end}
        return clipped

    return _clip_months(future), _clip_months(all_days), panel_titles[:TOTAL_MONTH_COUNT]


def availability_day_map(page, today: date) -> dict[date, str]:
    future, _, _ = read_availability_panels(page, today)
    return future
