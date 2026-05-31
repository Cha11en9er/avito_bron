"""Сдаваемость из datepicker Avito: 2 месяца как открылись + 2 клика «вперёд» (всего 4 месяца)."""

from __future__ import annotations

import calendar
import time
from datetime import date
from typing import Any, Literal

DayState = Literal["free", "booked", "skip"]

INITIAL_PANEL_COUNT = 2
TOTAL_MONTH_COUNT = 4
EXTRA_MONTH_NAV_CLICKS = 2
DATEPICKER_READY_MS = 5000
DATEPICKER_READY_AFTER_NAV_MS = 4000

NEXT_MONTH_SELECTORS: tuple[str, ...] = (
    '[data-marker="datepicker/next-button"]',
    "button._4c628a411bf08f14._5f2feca9eafe0236",
    "._4c628a411bf08f14._5f2feca9eafe0236",
)

_DATEPICKER_READY_JS = """
() => {
  const panels = document.querySelectorAll('[data-marker^="datepicker/calendar("]');
  if (panels.length === 0) return 0;
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
    let year = null;
    let month = null;
    let panelTitle = "";
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
    const marker = panel.getAttribute("data-marker") || "";
    if (!month || !year) {
      const mm = marker.match(/calendar\\((\\d+)-(\\d+)\\)/);
      if (mm) {
        year = parseInt(mm[1], 10);
        month = parseInt(mm[2], 10);
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
        out.extend(resolve_panel_days(year, month, rows))
    return out


def _nudge_second_panel(page) -> None:
    try:
        page.evaluate(
            """
            () => {
              const panels = document.querySelectorAll(
                '[data-marker^="datepicker/calendar("]'
              );
              if (panels.length > 1) {
                panels[1].scrollIntoView({ block: 'nearest', inline: 'end' });
              }
            }
            """
        )
        page.wait_for_timeout(800)
    except Exception:
        pass


def wait_datepicker_ready(page, timeout_ms: int = 20000, min_day_labels: int = 7) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            n = page.evaluate(_DATEPICKER_READY_JS)
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


def _months_in_raw(raw: list[dict[str, Any]]) -> set[tuple[int, int]]:
    return {(int(r["year"]), int(r["month"])) for r in raw if r.get("year") and r.get("month")}


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
    new_months = sorted(_months_in_raw(raw_next) - seen_months)
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


def read_availability_panels(page, today: date) -> tuple[dict[date, str], dict[date, str], list[str]]:
    """
    1) Две панели как в попапе (заголовок «Май 2026» и т.д.).
    2) Два клика «следующий месяц» — 3-й и 4-й месяцы (без дублей).
    В таблицу попадают только даты >= today (прошлые ячейки не трогаем).
    """
    _nudge_second_panel(page)
    wait_datepicker_ready(page, timeout_ms=DATEPICKER_READY_MS, min_day_labels=14)
    raw_first = page.evaluate(_PARSE_JS)
    if not isinstance(raw_first, list):
        return {}, {}, []

    seen_months = _months_in_raw(raw_first)
    panel_titles = _panel_titles_from_raw(raw_first)

    all_days: dict[date, str] = {}
    future: dict[date, str] = {}
    _apply_raw_to_maps(raw_first, today, all_days, future)

    for _ in range(EXTRA_MONTH_NAV_CLICKS):
        if len(seen_months) >= TOTAL_MONTH_COUNT:
            break
        _add_month_from_nav(page, today, seen_months, panel_titles, all_days, future)

    allowed = set(seen_months)

    def _clip_months(m: dict[date, str]) -> dict[date, str]:
        if not allowed:
            return m
        return {d: v for d, v in m.items() if (d.year, d.month) in allowed}

    return _clip_months(future), _clip_months(all_days), panel_titles[:TOTAL_MONTH_COUNT]


def availability_day_map(page, today: date) -> dict[date, str]:
    future, _, _ = read_availability_panels(page, today)
    return future
