"""Поиск кадастровых номеров по адресам на kadastor.com."""

from __future__ import annotations

import re
import sys
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urljoin

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
from playwright_stealth.stealth import Stealth

BASE_URL = "https://kadastor.com/"
NAV_TIMEOUT_MS = 90000
RESULT_WAIT_MS = 120000
ADDRESSES_FILE = "adresa.txt"
OUTPUT_FILE = "results_kadastor.json"

CHROME_ARGS = ["--start-maximized"]


@dataclass
class CadastralLink:
    cadastral_number: str
    area: float | None
    url: str


@dataclass
class AddressResult:
    address: str
    status: str  # ok | empty | error
    links: list[CadastralLink] = field(default_factory=list)
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
    page.set_default_timeout(30000)
    return context, page


def _parse_area(text: str) -> float | None:
    cleaned = text.replace("\xa0", " ").strip()
    match = re.search(r"[\d]+(?:[.,]\d+)?", cleaned)
    if not match:
        return None
    return float(match.group(0).replace(",", "."))


def _full_url(href: str) -> str:
    return urljoin(BASE_URL, href)


def _wait_search_form(page: Page) -> None:
    page.wait_for_selector(".search-form", state="visible", timeout=NAV_TIMEOUT_MS)


def _search_address(page: Page, address: str) -> None:
    form = page.locator(".search-form")
    row = form.locator(".search-form__row").first
    inp = row.locator(".search-form__input")
    inp.click()
    inp.fill("")
    inp.fill(address)
    page.wait_for_timeout(500)
    form.locator("button.btn.--btn-medium.--btn-black").click()


def _wait_results(page: Page) -> None:
    page.wait_for_selector("section.result, .result", state="visible", timeout=RESULT_WAIT_MS)
    page.wait_for_function(
        """() => {
            const loading = document.body?.innerText?.includes('Загрузка данных с сервера Росреестра');
            if (loading) return false;
            const section = document.querySelector('section.result, .result');
            return !!section;
        }""",
        timeout=RESULT_WAIT_MS,
    )
    page.wait_for_timeout(800)


def _extract_rows(page: Page) -> list[dict]:
    rows = page.locator(".result_table tbody tr[data-number]")
    count = rows.count()
    result: list[dict] = []
    for i in range(count):
        row = rows.nth(i)
        number = row.get_attribute("data-number") or ""
        area_text = row.locator("td.cadastral-number--area").inner_text(timeout=5000)
        href = row.locator('a.btn.--btn-medium.--btn-blue[href*="/kadnum/"]').first.get_attribute("href")
        if not href and number:
            href = f"/kadnum/{number}"
        result.append(
            {
                "number": number,
                "area": _parse_area(area_text),
                "url": _full_url(href) if href else "",
            }
        )
    return result


def _pick_links(rows: list[dict]) -> list[CadastralLink]:
    if not rows:
        return []

    valid = [r for r in rows if r.get("url")]
    if not valid:
        return []

    first = valid[0]
    links: list[CadastralLink] = [
        CadastralLink(
            cadastral_number=first["number"],
            area=first.get("area"),
            url=first["url"],
        )
    ]

    if len(valid) == 1:
        return links

    with_area = [r for r in valid if r.get("area") is not None]
    if not with_area:
        return links

    max_row = max(with_area, key=lambda r: r["area"])
    if max_row["url"] == first["url"]:
        return links

    links.append(
        CadastralLink(
            cadastral_number=max_row["number"],
            area=max_row["area"],
            url=max_row["url"],
        )
    )
    return links


def _process_address(page: Page, address: str) -> AddressResult:
    try:
        _wait_search_form(page)
        _search_address(page, address)
        _wait_results(page)

        rows = _extract_rows(page)
        if not rows:
            return AddressResult(address=address, status="empty", message="0 объектов")

        links = _pick_links(rows)
        return AddressResult(address=address, status="ok", links=links)
    except Exception as exc:
        return AddressResult(address=address, status="error", message=str(exc))


def _link_to_dict(link: CadastralLink) -> dict:
    data = asdict(link)
    data["кадастровый_номер"] = data.pop("cadastral_number")
    data["площадь"] = data.pop("area")
    data["ссылка"] = data.pop("url")
    return data


def _result_to_dict(item: AddressResult) -> dict:
    entry: dict = {
        "адрес": item.address,
        "статус": item.status,
        "сообщение": item.message,
    }
    if item.status == "ok" and item.links:
        entry["первая_строка"] = _link_to_dict(item.links[0])
        if len(item.links) > 1:
            entry["макс_площадь"] = _link_to_dict(item.links[1])
    return entry


def _print_result(idx: int, total: int, item: AddressResult) -> None:
    print(f"\n[{idx}/{total}] {item.address}")
    if item.status == "ok":
        for link in item.links:
            area = f", площадь {link.area}" if link.area is not None else ""
            print(f"  → {link.cadastral_number}{area}")
            print(f"    {link.url}")
    else:
        print(f"  → {item.status}: {item.message}")


def main() -> None:
    _console_utf8()
    _setup_playwright_env()

    base_dir = Path(__file__).resolve().parent
    addresses_path = base_dir / ADDRESSES_FILE
    output_path = base_dir / OUTPUT_FILE

    all_addresses, skipped_lines = load_addresses(addresses_path)
    checkpoint = load_checkpoint(output_path, "kadastor.com")
    todo = pending_addresses(all_addresses, checkpoint)

    print(f"Всего адресов (без заглушек): {len(all_addresses)}")
    print(f"Пропущено заглушек в файле: {len(skipped_lines)}")
    print(f"Уже в JSON: {len(checkpoint['результаты'])}")
    print(f"Осталось обработать: {len(todo)}")

    if not todo:
        print("Нечего обрабатывать — всё уже в results_kadastor.json")
        return

    save_checkpoint(output_path, checkpoint, skipped_placeholders=len(skipped_lines))

    with sync_playwright() as p:
        browser = _launch_browser(p)
        context, page = _new_page(browser)
        try:
            print(f"Открываю {BASE_URL}")
            page.goto(BASE_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            _wait_search_form(page)

            total = len(todo)
            for idx, address in enumerate(todo, start=1):
                item = _process_address(page, address)
                append_result(
                    output_path,
                    checkpoint,
                    _result_to_dict(item),
                    skipped_placeholders=len(skipped_lines),
                )
                done_total = len(checkpoint["результаты"])
                print(f"[{idx}/{total}] (всего в JSON: {done_total})")
                _print_result(idx, total, item)
                if idx < total:
                    page.wait_for_timeout(1500)
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
