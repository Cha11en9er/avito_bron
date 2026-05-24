"""Утилиты Google Таблицы.

  python -m google_sheets seed-settings        — лист «настройки»
  python -m google_sheets seed-settings --force
  python -m google_sheets seed-logs            — лист «логи ежедневного парсинга»
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    parser = argparse.ArgumentParser(description="Сервис Google Таблицы")
    parser.add_argument(
        "command",
        nargs="?",
        default="seed-settings",
        choices=("seed-settings", "seed-logs"),
        help="seed-settings | seed-logs",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="для seed-settings: перезаписать настройки целиком",
    )
    args = parser.parse_args()

    if args.command == "seed-settings":
        from google_sheets.settings import seed_settings_workbook

        seed_settings_workbook(ROOT, force=args.force)
        return 0

    if args.command == "seed-logs":
        from google_sheets.client import open_spreadsheet
        from google_sheets.iterations import rebuild_logs_sheet
        from google_sheets.links import load_url_list
        from google_sheets.settings import load_settings

        sh = open_spreadsheet(ROOT)
        settings = load_settings(ROOT, sh)
        sh = open_spreadsheet(ROOT, sheet_id=settings.spreadsheet_id)
        settings = load_settings(ROOT, sh)
        urls = load_url_list(ROOT, settings, sh)
        rebuild_logs_sheet(ROOT, settings, sh, urls)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
