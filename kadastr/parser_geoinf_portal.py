"""Поиск объектов на портале nspd.gov.ru (геоинф. портал)."""

from __future__ import annotations

import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

_KADASTR_DIR = Path(__file__).resolve().parent
if str(_KADASTR_DIR) not in sys.path:
    sys.path.insert(0, str(_KADASTR_DIR))

from playwright.sync_api import Page, sync_playwright

from parser_common import (
    append_result,
    load_addresses,
    load_checkpoint,
    pending_addresses,
    save_checkpoint,
)

BASE_URL = (
    "https://nspd.gov.ru/map?thematic=PKK&zoom=19.54853377873945"
    "&coordinate_x=4067826.997284765&coordinate_y=7434710.496979303"
    "&baseLayerId=235&theme_id=1&is_copy_url=true"
)
NAV_TIMEOUT_MS = 60000
RESULT_WAIT_MS = 25000
RESULT_POLL_MS = 300
PAUSE_AFTER_LOAD_MS = 4000
PAUSE_PANEL_MS = 800
PAUSE_AFTER_CLEAR_MS = 400
PAUSE_AFTER_FILL_MS = 600
PAUSE_AFTER_SEARCH_MS = 2500
PAUSE_AFTER_BUILDING_MS = 400
PAUSE_BETWEEN_ADDRESSES_MS = 1200
BUILDING_URL_WAIT_MS = 5000
EMPTY_MESSAGE = "нету объектов"
ADDRESSES_FILE = "adresa.txt"
OUTPUT_FILE = "results_geoinf_portal.json"

CHROME_ARGS = [
    "--start-maximized",
    "--disable-dev-shm-usage",
]

# Быстрый снимок панели результатов (без полного обхода DOM)
_JS_SEARCH_STATE = """
() => {
  function findHost(root) {
    const direct = root.querySelector('m-found-objects');
    if (direct) return direct;
    for (const el of root.querySelectorAll('*')) {
      if (!el.shadowRoot) continue;
      const found = findHost(el.shadowRoot);
      if (found) return found;
    }
    return null;
  }

  let objectCount = null;
  const host = findHost(document);
  let totalItems = 0;
  let buildingItems = 0;
  let accordionCount = 0;

  if (host?.shadowRoot) {
    for (const acc of host.shadowRoot.querySelectorAll('m-accordion')) {
      accordionCount += 1;
      const items = acc.shadowRoot?.querySelector('.accordion-items');
      if (!items) continue;
      for (const ch of items.children) {
        totalItems += 1;
        if ((ch.textContent || '').includes('Здание')) buildingItems += 1;
      }
    }
  }

  const content = document.querySelector('.content');
  if (content) {
    const m = (content.innerText || '').match(/(\\d+)\\s*объект/i);
    if (m) objectCount = parseInt(m[1], 10);
  }

  const panelOpen = !!host && host.style.display === 'block';

  return { objectCount, totalItems, buildingItems, accordionCount, panelOpen };
}
"""

_JS_GET_SEARCH_VALUE = """
() => {
  function walk(root) {
    if (!root) return '';
    const inp = root.querySelector('input.search');
    if (inp) return (inp.value || '').trim();
    for (const el of root.querySelectorAll('*')) {
      if (el.shadowRoot) {
        const v = walk(el.shadowRoot);
        if (v) return v;
      }
    }
    return '';
  }
  return walk(document);
}
"""

_JS_RESULTS_READY = """
() => {
  function findHost(root) {
    const direct = root.querySelector('m-found-objects');
    if (direct) return direct;
    for (const el of root.querySelectorAll('*')) {
      if (!el.shadowRoot) continue;
      const found = findHost(el.shadowRoot);
      if (found) return found;
    }
    return null;
  }
  let objectCount = null;
  const host = findHost(document);
  let totalItems = 0;
  let accordionCount = 0;
  if (host?.shadowRoot) {
    for (const acc of host.shadowRoot.querySelectorAll('m-accordion')) {
      accordionCount += 1;
      const items = acc.shadowRoot?.querySelector('.accordion-items');
      if (items) totalItems += items.children.length;
    }
  }
  const content = document.querySelector('.content');
  if (content) {
    const m = (content.innerText || '').match(/(\\d+)\\s*объект/i);
    if (m) objectCount = parseInt(m[1], 10);
  }
  const panelOpen = !!host && host.style.display === 'block';
  return objectCount !== null || totalItems > 0 || (panelOpen && accordionCount === 0);
}
"""


@dataclass
class BuildingsResult:
    url: str
    cadastral_number: str = ""


@dataclass
class AddressResult:
    address: str
    status: str
    search_url: str = ""
    buildings: BuildingsResult | None = None
    message: str = ""


def _setup_playwright_env() -> None:
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parent.parent
    bundled = base / "ms-playwright"
    if bundled.is_dir():
        import os

        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled)


def _console_utf8() -> None:
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def _launch_browser(playwright):
    try:
        return playwright.chromium.launch(channel="chrome", headless=False, args=CHROME_ARGS)
    except Exception:
        return playwright.chromium.launch(headless=False, args=CHROME_ARGS)


def _new_page(browser):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        ignore_https_errors=True,
    )
    page = context.new_page()
    page.set_default_navigation_timeout(NAV_TIMEOUT_MS)
    page.set_default_timeout(12000)
    return context, page


def _cadastral_from_url(url: str) -> str:
    match = re.search(r"selectedCard=([^&]+)", url)
    if not match:
        return ""
    for part in reversed(unquote(match.group(1)).split(",")):
        if re.match(r"\d+:\d+:\d+:\d+", part):
            return part
    return ""


def _cadastral_from_text(text: str) -> str:
    match = re.search(r"\d+:\d+:\d+:\d+", text)
    return match.group(0) if match else ""


def _search_input(page: Page):
    """input.search в shadow DOM — только Playwright-locator + fill(force)."""
    return page.locator("input.search").first


def _search_button(page: Page):
    """Кнопка поиска: button filled medium left (видна для force-click)."""
    return page.locator("button.button.filled.medium.left").first


def _open_search_panel(page: Page) -> None:
    page.locator(".input-label").first.click(force=True, timeout=10000)
    page.wait_for_timeout(PAUSE_PANEL_MS)


def _read_search_input(page: Page) -> str:
    val = (page.evaluate(_JS_GET_SEARCH_VALUE) or "").strip()
    if val:
        return val
    try:
        return _search_input(page).input_value(timeout=2000).strip()
    except Exception:
        return ""


def _clear_search_field(page: Page) -> None:
    inp = _search_input(page)
    inp.fill("", force=True)
    page.wait_for_timeout(150)
    page.keyboard.press("Control+A")
    page.keyboard.press("Backspace")
    page.wait_for_timeout(PAUSE_AFTER_CLEAR_MS)


def _reset_after_building(page: Page) -> None:
    """Вернуться к поиску без долгой перезагрузки всей карты."""
    if "selectedCard=" not in page.url:
        return
    for _ in range(2):
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    label = page.locator(".input-label").first
    try:
        label.wait_for(state="visible", timeout=5000)
        label.click(force=True, timeout=5000)
        page.wait_for_timeout(PAUSE_PANEL_MS)
        return
    except Exception:
        pass
    clean = page.url.split("&selectedCard=", 1)[0]
    page.goto(clean, wait_until="commit")
    label.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
    page.wait_for_timeout(PAUSE_AFTER_LOAD_MS)


def _fill_search_input(page: Page, address: str) -> str:
    expected = address.strip()
    _reset_after_building(page)
    _open_search_panel(page)
    _clear_search_field(page)

    inp = _search_input(page)
    inp.fill(expected, force=True)
    page.wait_for_timeout(PAUSE_AFTER_FILL_MS)

    actual = _read_search_input(page)
    if actual and actual != expected and expected not in actual:
        _clear_search_field(page)
        inp.fill(expected, force=True)
        page.wait_for_timeout(PAUSE_AFTER_FILL_MS)
        actual = _read_search_input(page)

    if actual and len(actual) > len(expected) and expected in actual:
        raise RuntimeError(f"Склейка адресов в поле: {actual!r}")

    return expected


def _short_error(message: str) -> str:
    first_line = message.split("\n", 1)[0].strip()
    if "Element is not visible" in first_line:
        return "поле поиска недоступно (скрыто на странице)"
    if len(first_line) > 180:
        return first_line[:180] + "…"
    return first_line


def _click_search_button(page: Page) -> None:
    """Рабочий способ: Playwright force-click по button.button.filled.medium.left."""
    btn = _search_button(page)
    btn.click(force=True, timeout=15000)
    page.wait_for_timeout(PAUSE_AFTER_SEARCH_MS)
    print("  поиск: кнопка нажата")


def _read_search_state(page: Page) -> dict:
    return page.evaluate(_JS_SEARCH_STATE)


def _wait_search_results(page: Page, prev: dict) -> dict:
    prev_total = prev.get("totalItems") or 0
    prev_sig = f"{prev_total}:{prev.get('objectCount')}:{prev.get('panelOpen')}"

    def _changed(state: dict) -> bool:
        sig = f"{state.get('totalItems') or 0}:{state.get('objectCount')}:{state.get('panelOpen')}"
        return sig != prev_sig

    deadline = page.evaluate("() => Date.now()") + RESULT_WAIT_MS
    last = _read_search_state(page)
    while page.evaluate("() => Date.now()") < deadline:
        last = _read_search_state(page)
        if _changed(last):
            if last.get("totalItems", 0) > 0 or last.get("objectCount") is not None:
                return last
            if last.get("panelOpen") and (last.get("accordionCount") or 0) == 0:
                return last
        page.wait_for_timeout(RESULT_POLL_MS)

    try:
        page.wait_for_function(_JS_RESULTS_READY, timeout=3000, polling=RESULT_POLL_MS)
    except Exception:
        pass
    return _read_search_state(page)


def _has_objects(state: dict) -> bool:
    count = state.get("objectCount")
    if count is not None:
        return count > 0
    return (state.get("totalItems") or 0) > 0


def _click_first_building(page: Page) -> tuple[bool, str, str]:
    btn = (
        page.locator("m-found-objects >> button.accordion-item.clickable")
        .filter(has_text=re.compile(r"Здание"))
        .first
    )
    if btn.count() == 0:
        return False, "", "нет зданий в результатах"
    text = btn.inner_text(timeout=5000)
    btn.click(force=True)
    try:
        page.wait_for_function(
            "() => location.href.includes('selectedCard=')",
            timeout=BUILDING_URL_WAIT_MS,
            polling=80,
        )
    except Exception:
        pass
    page.wait_for_timeout(PAUSE_AFTER_BUILDING_MS)
    return True, text, ""


def _process_address(page: Page, address: str) -> tuple[AddressResult, str]:
    queried = ""
    try:
        prev_state = _read_search_state(page)
        queried = _fill_search_input(page, address)
        _click_search_button(page)
        state = _wait_search_results(page, prev_state)

        if not _has_objects(state):
            return AddressResult(address=address, status="empty", message=EMPTY_MESSAGE), queried

        search_url = page.url
        ok, item_text, err = _click_first_building(page)
        if not ok:
            return (
                AddressResult(address=address, status="error", search_url=search_url, message=err),
                queried,
            )

        building_url = page.url
        cadastral = _cadastral_from_url(building_url) or _cadastral_from_text(item_text)
        return (
            AddressResult(
                address=address,
                status="ok",
                search_url=search_url,
                buildings=BuildingsResult(url=building_url, cadastral_number=cadastral),
            ),
            queried,
        )
    except Exception as exc:
        return AddressResult(address=address, status="error", message=_short_error(str(exc))), queried


def _result_to_dict(item: AddressResult) -> dict:
    entry: dict = {"адрес": item.address}
    if item.status == "empty":
        entry["результат"] = EMPTY_MESSAGE
        return entry
    if item.status == "error":
        entry["результат"] = _short_error(item.message) if item.message else "ошибка"
        return entry
    entry["результат"] = "найдено"
    entry["ссылка_поиска"] = item.search_url
    if item.buildings:
        entry["здания"] = {
            "ссылка": item.buildings.url,
            "кадастровый_номер": item.buildings.cadastral_number,
        }
    return entry


def _print_result(idx: int, total: int, item: AddressResult, queried: str = "") -> None:
    print(f"[{idx}/{total}] {item.address}")
    if queried:
        print(f"  запрос: {queried}")
    if item.status == "empty":
        print(f"  → {EMPTY_MESSAGE}")
    elif item.status == "ok" and item.buildings:
        kn = item.buildings.cadastral_number
        print(f"  → найдено{(' ' + kn) if kn else ''}")
    else:
        print(f"  → ошибка: {item.message}")


def main() -> None:
    _console_utf8()
    _setup_playwright_env()

    base_dir = Path(__file__).resolve().parent
    addresses_path = base_dir / ADDRESSES_FILE
    output_path = base_dir / OUTPUT_FILE

    all_addresses, skipped_lines = load_addresses(addresses_path)
    checkpoint = load_checkpoint(output_path, "nspd.gov.ru")
    todo = pending_addresses(all_addresses, checkpoint)

    print(f"Всего адресов (без заглушек): {len(all_addresses)}")
    print(f"Пропущено заглушек в файле: {len(skipped_lines)}")
    print(f"Уже в JSON: {len(checkpoint['результаты'])}")
    print(f"Осталось обработать: {len(todo)}")

    if not todo:
        print("Нечего обрабатывать — всё уже в results_geoinf_portal.json")
        return

    save_checkpoint(output_path, checkpoint, skipped_placeholders=len(skipped_lines))

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context, page = _new_page(browser)
        try:
            print(f"Открываю {BASE_URL}")
            page.goto(BASE_URL, wait_until="domcontentloaded")
            page.locator(".input-label").first.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)
            page.wait_for_timeout(PAUSE_AFTER_LOAD_MS)

            total = len(todo)
            for idx, address in enumerate(todo, start=1):
                item, queried = _process_address(page, address)
                append_result(
                    output_path,
                    checkpoint,
                    _result_to_dict(item),
                    skipped_placeholders=len(skipped_lines),
                )
                done_total = len(checkpoint["результаты"])
                print(f"[{idx}/{total}] (всего в JSON: {done_total})")
                _print_result(idx, total, item, queried)
                if idx < total:
                    page.wait_for_timeout(PAUSE_BETWEEN_ADDRESSES_MS)
        finally:
            context.close()
            browser.close()

    print(f"\nГотово: {output_path} ({len(checkpoint['результаты'])} записей)")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
