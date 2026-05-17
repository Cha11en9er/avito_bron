"""
Точка входа: тот же парсер, что avito-houses-parser_all_info.py, плюс запись в Google Таблицу
(лист «детальная информация»: строка по URL в столбце A, поля с B; календарные листы — см. avito_google_sheet.py).

Перед запуском:
  1) Установите зависимости: pip install -r requirements.txt
  2) Положите JSON сервисного аккаунта и укажите в .env рядом со скриптом:
       GOOGLE_CREDENTIALS_JSON=путь\\к\\service_account.json
     (или GOOGLE_APPLICATION_CREDENTIALS)
  3) Сервисный аккаунт — редактор таблицы (id по умолчанию уже зашит в avito_google_sheet.py,
     переопределение: AVITO_GOOGLE_SHEET_ID=…, лист: AVITO_GOOGLE_SHEET_TAB=детальная информация)
  4) Лист «ссылки»: в столбце A со 2-й строки — URL объявлений (A1 — заголовок). Имя листа:
     AVITO_GOOGLE_SHEET_LINKS (по умолчанию «ссылки»). В очередь попадают ссылки без «название»
     на «детальная информация» (в т.ч. после обрыва сети); URL переносятся на остальные листы.

Запуск: python avito-houses-parser_all_info_sheets.py
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

if __name__ == "__main__":
    os.environ.setdefault("AVITO_GOOGLE_SHEET", "1")
    base = Path(__file__).resolve().parent
    target = base / "avito-houses-parser_all_info.py"
    if not target.is_file():
        print(f"Не найден основной парсер: {target}", file=sys.stderr)
        sys.exit(1)
    runpy.run_path(str(target), run_name="__main__")
