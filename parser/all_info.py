from __future__ import annotations

import base64
import io
import json
import os
import random
import re
import sys
import time
import traceback
from datetime import date, datetime
from typing import Any
from pathlib import Path
from urllib.parse import urljoin

import ddddocr
from PIL import Image, ImageOps
from playwright.sync_api import sync_playwright
from playwright_stealth.stealth import Stealth

from parser.paths import project_root

try:
    from google_sheets import is_listing_removed
except ImportError:
    def is_listing_removed(title: str, record: dict | None = None) -> bool:  # type: ignore[misc]
        return "не посмотреть" in (title or "").lower()

NAV_TIMEOUT_MS = 90000
# Сумма фаз на карточке (сек.): пауза 0.8–1.5 + скролл 1–1.5 + бронь 7–8 + финал 0.8–1.2 ≈ 10–12 + OCR/таблица.
INITIAL_WAIT_MIN_S = 0.8
INITIAL_WAIT_MAX_S = 1.5
LIGHT_SCROLL_PHASE_MIN_S = 1.0
LIGHT_SCROLL_PHASE_MAX_S = 1.5
BOOKING_PHASE_MIN_S = 7.0
BOOKING_PHASE_MAX_S = 8.0
FINAL_SCROLL_MIN_S = 0.8
FINAL_SCROLL_MAX_S = 1.2
BOOKING_LOADER_CLICKS = 2
BOOKING_CLICK_PAUSE_MS_MIN = 150
BOOKING_CLICK_PAUSE_MS_MAX = 400
# Фазы карточки (run_detail=0): +1 с на этап относительно быстрого режима.
CARD_INITIAL_LOAD_MIN_S = 0.0
CARD_INITIAL_LOAD_MAX_S = 10.0
CARD_INITIAL_CAPTCHA_GRACE_S = 2.5
CARD_WIDGETS_WAIT_MAX_S = 10.0
CARD_CALENDAR_PHASE_MAX_S = 10.0
CARD_PRICE_PHASE_MAX_S = 10.0
CARD_PHASE_RETRY_TIMEOUT_S = 3.5
CARD_SCROLL_MIN_S = 2.0
CARD_SCROLL_MAX_S = 2.5
LISTING_PACE_MAX_S = 35.0
BETWEEN_LISTINGS_MIN_S = 1.5
BETWEEN_LISTINGS_MAX_S = 2.5
WARMUP_LISTINGS = 20
WARMUP_PACE_MULT = 1.0
CAPTCHA_STREAK_LIMIT = 5
CAPTCHA_PROGRESS_BACK_ROWS = 5
DATEPICKER_READY_TIMEOUT_MS = 3500
# После 10 успешно сохранённых объявлений — новый браузер (сессия не раздувается).
BROWSER_RESTART_EVERY = 10
URLS_FILE_NAME = "urls.txt"
NOT_FOUND_VALUE = "нету на сайте"
PHONE_NOT_RECOGNIZED = "не распознан"
CONTACT_PLACEHOLDER = NOT_FOUND_VALUE
# Авито подставляет «Пользователь», если имя не показывают.
SELLER_HIDDEN_LABEL = "(не даёт сайт)"

_OCR = ddddocr.DdddOcr(show_ad=False)

EXPORT_COLUMNS = [
    "номер",
    "название",
    "цена",
    "ссылки на фото",
    "адрес",
    "автор",
    "ссылка создателя",
    "рейтинг",
    "кол-во отзывов",
    "контакт",
    "описание",
    "телефон",
    "комнат",
    "площадь дома",
    "площадь участка",
    "этажей",
    "кровати",
    "год постройки",
    "бытовая техника",
    "что рядом",
    "особенности",
    "залог",
    "расстояние от МКАД",
    "заезд",
    "выезд",
    "кол-во гостей",
    "шуметь можно",
    "можно с детьми",
    "можно с питомцами",
    "можно курить",
    "разрешены вечеринки",
    "характеристики json",
    "цены по датам",
    "ссылка",
]

# Колонка Excel → подстроки для поиска в ключах блока «О доме» (нижний регистр).
DETAIL_FIELD_SPECS: dict[str, tuple[str, ...]] = {
    "комнат": ("количество комнат", "комнат в доме", "число комнат"),
    "площадь дома": ("площадь дома", "общая площадь"),
    "площадь участка": ("площадь участка",),
    "этажей": ("этажей в доме", "этажност", "этажей"),
    "кровати": ("кровати",),
    "год постройки": ("год постройки",),
    "бытовая техника": ("бытовая техника",),
    "что рядом": ("что рядом",),
    "особенности": ("особенности",),
    "залог": ("залог",),
    "расстояние от МКАД": ("расстояние от мкад",),
    "заезд": ("заезд", "время заезда", "дата заезда"),
    "выезд": ("выезд", "время выезда", "дата выезда"),
    "кол-во гостей": ("количество гостей", "кол-во гостей", "гостей:", "гостей "),
    "шуметь можно": ("шуметь", "уровень шума", "шум"),
    "можно с детьми": ("можно с детьми", "с детьми", "дети"),
    "можно с питомцами": ("можно с питомцами", "питомц", "животн"),
    "можно курить": ("курит", "курение"),
    "разрешены вечеринки": ("вечерин", "тусовк", "вечеринки"),
}

# Как в 24.04.26-avito / avito-parser-exactly.py — только разворот окна, без лишних флагов.
CHROME_ARGS = [
    "--start-maximized",
]


def _setup_playwright_env() -> None:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = project_root()
    bundled = base / "ms-playwright"
    if bundled.is_dir():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)


def _console_utf8() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _playwright_proxy() -> dict[str, str] | None:
    """
    Прокси из .env (опционально).
    Либо AVITO_PROXY_SERVER + USER/PASSWORD, либо одной строкой AVITO_PROXY=http://user:pass@host:port
    """
    raw = (os.getenv("AVITO_PROXY") or "").strip()
    server = (os.getenv("AVITO_PROXY_SERVER") or "").strip()
    user = (os.getenv("AVITO_PROXY_USER") or "").strip()
    password = (os.getenv("AVITO_PROXY_PASSWORD") or "").strip()

    if raw and not server:
        from urllib.parse import urlparse

        u = urlparse(raw if "://" in raw else f"http://{raw}")
        if not u.hostname or not u.port:
            print(f"Прокси: неверный AVITO_PROXY={raw!r}")
            return None
        scheme = u.scheme or "http"
        proxy: dict[str, str] = {"server": f"{scheme}://{u.hostname}:{u.port}"}
        if u.username:
            proxy["username"] = u.username
        if u.password:
            proxy["password"] = u.password
        return proxy

    if not server:
        return None
    if "://" not in server:
        server = f"http://{server}"
    proxy = {"server": server}
    if user:
        proxy["username"] = user
        proxy["password"] = password
    return proxy


def _launch_browser(playwright):
    """Как в avito-parser-exactly.py: системный Chrome, при отсутствии — bundled Chromium."""
    kwargs: dict[str, Any] = {"headless": False, "args": CHROME_ARGS}
    proxy = _playwright_proxy()
    if proxy:
        kwargs["proxy"] = proxy
        print(f"Прокси: {proxy['server']}" + (" (с логином)" if proxy.get("username") else ""))
    try:
        return playwright.chromium.launch(channel="chrome", **kwargs)
    except Exception:
        return playwright.chromium.launch(**kwargs)


def _new_page(browser):
    """Контекст + stealth как в avito-parser-exactly.py (viewport 1920×1080, ru, Москва)."""
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
    )
    stealth = Stealth(
        navigator_languages_override=("ru-RU", "ru", "en-US", "en"),
        navigator_platform_override="Win32",
        webgl_vendor_override="Intel Inc.",
        webgl_renderer_override="Intel Iris OpenGL Engine",
    )
    stealth.apply_stealth_sync(context)
    page = context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    page.set_default_timeout(25000)
    return context, page


def _listing_pace_bounds(listing_num: int) -> tuple[float, float]:
    return 0.0, LISTING_PACE_MAX_S


def _ensure_listing_pace(page, started_at: float, listing_num: int) -> float:
    """Не добавляем искусственных пауз — только логируем, если карточка > LISTING_PACE_MAX_S."""
    elapsed = time.monotonic() - started_at
    over = elapsed - LISTING_PACE_MAX_S
    if over > 0.3:
        return 0.0
    return 0.0


class _ListingTimings:
    """Секунды по этапам одного объявления (monotonic)."""

    __slots__ = ("_root", "_since", "stages")

    def __init__(self, root: float) -> None:
        self._root = root
        self._since = root
        self.stages: dict[str, float] = {}

    def mark(self, name: str) -> None:
        now = time.monotonic()
        self.stages[name] = self.stages.get(name, 0.0) + (now - self._since)
        self._since = now

    def add(self, name: str, seconds: float) -> None:
        if seconds > 0.05:
            self.stages[name] = self.stages.get(name, 0.0) + seconds

    def total(self) -> float:
        return time.monotonic() - self._root

    def line(self) -> str:
        order = ("загрузка", "виджеты", "календарь", "цены", "скролл", "детали", "debug", "запись", "прочее")
        staged = sum(self.stages.values())
        total = self.total()
        gap = total - staged
        if gap > 0.25:
            self.stages["прочее"] = self.stages.get("прочее", 0.0) + gap
        parts: list[str] = []
        for key in order:
            if key in self.stages:
                parts.append(f"{key} {self.stages[key]:.1f}с")
        for key, val in self.stages.items():
            if key not in order:
                parts.append(f"{key} {val:.1f}с")
        parts.append(f"итого {total:.1f}с")
        return "время: " + ", ".join(parts)


def _listing_log_timings(timings: _ListingTimings) -> None:
    msg = timings.line()
    if timings.total() > LISTING_PACE_MAX_S + 0.5:
        msg += f" (лимит {LISTING_PACE_MAX_S:.0f}с)"
    _listing_log_status(msg)


def _pause_between_listings() -> None:
    lo, hi = BETWEEN_LISTINGS_MIN_S, BETWEEN_LISTINGS_MAX_S
    if hi < lo:
        hi = lo
    time.sleep(random.uniform(lo, hi))


def _human_pause(page, min_seconds: float = 0.4, max_seconds: float = 1.6) -> None:
    wait_ms = int(random.uniform(min_seconds, max_seconds) * 1000)
    page.wait_for_timeout(wait_ms)


def _simulate_human_activity(page) -> None:
    # Лёгкие случайные движения мышью — убираем "идеально ровный" машинный паттерн.
    try:
        for _ in range(random.randint(1, 2)):
            x = random.randint(100, 1200)
            y = random.randint(120, 760)
            page.mouse.move(x, y, steps=random.randint(6, 14))
            _human_pause(page, 0.05, 0.2)
    except Exception:
        pass


def _listing_spa_visible(page) -> bool:
    selectors = (
        '[data-marker="item-view/item-price"], '
        'h1[itemprop="name"], '
        '[data-marker="item-view/title-info"] h1'
    )
    try:
        loc = page.locator(selectors).first
        return loc.count() > 0 and loc.is_visible(timeout=400)
    except Exception:
        return False


def _card_scroll_phase(page) -> None:
    _scroll_for_duration(page, CARD_SCROLL_MIN_S, CARD_SCROLL_MAX_S)


def _initial_load_bounds() -> tuple[float, float]:
    return CARD_INITIAL_LOAD_MIN_S, CARD_INITIAL_LOAD_MAX_S


def _wait_initial_page_content(page) -> str:
    """До max с ждём карточку; если SPA уже видна — сразу ready. Капчу не раньше grace."""
    _, max_s = _initial_load_bounds()
    started = time.monotonic()
    while time.monotonic() - started < max_s:
        if _listing_spa_visible(page):
            return "ready"
        if _page_looks_removed(page):
            return "removed"
        if time.monotonic() - started >= CARD_INITIAL_CAPTCHA_GRACE_S and _page_looks_blocked(page):
            return "blocked"
        page.wait_for_timeout(400)

    if _listing_spa_visible(page):
        return "ready"
    if _page_looks_removed(page):
        return "removed"
    if _page_looks_blocked(page):
        return "blocked"
    return "timeout"


def _page_looks_blocked(page) -> bool:
    """Капча / firewall — только если карточка объявления ещё не видна."""
    if _listing_spa_visible(page):
        return False
    try:
        url = (page.url or "").lower()
        if any(x in url for x in ("captcha", "firewall", "accessdenied", "blocked")):
            return True
    except Exception:
        pass
    try:
        if page.locator(
            'iframe[src*="captcha"], iframe[src*="hcaptcha"], '
            '[class*="Captcha"], [data-marker*="captcha"]'
        ).count() > 0:
            return True
    except Exception:
        pass
    try:
        low = (page.locator("body").inner_text(timeout=1200) or "").lower()
        if any(
            x in low
            for x in (
                "подтвердите, что вы не робот",
                "подозрительная активность",
                "доступ ограничен",
                "слишком много запросов",
                "captcha",
                "hcaptcha",
            )
        ):
            return True
    except Exception:
        pass
    return False


def _page_looks_removed(page) -> bool:
    """Снятое/закрытое объявление — не путать с капчей (у капчи нет «не посмотреть»)."""
    if _page_looks_blocked(page):
        return False
    try:
        for sel in (
            'h1[itemprop="name"]',
            '[data-marker="item-view/title-info"] h1',
            "h1",
        ):
            loc = page.locator(sel).first
            if loc.count() > 0:
                t = loc.inner_text(timeout=800) or ""
                if is_listing_removed(t):
                    return True
    except Exception:
        pass
    try:
        low = (page.locator("body").inner_text(timeout=1500) or "").lower()
        if any(
            x in low
            for x in (
                "не посмотреть",
                "снято с продажи",
                "объявление закрыто",
                "объявление недоступно",
                "объявление истекло",
                "пользователь его удалил",
            )
        ):
            return True
        if "попробуйте обновить страницу" in low and not _listing_spa_visible(page):
            return True
    except Exception:
        pass
    return False


def _url_is_listing_card(url: str) -> bool:
    cleaned = url.split("?", 1)[0].rstrip("/")
    return bool(re.search(r"_\d{6,}$", cleaned))


def _listing_not_on_site(page, url: str, title: str | None = None) -> bool:
    """Страница не карточка объявления или объявление снято/удалено."""
    if not _url_is_listing_card(url):
        return True
    t = title if title is not None else _extract_title(page)
    if is_listing_removed(t):
        return True
    return _page_looks_removed(page)


def _finish_removed_listing(
    page,
    base_dir: Path,
    url: str,
    item_id: int | str,
    sheet_row: int,
    last_row: int,
    *,
    sh: Any,
    settings: Any,
    queue_next_row: int | None,
    day_at_start: date,
    day_session: Any,
    sheet_worker: Any,
    timings: _ListingTimings,
) -> dict:
    write_cutoff, _, _ = _sheet_write_cutoff(day_at_start, {}, {}, url, day_session)
    record = {"ссылка": url, "название": NOT_FOUND_VALUE}
    for col in EXPORT_COLUMNS:
        if col not in ("ссылка",):
            record[col] = NOT_FOUND_VALUE
    ok = _maybe_append_google_sheet(
        record, base_dir, item_id, {}, url,
        sh=sh, settings=settings, removed=True, queue_next_row=queue_next_row,
        log_sheet_row=sheet_row, sheet_today=write_cutoff, sheet_worker=sheet_worker,
    )
    _listing_log_status("нет на сайте")
    _listing_log_write(sheet_worker, ok)
    _listing_log_timings(timings)
    return record


def _listing_log_start(sheet_row: int, last_row: int) -> None:
    ts = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    print(f"{ts}  {sheet_row}/{last_row}")


def _listing_log_status(status: str) -> None:
    print(f"  {status}")


def _captcha_hit(url: str) -> dict:
    return {"_captcha": True, "ссылка": url}


def _is_captcha_record(record: dict | None) -> bool:
    return isinstance(record, dict) and bool(record.get("_captcha"))


def _scroll_through_page(page, total_scrolls: int = 6) -> None:
    """Прокручиваем страницу постепенно: вниз пачкой, иногда чуть обратно, в конце — наверх."""
    try:
        size = page.viewport_size or {"height": 900}
        viewport_h = size.get("height") or 900
        for _ in range(total_scrolls):
            step = int(viewport_h * random.uniform(0.45, 0.9))
            page.mouse.wheel(0, step)
            _human_pause(page, 0.5, 1.3)
            if random.random() < 0.3:
                page.mouse.wheel(0, -int(step * random.uniform(0.2, 0.45)))
                _human_pause(page, 0.3, 0.8)
        page.evaluate("window.scrollTo({ top: 0, behavior: 'smooth' })")
        _human_pause(page, 0.8, 1.6)
    except Exception:
        pass


def _scroll_for_duration(page, min_s: float, max_s: float) -> None:
    """Плавная прокрутка в течение случайного интервала (сек.)."""
    try:
        until = time.monotonic() + random.uniform(min_s, max_s)
        while time.monotonic() < until:
            remaining = until - time.monotonic()
            if remaining <= 0.05:
                break
            page.mouse.wheel(0, random.randint(90, 260))
            page.wait_for_timeout(int(min(random.uniform(0.12, 0.38), remaining) * 1000))
    except Exception:
        pass


def _micro_scroll_nudge(page) -> None:
    try:
        page.mouse.wheel(0, random.randint(45, 160))
        page.wait_for_timeout(random.randint(70, 200))
    except Exception:
        pass


def _slot_price_display(text: str) -> str:
    t = text.replace("\u2060", "")
    m = re.search(r"([\d\s\u00a0]+)\s*₽", t)
    if not m:
        return ""
    digits = re.sub(r"\D", "", m.group(1))
    if not digits:
        return ""
    n = int(digits)
    return f"{n:,}".replace(",", " ") + " ₽"


def _slot_period_label(text: str, data_id: str) -> str:
    """Подпись периода вроде «14-15 май» из текста ячейки или из data-id."""
    t = text.replace("\u2060", "").replace("\xa0", " ")
    m = re.search(r"(\d{1,2})\s*[—\-]\s*(\d{1,2})\s+([а-яёa-z]+)", t, re.I)
    if m:
        return f"{m.group(1)}-{m.group(2)} {m.group(3)}"
    parts = data_id.split("--")
    if len(parts) == 2:
        try:
            d0 = datetime.strptime(parts[0][:10], "%Y-%m-%d")
            d1 = datetime.strptime(parts[1][:10], "%Y-%m-%d")
            months = (
                "янв",
                "фев",
                "мар",
                "апр",
                "май",
                "июн",
                "июл",
                "авг",
                "сен",
                "окт",
                "ноя",
                "дек",
            )
            if d0.month == d1.month:
                return f"{d0.day}-{d1.day} {months[d0.month - 1]}"
            return f"{d0.day}.{d0.month}-{d1.day}.{d1.month}"
        except Exception:
            pass
    return data_id


def _slot_is_booked(text: str, price: str) -> bool:
    """С ₽ — свободно; без цены (или «занято») — есть бронь."""
    if price:
        return False
    t = (text or "").lower()
    return "занят" in t or "недоступ" in t or True


def has_nearest_dates_block(page) -> bool:
    """Есть ли на карточке блок «ближайшие даты» (без ожидания)."""
    try:
        return page.locator('[data-marker="nearest-dates"]').count() > 0
    except Exception:
        return False


def _booking_widgets_present(page) -> tuple[bool, bool]:
    from parser.calendar_page import has_calendar_on_page

    return has_calendar_on_page(page), has_nearest_dates_block(page)


def _wait_booking_widgets(page) -> tuple[bool, bool]:
    """
    После загрузки SPA — до CARD_WIDGETS_WAIT_MAX_S ждём календарь и/или «ближайшие даты».
    Листаем страницу: виджеты часто подгружаются после скролла.
    """
    has_cal, has_prices = _booking_widgets_present(page)
    if has_cal or has_prices:
        return has_cal, has_prices

    deadline = time.monotonic() + CARD_WIDGETS_WAIT_MAX_S
    step = 0
    while time.monotonic() < deadline:
        try:
            dy = 450 if step % 2 == 0 else -200
            page.evaluate(
                f"window.scrollBy(0, {dy})"
            )
        except Exception:
            pass
        page.wait_for_timeout(450)
        has_cal, has_prices = _booking_widgets_present(page)
        if has_cal or has_prices:
            return has_cal, has_prices
        step += 1

    return _booking_widgets_present(page)


def _read_booking_slots(page) -> dict[str, tuple[str, str]]:
    """data_id -> (подпись периода, цена ₽ или маркер занятости)."""
    rows = page.evaluate(
        """() => {
            const root = document.querySelector('[data-marker="nearest-dates"]');
            if (!root) return [];
            const out = [];
            for (const li of root.querySelectorAll('li[data-id]')) {
                out.push({
                    dataId: li.getAttribute('data-id') || '',
                    text: (li.innerText || '').replace(/\\s+/g, ' ').trim()
                });
            }
            return out;
        }"""
    )
    out: dict[str, tuple[str, str]] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        did = str(row.get("dataId") or "").strip()
        text = str(row.get("text") or "")
        if not did:
            continue
        label = _slot_period_label(text, did)
        price = _slot_price_display(text)
        if _slot_is_booked(text, price):
            from google_sheets.constants import BOOKED_SLOT_MARKER

            out[did] = (label, BOOKED_SLOT_MARKER)
        else:
            out[did] = (label, price)
    return out


def _merge_booking_dicts(
    acc: dict[str, tuple[str, str]], new: dict[str, tuple[str, str]]
) -> dict[str, tuple[str, str]]:
    merged = dict(acc)
    merged.update(new)
    return merged


def _click_nearest_dates_loader(page) -> bool:
    """
    Листаем карусель «ближайшие даты».
    Сначала плейсхолдер (как в рабочей версии), затем кнопка вперёд без is_enabled
    (у Авито часто tabindex=-1 при рабочей кнопке).
    """
    loader = page.locator(
        '[data-marker="nearest-dates"] div._4fc2ff53aabfae78.edd2186806a9484c._0ad1ed89cd18697d'
    ).first
    if loader.count() > 0:
        try:
            if loader.is_visible(timeout=1500):
                loader.click(timeout=10000)
                return True
        except Exception:
            pass

    fwd = page.locator('[data-marker="nearest-dates/scroll-button-forward"]').first
    if fwd.count() > 0:
        try:
            if fwd.is_visible(timeout=1500):
                fwd.click(timeout=10000, force=True)
                return True
        except Exception:
            pass

    try:
        return bool(
            page.evaluate(
                """() => {
                const root = document.querySelector('[data-marker="nearest-dates"]');
                if (!root) return false;
                const ph = root.querySelector(
                    'div._4fc2ff53aabfae78.edd2186806a9484c._0ad1ed89cd18697d'
                );
                if (ph) { ph.click(); return true; }
                const btn = root.querySelector(
                    '[data-marker="nearest-dates/scroll-button-forward"]'
                );
                if (btn) { btn.click(); return true; }
                const ul = root.querySelector('ul');
                if (ul && ul.scrollWidth > ul.clientWidth + 2) {
                    ul.scrollLeft = Math.min(
                        ul.scrollLeft + Math.max(ul.clientWidth, 120),
                        ul.scrollWidth
                    );
                    return true;
                }
                return false;
            }"""
            )
        )
    except Exception:
        return False


def _collect_booking_prices_with_loaders(page, phase_deadline: float) -> dict[str, tuple[str, str]]:
    """
    Карусель «ближайшие даты»: читаем цены, до BOOKING_LOADER_CLICKS раз жмём плейсхолдер.
    Между действиями — короткие паузы; фазовый бюджет — phase_deadline (как в 1c28f1b).
    """
    acc = _read_booking_slots(page)
    for _ in range(BOOKING_LOADER_CLICKS):
        if time.monotonic() >= phase_deadline - 0.15:
            break
        if _page_looks_blocked(page):
            break
        for _ in range(random.randint(1, 2)):
            if time.monotonic() >= phase_deadline - 0.15:
                break
            _micro_scroll_nudge(page)
        if not _click_nearest_dates_loader(page):
            break
        remaining_ms = int((phase_deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        pause_ms = random.randint(BOOKING_CLICK_PAUSE_MS_MIN, BOOKING_CLICK_PAUSE_MS_MAX)
        pause_ms = min(pause_ms, max(80, remaining_ms - 40))
        page.wait_for_timeout(pause_ms)
        acc = _merge_booking_dicts(acc, _read_booking_slots(page))
    while time.monotonic() < phase_deadline:
        _micro_scroll_nudge(page)
        left_ms = int((phase_deadline - time.monotonic()) * 1000)
        if left_ms <= 0:
            break
        page.wait_for_timeout(min(80, left_ms))
    return acc


def _booking_prices_to_dict(by_id: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Период → цена или BOOKED_SLOT_MARKER. Ключ ISO data-id."""
    items = sorted(by_id.items(), key=lambda x: x[0])
    out: dict[str, str] = {}
    for did, (label, price) in items:
        key = did if "--" in did and did[:4].isdigit() else (label if label else did)
        if key in out and out[key] != price:
            key = f"{key} ({did})"
        out[key] = price
    return out


def _safe_slug(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() else "_" for ch in value.strip().lower())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return cleaned[:80] or "house"


def _normalize_space(value: str) -> str:
    return " ".join((value or "").split()).strip()


def _normalize_multiline(value: str) -> str:
    """Сохраняет переносы строк (для адреса «Расположение»)."""
    lines = [" ".join(line.split()) for line in (value or "").splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _load_urls(base_dir: Path) -> tuple[Any, Any, list[str], int, int]:
    try:
        from google_sheets import bootstrap_google_sheet_mode, is_google_sheet_enabled, prepare_parse_session
    except ImportError:
        bootstrap_google_sheet_mode = None  # type: ignore[assignment,misc]
        is_google_sheet_enabled = lambda: False  # noqa: E731
        prepare_parse_session = None  # type: ignore[assignment]

    if bootstrap_google_sheet_mode is not None:
        mode = bootstrap_google_sheet_mode(base_dir)
        if mode == "sheet":
            print("Режим: Google Таблица (настройки — лист «настройки»).")
        else:
            print(
                f"Режим: файл {URLS_FILE_NAME}. "
                "Для таблицы укажите AVITO_GOOGLE_SHEET=1 или положите service_account.json."
            )

    if is_google_sheet_enabled():
        if prepare_parse_session is None:
            raise RuntimeError("AVITO_GOOGLE_SHEET=1, но пакет google_sheets недоступен.")
        sh, settings, queue, start_offset, full_len = prepare_parse_session(base_dir, EXPORT_COLUMNS)
        return sh, settings, queue, start_offset, full_len

    urls_path = base_dir / URLS_FILE_NAME
    if not urls_path.exists():
        raise FileNotFoundError(
            f"Не найден файл со ссылками: {urls_path}. Создайте {URLS_FILE_NAME} рядом со скриптом."
        )
    urls: list[str] = []
    seen: set[str] = set()
    with open(urls_path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line in seen:
                continue
            seen.add(line)
            urls.append(line)
    if not urls:
        raise ValueError(f"{URLS_FILE_NAME} не содержит ни одной валидной ссылки")
    print(
        f"ВНИМАНИЕ: берутся только {len(urls)} ссылок из {URLS_FILE_NAME}, "
        "а не из Google Таблицы. Данные в таблице не обновятся."
    )
    return None, None, urls, 0, len(urls)


def _extract_id_from_url(url: str) -> int | None:
    cleaned = url.split("?", 1)[0].rstrip("/")
    match = re.search(r"_(\d{6,})$", cleaned)
    if match:
        return int(match.group(1))
    return None


def _extract_title(page) -> str:
    for selector in (
        'h1[itemprop="name"]',
        '[data-marker="item-view/title-info"] h1',
        "h1",
    ):
        loc = page.locator(selector).first
        if loc.count() > 0:
            text = _normalize_space(loc.inner_text())
            if text:
                return text
    return ""


def _extract_price(page) -> str:
    candidates = (
        '[data-marker="item-view/item-price-container"]',
        '[data-marker="item-view/item-price"]',
        'span[data-marker="item-view/item-price-content"]',
        '[itemprop="price"]',
    )
    for selector in candidates:
        loc = page.locator(selector).first
        if loc.count() == 0:
            continue
        try:
            content_attr = loc.get_attribute("content")
            if content_attr and re.search(r"\d", content_attr):
                return _normalize_space(content_attr)
        except Exception:
            pass
        text = _normalize_space(loc.inner_text())
        if text:
            return text
    return ""


def _extract_address(page) -> str:
    for selector in (
        '[itemprop="address"]',
        '[data-marker="item-view/item-address"]',
    ):
        loc = page.locator(selector).first
        if loc.count() > 0:
            text = _normalize_multiline(loc.inner_text())
            if text:
                return text
    loc = page.locator('[itemprop="address"] span._8360df6eedcf8d52').first
    if loc.count() > 0:
        text = _normalize_space(loc.inner_text())
        if text:
            return text
    return ""


def _extract_seller_name_and_profile(page) -> tuple[str, str]:
    """
    Имя и ссылка на профиль. Если в разметке только «Пользователь» — подставляем SELLER_HIDDEN_LABEL.
    Имя с якоря /user/.../ приоритетнее, чем data-marker seller-info/name.
    """
    data = page.evaluate(
        """() => {
            const root = document.querySelector('[data-marker="item-view/seller-info"]');
            let profileHref = null;
            let nameFromProfileLink = '';
            if (root) {
                const a = root.querySelector('a[href*="/user/"]');
                if (a) {
                    profileHref = a.getAttribute('href');
                    nameFromProfileLink = (a.innerText || '').replace(/\\s+/g, ' ').trim();
                }
            }
            if (!profileHref) {
                const scope = document.querySelector('[data-marker="item-view"]') || document.body;
                const g = scope.querySelector('a[href*="/user/"][href*="/profile"]');
                if (g) profileHref = g.getAttribute('href');
            }
            const marker = document.querySelector('[data-marker="seller-info/name"]');
            const markerName = marker ? marker.innerText.replace(/\\s+/g, ' ').trim() : '';
            return { profileHref, nameFromProfileLink, markerName };
        }"""
    )
    if not isinstance(data, dict):
        data = {}
    href = (data.get("profileHref") or "").strip()
    profile_url = urljoin("https://www.avito.ru", href) if href else ""
    link_name = _normalize_space(data.get("nameFromProfileLink") or "")
    marker_name = _normalize_space(data.get("markerName") or "")

    name = ""
    if link_name and link_name != "Пользователь":
        name = link_name
    elif marker_name and marker_name != "Пользователь":
        name = marker_name
    elif link_name == "Пользователь" or marker_name == "Пользователь":
        name = SELLER_HIDDEN_LABEL
    else:
        name = link_name or marker_name

    if not name:
        name = NOT_FOUND_VALUE
    return name, profile_url


def _extract_seller_rating_and_reviews(page) -> tuple[str, str]:
    """
    Рейтинг (например «5,0») и число отзывов.
    Блок часто в шапке: [data-marker="item-navigation/rating-badge"] или
    [data-marker="item-view/rating-badge-link"] (без ссылки /user/), реже — в seller-info.
    """
    raw = page.evaluate(
        """() => {
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            const isReviewLabel = (t) => /отзыв/i.test(t);
            const looksLikeScore = (t) => {
                if (!t || isReviewLabel(t)) return false;
                const u = t.replace(',', '.').replace(/\\s/g, '');
                return /^\\d+(\\.\\d+)?$/.test(u);
            };
            const parsePairFromRoot = (root) => {
                if (!root) return null;
                const spans = [...root.querySelectorAll('span.b77ace83bc35f090')];
                if (!spans.length) return null;
                for (let i = 0; i < spans.length; i++) {
                    const t = norm(spans[i].innerText);
                    if (!t) continue;
                    if (looksLikeScore(t)) {
                        for (let j = i + 1; j < Math.min(i + 5, spans.length); j++) {
                            const tj = norm(spans[j].innerText);
                            if (isReviewLabel(tj)) return { rating: t, reviews: tj };
                        }
                    }
                }
                if (spans.length >= 2) {
                    const t0 = norm(spans[0].innerText);
                    const t1 = norm(spans[1].innerText);
                    if (looksLikeScore(t0) && isReviewLabel(t1)) return { rating: t0, reviews: t1 };
                }
                if (spans.length >= 1) {
                    const t0 = norm(spans[0].innerText);
                    if (isReviewLabel(t0) && !looksLikeScore(t0)) return { rating: '0', reviews: t0 };
                }
                return null;
            };

            const roots = [];
            const seen = new Set();
            const add = (el) => {
                if (el && !seen.has(el)) {
                    seen.add(el);
                    roots.push(el);
                }
            };
            add(document.querySelector('[data-marker="item-navigation/rating-badge"]'));
            add(document.querySelector('[data-marker="item-view/rating-badge-link"]'));
            for (const el of document.querySelectorAll('[data-marker*="rating-badge"]')) {
                add(el);
            }
            add(document.querySelector('[data-marker="item-view/seller-info"]'));
            const itemView = document.querySelector('[data-marker="item-view"]');
            if (itemView) {
                const anchors = itemView.querySelectorAll('a[href*="/user/"]');
                for (let k = 0; k < anchors.length && k < 10; k++) {
                    add(anchors[k]);
                }
                add(itemView);
            }
            for (const r of roots) {
                const hit = parsePairFromRoot(r);
                if (hit && (hit.rating || hit.reviews)) return hit;
            }
            return { rating: '', reviews: '' };
        }"""
    )
    if not isinstance(raw, dict):
        return "", ""
    rating_raw = _normalize_space(str(raw.get("rating") or ""))
    reviews_raw = _normalize_space(str(raw.get("reviews") or ""))

    rating_out = ""
    if rating_raw:
        if rating_raw in ("—", "-", "–", "−", "\u2014", "\u2013", "нет", "Нет"):
            rating_out = "0"
        else:
            r = rating_raw.replace(",", ".").strip()
            if re.match(r"^\d+(\.\d+)?$", r):
                try:
                    v = float(r)
                    if v == 0.0:
                        rating_out = "0"
                    else:
                        rating_out = f"{v:.1f}".replace(".", ",")
                except ValueError:
                    rating_out = rating_raw
            else:
                rating_out = rating_raw

    reviews_out = ""
    if reviews_raw:
        m = re.search(r"(\d+)", reviews_raw)
        if m:
            reviews_out = m.group(1)

    return rating_out, reviews_out


def _extract_gallery_image_urls(page) -> list[str]:
    """Реальные https URL из галереи (blob: в дампе не переносимы — отбрасываем)."""
    raw: list[str] = page.evaluate(
        """() => {
            const root = document.querySelector('[data-marker="item-view/main-gallery"]');
            if (!root) return [];
            const out = [];
            const seen = new Set();
            for (const img of root.querySelectorAll('img[src]')) {
                const s = img.getAttribute('src');
                if (!s || s.startsWith('data:') || s.startsWith('blob:')) continue;
                if (!s.startsWith('http')) continue;
                if (seen.has(s)) continue;
                seen.add(s);
                out.push(s);
            }
            return out;
        }"""
    )
    if not isinstance(raw, list):
        return []
    return [str(u).strip() for u in raw if str(u).strip().startswith("http")]


def _extract_description(page) -> str:
    loc = page.locator('div[data-marker="item-view/item-description"]').first
    if loc.count() == 0:
        return ""
    return _normalize_space(loc.inner_text())


def _extract_details_map(page) -> dict[str, str]:
    """Пары «параметр: значение» из блока «О доме» / характеристик."""
    details: dict[str, str] = {}

    rows = page.locator('[data-marker="item-view/item-params"] li')
    if rows.count() == 0:
        rows = page.locator("li.d2936d013c910379")
    if rows.count() == 0:
        rows = page.locator(
            '[data-marker="item-view/params"] li, '
            'ul[class*="params"] li, '
            'div[class*="params"] li'
        )

    count = rows.count()
    for i in range(count):
        row = rows.nth(i)
        label_locator = row.locator(
            'span.d6e8fd2e3d52b32a, span[class*="styles-module-label"], span[class*="param-label"]'
        ).first
        full_text = _normalize_space(row.inner_text())
        label = ""
        value = ""
        if label_locator.count() > 0:
            label_text = _normalize_space(label_locator.inner_text())
            label = label_text.rstrip(":").lower()
            value = full_text.replace(label_text, "", 1).strip(" :")
        elif ":" in full_text:
            head, tail = full_text.split(":", 1)
            label = _normalize_space(head).lower()
            value = _normalize_space(tail)
        if label:
            details[label] = _normalize_space(value)
    return details


def _pick_detail_column(details: dict[str, str], column: str) -> str:
    patterns = DETAIL_FIELD_SPECS.get(column)
    if not patterns:
        return ""
    for label, value in details.items():
        lab = label.lower()
        for kw in patterns:
            if kw in lab:
                return value
    return ""


def _save_phone_image_from_popup(
    page,
    save_dir: Path,
    item_id: int,
    title: str,
    timestamp: str,
) -> str | None:
    button = page.locator('button[data-marker="item-phone-button/card"]').first
    if button.count() == 0:
        return None
    button.click(timeout=10000)

    popup = page.locator("div.ea88242d5926e753.b0253be71a159c76").first
    popup.wait_for(state="visible", timeout=10000)

    image = popup.locator("img._71e723cdca9c6624").first
    image.wait_for(state="visible", timeout=10000)
    src = image.get_attribute("src")
    if not src:
        return None

    content: bytes
    extension = "png"

    if src.startswith("data:image/"):
        header, _, encoded = src.partition(",")
        match = re.match(r"^data:image/([a-zA-Z0-9.+-]+);base64$", header)
        if not match:
            return None
        extension = match.group(1).replace("+xml", "")
        content = base64.b64decode(encoded)
    else:
        if src.startswith("//"):
            src = f"https:{src}"
        response = page.request.get(src, timeout=15000)
        if not response.ok:
            return None
        content_type = (response.header_value("content-type") or "").lower()
        match = re.search(r"image/([a-z0-9.+-]+)", content_type)
        if match:
            extension = match.group(1).replace("+xml", "")
        content = response.body()

    file_name = f"{item_id}_{_safe_slug(title)}_{timestamp}_phone.{extension}"
    out_path = save_dir / file_name
    with open(out_path, "wb") as f:
        f.write(content)
    return str(out_path)


def _extract_russian_phone(text: str) -> str | None:
    prepared = (
        (text or "")
        .replace("O", "0")
        .replace("o", "0")
        .replace("I", "1")
        .replace("l", "1")
        .replace("S", "5")
        .replace("B", "8")
    )
    cleaned = re.sub(r"[^\d+]", "", prepared)
    candidates: list[str] = []
    if cleaned:
        candidates.append(cleaned)
    candidates.extend(re.findall(r"(?:\+?7|8)\D*\d\D*\d\D*\d\D*\d\D*\d\D*\d\D*\d\D*\d\D*\d\D*\d", text))

    for candidate in candidates:
        digits = re.sub(r"\D", "", candidate)
        if len(digits) == 11 and (digits.startswith("8") or digits.startswith("7")):
            if digits.startswith("7"):
                digits = "8" + digits[1:]
            return digits
    return None


def _ocr_phone_number(image_path: str) -> str | None:
    with Image.open(image_path) as source_image:
        rgba = source_image.convert("RGBA")
        white_bg = Image.new("RGBA", rgba.size, "WHITE")
        white_bg.alpha_composite(rgba)
        image = ImageOps.grayscale(white_bg)
        # Увеличиваем и бинаризуем: для крупных чёрных цифр на белом фоне сильно повышает OCR-точность.
        image = image.resize((image.width * 3, image.height * 3))
        image = image.point(lambda p: 255 if p > 180 else 0)

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    ocr_text = _OCR.classification(buf.getvalue())
    return _extract_russian_phone(ocr_text)


def _or_not_found(value: str | None) -> str:
    normalized = _normalize_space(value or "")
    return normalized if normalized else NOT_FOUND_VALUE


def _collect_nearest_date_prices(page) -> dict[str, str]:
    """Цены из карусели «ближайшие даты»: до CARD_PRICE_PHASE_MAX_S, шаг — CARD_PHASE_RETRY_TIMEOUT_S."""
    phase_end = time.monotonic() + CARD_PRICE_PHASE_MAX_S
    booking_map: dict[str, tuple[str, str]] = {}

    while time.monotonic() < phase_end:
        remaining_s = phase_end - time.monotonic()
        if remaining_s <= 0.15:
            break
        step_end = min(time.monotonic() + CARD_PHASE_RETRY_TIMEOUT_S, phase_end)
        try:
            page.evaluate(
                "window.scrollBy(0, Math.min(900, document.body.scrollHeight * 0.35))"
            )
            nd = page.locator('[data-marker="nearest-dates"]').first
            if nd.count() > 0:
                left_ms = int((step_end - time.monotonic()) * 1000)
                if left_ms > 0:
                    nd.scroll_into_view_if_needed(timeout=min(3500, left_ms))
        except Exception:
            pass

        chunk = _collect_booking_prices_with_loaders(page, step_end)
        if chunk:
            booking_map = _merge_booking_dicts(booking_map, chunk)
            return _booking_prices_to_dict(booking_map)

        if time.monotonic() >= phase_end - 0.15:
            break
        page.wait_for_timeout(200)

    return _booking_prices_to_dict(booking_map)


def _parse_listing_calendar(
    page,
    url: str,
    sheet_row: int,
    item_id: str | int,
    today: date,
    *,
    base_dir: Path | None = None,
    settings: Any = None,
) -> dict[date, str]:
    """
    Сдаваемость из datepicker (2 месяца, без листания).
    Возвращает только даты >= today — для записи в таблицу (прошлое не перезаписываем).
    """
    from parser.calendar_availability import read_availability_panels
    from parser.calendar_page import (
        close_calendar_popup,
        has_calendar_on_page,
        open_calendar_popup,
        scroll_to_calendar,
    )

    calendar_in_url = "calendar=true" in url.lower()
    availability_days: dict[date, str] = {}
    calendar_trigger: str | None = None
    sid = str(item_id)
    phase_end = time.monotonic() + CARD_CALENDAR_PHASE_MAX_S

    scroll_to_calendar(page)

    def _run_calendar_parse(ready_timeout_ms: int) -> None:
        nonlocal availability_days, calendar_trigger
        calendar_trigger = open_calendar_popup(
            page,
            sheet_row,
            sid,
            after_open_wait_s=0.25,
            ready_timeout_ms=ready_timeout_ms,
            quiet=True,
        )
        if calendar_in_url and not calendar_trigger:
            calendar_trigger = "calendar=true"
        if not calendar_trigger:
            return
        try:
            debug_dir: Path | None = None
            incomplete_debug_dir: Path | None = None
            if base_dir is not None:
                rel = (
                    (getattr(settings, "debug_html_dir", None) or "debug_html").strip()
                    if settings is not None
                    else "debug_html"
                )
                incomplete_debug_dir = base_dir / rel
                if settings is not None and getattr(settings, "debug_dump_html", False):
                    debug_dir = incomplete_debug_dir
            availability_days, _, _ = read_availability_panels(
                page,
                today,
                debug_dir=debug_dir,
                incomplete_debug_dir=incomplete_debug_dir,
                debug_id=sid,
            )
        except Exception:
            traceback.print_exc()

    while time.monotonic() < phase_end:
        remaining_s = phase_end - time.monotonic()
        if remaining_s <= 0.15:
            break
        timeout_ms = int(min(CARD_PHASE_RETRY_TIMEOUT_S, remaining_s) * 1000)
        _run_calendar_parse(timeout_ms)
        if availability_days:
            break
        if calendar_trigger:
            break
        page.wait_for_timeout(200)

    if not calendar_in_url and calendar_trigger:
        close_calendar_popup(page, calendar_trigger, quiet=True)

    n = len(availability_days)
    if n:
        print(f"  сдаваемость: {n} дн.")
    elif calendar_trigger:
        print("  сдаваемость: пусто (календарь открыт, дней нет)")
    else:
        print("  сдаваемость: пусто (календарь не открылся)")

    return availability_days


def _dump_debug_html(
    page,
    base_dir: Path,
    settings: Any,
    item_id: int | str,
) -> None:
    if settings is None or not getattr(settings, "debug_dump_html", False):
        return
    rel = (getattr(settings, "debug_html_dir", None) or "debug_html").strip()
    out_dir = base_dir / rel
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{item_id}_{stamp}.html"
    try:
        path.write_text(page.content(), encoding="utf-8")
        print(f"Debug HTML → {path}")
    except Exception as exc:
        print(f"Debug HTML не сохранён: {exc}")


def _sheet_write_cutoff(
    day_at_start: date,
    availability_days: dict[date, str],
    booking_prices: dict[str, str],
    listing_url: str,
    day_session: Any,
) -> tuple[date, dict[date, str], dict[str, str]]:
    from google_sheets.calendar import filter_availability_day_map
    from google_sheets.parse_day import ParseDaySession, filter_booking_prices, write_cutoff_for_listing

    write_cutoff = write_cutoff_for_listing(day_at_start)
    availability_days = filter_availability_day_map(availability_days, write_cutoff)
    booking_prices = filter_booking_prices(booking_prices, write_cutoff)
    if day_session is not None and hasattr(day_session, "note_url_completed"):
        day_session.note_url_completed(listing_url, write_cutoff)
    return write_cutoff, availability_days, booking_prices


def _maybe_append_google_sheet(
    record: dict,
    base_dir: Path,
    item_id: int,
    booking_prices: dict[str, str],
    listing_url: str,
    *,
    sh: Any = None,
    settings: Any = None,
    removed: bool = False,
    queue_next_row: int | None = None,
    log_sheet_row: int | None = None,
    availability_days: dict[date, str] | None = None,
    sheet_today: date | None = None,
    sheet_worker: Any = None,
) -> bool:
    if os.environ.get("AVITO_GOOGLE_SHEET", "").lower() not in ("1", "true", "yes", "on"):
        return True
    if sh is None or settings is None:
        return True
    if sheet_worker is not None:
        from google_sheets.batch_sync import PendingListingSync

        sheet_worker.submit(
            PendingListingSync(
                record=record,
                columns=EXPORT_COLUMNS,
                booking_prices=booking_prices,
                listing_url=listing_url,
                removed=removed,
                queue_next_row=queue_next_row,
                log_sheet_row=log_sheet_row,
                availability_days=availability_days if not removed else None,
                sheet_today=sheet_today,
            )
        )
        return True
    try:
        from google_sheets import sync_after_listing
    except ImportError:
        return False
    try:
        ok, _wrote = sync_after_listing(
            sh,
            settings,
            record,
            EXPORT_COLUMNS,
            booking_prices,
            listing_url,
            removed=removed,
            queue_next_index=queue_next_row,
            log_sheet_row=log_sheet_row,
            availability_days=availability_days if not removed else None,
            today=sheet_today,
        )
        return ok
    except Exception:
        return False


def _listing_log_write(sheet_worker: Any, ok: bool | None = None) -> None:
    if sheet_worker is not None:
        pending = sheet_worker.pending
        if pending:
            _listing_log_status(f"запись: фон ({pending})")
        else:
            _listing_log_status("запись: фон")
    elif ok is False:
        _listing_log_status("запись: ошибка")
    else:
        _listing_log_status("запись: ок")


def _process_one_url(
    page,
    base_dir: Path,
    url: str,
    sheet_row: int,
    last_row: int,
    *,
    sh: Any = None,
    settings: Any = None,
    queue_next_row: int | None = None,
    day_session: Any = None,
    sheet_worker: Any = None,
    listing_num: int = 1,
) -> dict | None:
    try:
        from google_sheets.calendar import today_moscow
        from google_sheets.parse_day import ParseDaySession
    except ImportError:

        def today_moscow() -> date:  # type: ignore[misc]
            return datetime.now().date()

        ParseDaySession = None  # type: ignore[misc, assignment]

    item_id = _extract_id_from_url(url) or sheet_row
    day_at_start = today_moscow()

    if (
        day_session is not None
        and ParseDaySession is not None
        and isinstance(day_session, ParseDaySession)
        and day_session.should_skip_repeat(url)
    ):
        _listing_log_start(sheet_row, last_row)
        _listing_log_status("пропуск")
        return {"ссылка": url, "номер": item_id}

    _listing_log_start(sheet_row, last_row)
    listing_started = time.monotonic()
    timings = _ListingTimings(listing_started)
    page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    spa = _wait_initial_page_content(page)
    timings.add("загрузка", time.monotonic() - listing_started)
    timings._since = time.monotonic()

    if spa == "timeout" and _page_looks_blocked(page):
        spa = "blocked"

    if spa == "blocked":
        page.wait_for_timeout(1000)
        if _listing_spa_visible(page):
            spa = "ready"
        elif _page_looks_blocked(page):
            _listing_log_status("капча")
            _listing_log_timings(timings)
            return _captcha_hit(url)
        else:
            spa = "timeout"

    if spa != "ready" and _page_looks_blocked(page):
        _listing_log_status("капча")
        _listing_log_timings(timings)
        return _captcha_hit(url)

    title_early = _extract_title(page)
    if spa == "removed" or _listing_not_on_site(page, url, title_early):
        return _finish_removed_listing(
            page, base_dir, url, item_id, sheet_row, last_row,
            sh=sh, settings=settings, queue_next_row=queue_next_row,
            day_at_start=day_at_start, day_session=day_session,
            sheet_worker=sheet_worker, timings=timings,
        )

    t_phase = time.monotonic()
    has_cal, has_prices = _wait_booking_widgets(page)
    widget_s = time.monotonic() - t_phase
    if has_cal and has_prices:
        print(f"  загрузка виджетов: календарь + цены ({widget_s:.1f}с)")
    elif has_cal:
        print(f"  загрузка виджетов: календарь ({widget_s:.1f}с)")
    elif has_prices:
        print(f"  загрузка виджетов: цены ({widget_s:.1f}с)")
    else:
        print(f"  загрузка виджетов: нет брони/цен ({widget_s:.1f}с)")
    timings.add("виджеты", widget_s)

    calendar_in_url = "calendar=true" in url.lower()
    t_phase = time.monotonic()
    if has_cal or calendar_in_url:
        availability_days = _parse_listing_calendar(
            page, url, sheet_row, item_id, day_at_start, base_dir=base_dir, settings=settings
        )
    else:
        print("  сдаваемость: пропуск (нет календаря)")
        availability_days = {}
    timings.add("календарь", time.monotonic() - t_phase)

    t_phase = time.monotonic()
    if has_prices:
        booking_prices = _collect_nearest_date_prices(page)
        n_prices = len(booking_prices)
        if n_prices:
            print(f"  цены: {n_prices} слотов")
        else:
            print("  цены: пусто")
    else:
        print("  цены: пропуск (нет блока)")
        booking_prices = {}
    timings.add("цены", time.monotonic() - t_phase)

    if _listing_not_on_site(page, url):
        return _finish_removed_listing(
            page, base_dir, url, item_id, sheet_row, last_row,
            sh=sh, settings=settings, queue_next_row=queue_next_row,
            day_at_start=day_at_start, day_session=day_session,
            sheet_worker=sheet_worker, timings=timings,
        )

    t_phase = time.monotonic()
    _dump_debug_html(page, base_dir, settings, item_id)
    timings.add("debug", time.monotonic() - t_phase)

    run_detail = settings.run_detail if settings else True
    if not run_detail:
        t_phase = time.monotonic()
        write_cutoff, availability_days, booking_prices = _sheet_write_cutoff(
            day_at_start, availability_days, booking_prices, url, day_session,
        )
        booking_cell = (
            json.dumps(booking_prices, ensure_ascii=False)
            if booking_prices
            else NOT_FOUND_VALUE
        )
        record: dict[str, Any] = {
            "номер": item_id,
            "ссылка": url,
            "цены по датам": booking_cell,
        }
        for col in EXPORT_COLUMNS:
            record.setdefault(col, NOT_FOUND_VALUE)
        ok = _maybe_append_google_sheet(
            record, base_dir, item_id, booking_prices, url,
            sh=sh, settings=settings, removed=False, queue_next_row=queue_next_row,
            log_sheet_row=sheet_row,
            availability_days=availability_days, sheet_today=write_cutoff,
            sheet_worker=sheet_worker,
        )
        _listing_log_write(sheet_worker, ok)
        timings.add("запись", time.monotonic() - t_phase)
        _listing_log_timings(timings)
        return record

    detail_started = time.monotonic()
    title = _extract_title(page) or f"house_{item_id}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        phone_image_path = _save_phone_image_from_popup(
            page=page,
            save_dir=base_dir,
            item_id=item_id,
            title=title,
            timestamp=timestamp,
        )
    except Exception as exc:
        pass
        phone_image_path = None

    phone_number: str | None = None
    if phone_image_path:
        try:
            phone_number = _ocr_phone_number(phone_image_path)
        except Exception as exc:
            pass
            phone_number = None
    if phone_image_path and phone_number:
        try:
            Path(phone_image_path).unlink(missing_ok=True)
            phone_image_path = None
        except Exception as exc:
            pass

    price = _extract_price(page)
    address = _extract_address(page)
    description = _extract_description(page)
    details = _extract_details_map(page)
    seller, seller_profile = _extract_seller_name_and_profile(page)
    seller_rating, seller_reviews_n = _extract_seller_rating_and_reviews(page)
    gallery_urls = _extract_gallery_image_urls(page)
    # В таблице одна строка — без «лестницы» из URL в ячейке.
    photos_cell = " ".join(gallery_urls) if gallery_urls else NOT_FOUND_VALUE
    details_json = json.dumps(details, ensure_ascii=False) if details else ""
    booking_cell = json.dumps(booking_prices, ensure_ascii=False) if booking_prices else NOT_FOUND_VALUE

    record = {
        "номер": item_id,
        "название": _or_not_found(title),
        "цена": _or_not_found(price),
        "ссылки на фото": photos_cell,
        "адрес": _or_not_found(address),
        "автор": _or_not_found(seller),
        "рейтинг": _or_not_found(seller_rating),
        "кол-во отзывов": _or_not_found(seller_reviews_n),
        "контакт": CONTACT_PLACEHOLDER,
        "описание": _or_not_found(description),
        "телефон": phone_number if phone_number else (PHONE_NOT_RECOGNIZED if phone_image_path else NOT_FOUND_VALUE),
        "комнат": _or_not_found(_pick_detail_column(details, "комнат")),
        "площадь дома": _or_not_found(_pick_detail_column(details, "площадь дома")),
        "площадь участка": _or_not_found(_pick_detail_column(details, "площадь участка")),
        "этажей": _or_not_found(_pick_detail_column(details, "этажей")),
        "кровати": _or_not_found(_pick_detail_column(details, "кровати")),
        "год постройки": _or_not_found(_pick_detail_column(details, "год постройки")),
        "бытовая техника": _or_not_found(_pick_detail_column(details, "бытовая техника")),
        "что рядом": _or_not_found(_pick_detail_column(details, "что рядом")),
        "особенности": _or_not_found(_pick_detail_column(details, "особенности")),
        "залог": _or_not_found(_pick_detail_column(details, "залог")),
        "расстояние от МКАД": _or_not_found(_pick_detail_column(details, "расстояние от МКАД")),
        "заезд": _or_not_found(_pick_detail_column(details, "заезд")),
        "выезд": _or_not_found(_pick_detail_column(details, "выезд")),
        "кол-во гостей": _or_not_found(_pick_detail_column(details, "кол-во гостей")),
        "шуметь можно": _or_not_found(_pick_detail_column(details, "шуметь можно")),
        "можно с детьми": _or_not_found(_pick_detail_column(details, "можно с детьми")),
        "можно с питомцами": _or_not_found(_pick_detail_column(details, "можно с питомцами")),
        "можно курить": _or_not_found(_pick_detail_column(details, "можно курить")),
        "разрешены вечеринки": _or_not_found(_pick_detail_column(details, "разрешены вечеринки")),
        "характеристики json": details_json if details_json else NOT_FOUND_VALUE,
        "цены по датам": booking_cell,
        "ссылка": url,
    }
    if _normalize_space(seller_profile):
        record["ссылка создателя"] = _normalize_space(seller_profile)

    timings.add("детали", time.monotonic() - detail_started)

    t_phase = time.monotonic()
    write_cutoff, availability_days, booking_prices = _sheet_write_cutoff(
        day_at_start, availability_days, booking_prices, url, day_session,
    )

    ok = _maybe_append_google_sheet(
        record, base_dir, item_id, booking_prices, url,
        sh=sh, settings=settings, removed=False, queue_next_row=queue_next_row,
        log_sheet_row=sheet_row,
        availability_days=availability_days, sheet_today=write_cutoff,
        sheet_worker=sheet_worker,
    )
    _listing_log_write(sheet_worker, ok)
    timings.add("запись", time.monotonic() - t_phase)
    _listing_log_timings(timings)
    return record


def main() -> None:
    _setup_playwright_env()

    base_dir = project_root()

    sh, settings, urls, start_index, full_queue_len = _load_urls(base_dir)
    if not urls:
        print("Нечего парсить.")
        return

    from google_sheets.link_index import FIRST_DATA_ROW, index_to_row, last_data_row

    restart_every = settings.browser_restart_every if settings else BROWSER_RESTART_EVERY
    last_row = last_data_row(full_queue_len)
    first_row = index_to_row(start_index)
    print(
        f"К обработке {len(urls)} ссылок "
        f"(итерация {settings.parse_iteration if settings else 1}, "
        f"со строки {first_row} из {last_row})."
    )

    processed_ok = 0
    last_progress_row = first_row
    interrupted = False
    try:
        from google_sheets.calendar import today_moscow
        from google_sheets.parse_day import ParseDaySession

        day_session = ParseDaySession(iteration_day=today_moscow())
    except ImportError:
        day_session = None

    sheet_worker = None
    if sh is not None and settings is not None:
        try:
            from google_sheets.async_sync import AsyncSheetSyncWorker
            from google_sheets.settings import warm_settings_row_cache

            from google_sheets.iterations import get_logs_master_urls

            warm_settings_row_cache(sh, settings)

            interval = max(
                2.0,
                float(getattr(settings, "sheet_sync_min_interval_s", 3.5) or 3.5),
            )
            sheet_worker = AsyncSheetSyncWorker(
                sh,
                settings,
                min_interval_s=interval,
                log_urls=get_logs_master_urls() or list(urls),
            )
        except ImportError:
            sheet_worker = None

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context, page = _new_page(browser)
        parsed_in_session = 0
        captcha_streak = 0
        captcha_first_row: int | None = None
        stop_for_captcha = False
        try:
            for batch_pos, url in enumerate(urls, start=1):
                sheet_row = index_to_row(start_index + batch_pos - 1)
                next_row = sheet_row + 1
                if parsed_in_session >= restart_every:
                    if sheet_worker is not None and sheet_worker.pending > 0:
                        sheet_worker.drain()
                    try:
                        context.close()
                    except Exception:
                        pass
                    try:
                        browser.close()
                    except Exception:
                        pass
                    browser = _launch_browser(p)
                    context, page = _new_page(browser)
                    parsed_in_session = 0

                try:
                    record = _process_one_url(
                        page,
                        base_dir,
                        url,
                        sheet_row,
                        last_row,
                        sh=sh,
                        settings=settings,
                        queue_next_row=next_row,
                        day_session=day_session,
                        sheet_worker=sheet_worker,
                        listing_num=batch_pos,
                    )
                except Exception as exc:
                    _listing_log_start(sheet_row, last_row)
                    _listing_log_status(f"ошибка: {str(exc)[:80]}")
                    traceback.print_exc()
                    record = None

                if _is_captcha_record(record):
                    if captcha_streak == 0:
                        captcha_first_row = sheet_row
                    captcha_streak += 1
                    print(
                        f"Капча подряд {captcha_streak}/{CAPTCHA_STREAK_LIMIT} "
                        f"(строка {sheet_row})."
                    )
                    if captcha_streak >= CAPTCHA_STREAK_LIMIT:
                        stop_for_captcha = True
                        resume_row = max(
                            FIRST_DATA_ROW,
                            (captcha_first_row or sheet_row) - CAPTCHA_PROGRESS_BACK_ROWS,
                        )
                        print(
                            f"Капча: стоп после {CAPTCHA_STREAK_LIMIT} подряд. "
                            f"Следующий запуск — со строки {resume_row} "
                            f"(−{CAPTCHA_PROGRESS_BACK_ROWS} от строки {captcha_first_row})."
                        )
                        if sheet_worker is not None:
                            sheet_worker.drain()
                            sheet_worker._last_progress_row = resume_row
                        if sh is not None and settings is not None:
                            try:
                                from google_sheets.iterations import save_iteration_progress

                                settings = save_iteration_progress(
                                    sh,
                                    settings,
                                    resume_row,
                                    total_urls=full_queue_len,
                                    announce=True,
                                )
                                last_progress_row = resume_row
                            except Exception as exc:
                                print(f"Не удалось записать прогресс в настройках: {exc}")
                        break
                elif record is not None:
                    captcha_streak = 0
                    captcha_first_row = None

                if _is_captcha_record(record) or record is None:
                    continue

                parsed_in_session += 1
                processed_ok += 1
                last_progress_row = next_row
                between_started = time.monotonic()
                _pause_between_listings()
                between_s = time.monotonic() - between_started
                if between_s >= 0.2:
                    print(f"  время: между объявлениями {between_s:.1f}с")

            synced_next_row: int | None = None
            if sheet_worker is not None:
                synced_next_row = sheet_worker.finalize_sync()

            if stop_for_captcha:
                print("Парсер остановлен из‑за капчи. Решите блокировку и перезапустите.")
            else:
                print("Обработка завершена.")
                if sh is not None and settings is not None and full_queue_len > 0:
                    try:
                        from google_sheets import finish_parse_session

                        final_progress = synced_next_row or index_to_row(
                            start_index + processed_ok
                        )
                        finish_parse_session(
                            sh,
                            settings,
                            full_queue_len=full_queue_len,
                            final_progress=final_progress,
                        )
                    except ImportError:
                        pass
        except KeyboardInterrupt:
            interrupted = True
            print("\nОстановлено пользователем (Ctrl+C).")
            raise
        finally:
            if interrupted and sh is not None and settings is not None and not stop_for_captcha:
                try:
                    if sheet_worker is not None:
                        drained = sheet_worker.drain()
                        if drained is not None:
                            last_progress_row = drained
                    from google_sheets.iterations import save_iteration_progress

                    save_iteration_progress(
                        sh,
                        settings,
                        last_progress_row,
                        total_urls=full_queue_len,
                        announce=True,
                    )
                except Exception as exc:
                    print(f"Не удалось сохранить прогресс в настройках: {exc}")
            if sheet_worker is not None:
                try:
                    sheet_worker.shutdown()
                except Exception:
                    pass
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


if __name__ == "__main__":
    _console_utf8()
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception:
        print("Произошла ошибка:")
        traceback.print_exc()
        sys.exit(1)
