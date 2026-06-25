"""Подключение к Google Таблице: авторизация, retry, утилиты."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable

SCOPES = ("https://www.googleapis.com/auth/spreadsheets",)


def load_dotenv(base_dir: Path) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(base_dir / ".env")
    except ImportError:
        pass


def env(name: str, default: str = "") -> str:
    v = os.getenv(name, default)
    return (v or "").strip().strip('"').strip("'")


def credentials_path(base_dir: Path) -> Path:
    p = env("GOOGLE_CREDENTIALS_JSON", env("GOOGLE_APPLICATION_CREDENTIALS"))
    if not p:
        raise RuntimeError(
            "Нет пути к JSON ключу сервисного аккаунта. Укажите в .env:\n"
            "  GOOGLE_CREDENTIALS_JSON=service_account.json"
        )
    path = Path(p)
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        raise FileNotFoundError(f"Файл ключа сервисного аккаунта не найден: {path}")
    return path


def spreadsheet_id(settings_spreadsheet_id: str = "") -> str:
    from google_sheets.constants import DEFAULT_SPREADSHEET_ID

    return env("AVITO_GOOGLE_SHEET_ID", settings_spreadsheet_id or DEFAULT_SPREADSHEET_ID)


def open_spreadsheet(base_dir: Path, *, sheet_id: str = "") -> Any:
    import gspread

    load_dotenv(base_dir)
    cred_path = credentials_path(base_dir)
    gc = gspread.service_account(filename=str(cred_path), scopes=list(SCOPES))
    sid = sheet_id or spreadsheet_id()
    return gc.open_by_key(sid)


def api_retry(fn: Callable[[], Any], waits: tuple[float, ...] = (2.0, 5.0, 12.0, 30.0)) -> Any:
    from gspread.exceptions import APIError

    last: Exception | None = None
    for attempt in range(len(waits) + 1):
        try:
            return fn()
        except APIError as exc:
            last = exc
            code = getattr(getattr(exc, "response", None), "status_code", None)
            msg = str(exc).lower()
            quota = code == 429 or "rate limit" in msg or "read requests" in msg
            if quota and attempt < len(waits):
                time.sleep(waits[attempt])
                continue
            raise
    if last:
        raise last


def get_worksheet(sh: Any, title: str) -> Any | None:
    from gspread.exceptions import WorksheetNotFound

    try:
        return sh.worksheet(title)
    except WorksheetNotFound:
        return None


def ensure_worksheet(sh: Any, title: str, *, rows: int = 3000, cols: int = 60) -> Any:
    ws = get_worksheet(sh, title)
    if ws is not None:
        return ws

    def _create() -> Any:
        return sh.add_worksheet(title=title, rows=rows, cols=cols)

    ws = api_retry(_create)
    print(f"Создан лист «{title}».")
    return ws


def header_column_index(headers: list[str], name: str) -> int | None:
    target = (name or "").strip().lower()
    for i, h in enumerate(headers):
        if (h or "").strip().lower() == target:
            return i
    return None


def canon_url(u: str) -> str:
    s = (u or "").strip()
    if not s:
        return ""
    return s.split("?", 1)[0].strip().rstrip("/")


def find_row_by_url_col_a(ws: Any, url: str) -> int:
    canon = canon_url(url)
    col_a = ws.col_values(1)
    for i, cell in enumerate(col_a, start=1):
        if i == 1:
            continue
        if canon_url(cell) == canon:
            return i
    return len(col_a) + 1


def orphan_url_row_indices(ws: Any, allowed_canon: set[str]) -> list[int]:
    """Номера строк (1-based), где в A — URL, которого нет в allowed_canon."""
    col_a = ws.col_values(1)
    rows: list[int] = []
    for i, cell in enumerate(col_a, start=1):
        if i == 1:
            continue
        c = canon_url(cell)
        if c and c not in allowed_canon:
            rows.append(i)
    return rows


def delete_worksheet_rows(ws: Any, row_numbers_1based: list[int]) -> int:
    """Удалить строки листа (сдвиг вверх). Сначала нижние, чтобы индексы не сбивались."""
    rows = sorted({r for r in row_numbers_1based if r > 1}, reverse=True)
    if not rows:
        return 0

    def _do() -> None:
        sheet_id = ws.id
        requests = [
            {
                "deleteDimension": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "ROWS",
                        "startIndex": r - 1,
                        "endIndex": r,
                    }
                }
            }
            for r in rows
        ]
        ws.spreadsheet.batch_update({"requests": requests})

    api_retry(_do)
    return len(rows)


def is_google_sheet_enabled() -> bool:
    return env("AVITO_GOOGLE_SHEET").lower() in ("1", "true", "yes", "on")


def bootstrap_google_sheet_mode(base_dir: Path) -> str:
    load_dotenv(base_dir)
    flag = env("AVITO_GOOGLE_SHEET").lower()
    if flag in ("0", "false", "no", "off"):
        return "urls_file"
    if is_google_sheet_enabled():
        return "sheet"

    for key in ("GOOGLE_CREDENTIALS_JSON", "GOOGLE_APPLICATION_CREDENTIALS"):
        p = env(key)
        if not p:
            continue
        path = Path(p)
        if not path.is_absolute():
            path = base_dir / path
        if path.is_file():
            os.environ["AVITO_GOOGLE_SHEET"] = "1"
            return "sheet"

    default_cred = base_dir / "service_account.json"
    if default_cred.is_file():
        os.environ["AVITO_GOOGLE_SHEET"] = "1"
        if not env("GOOGLE_CREDENTIALS_JSON"):
            os.environ["GOOGLE_CREDENTIALS_JSON"] = "service_account.json"
        return "sheet"

    return "urls_file"
