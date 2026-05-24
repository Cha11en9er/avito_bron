# avito_bron

Парсер забронированных домиков с Avito → Google Таблица.

## Структура

```
avito_bron/
├── google_sheets/     # Google Таблица (настройки, листы, запись)
├── parser/            # парсер Avito: python -m parser
├── kadastr/           # кадастр
└── docs/              # документация
```

## Быстрый старт

```bash
pip install -r requirements.txt
python -m google_sheets seed-settings   # заполнить лист «настройки», если пустой
python -m parser
```

Секреты в `.env`: `GOOGLE_CREDENTIALS_JSON`, `AVITO_GOOGLE_SHEET=1`.

## Тест: сдаваемость и цены, 10 ссылок, без листа «ссылки»

На листе **«настройки»** в таблице:

| ключ | значение |
|------|----------|
| `run_detail` | `0` |
| `run_calendar` | `1` |
| `sync_from_links_sheet` | `0` |
| `detail_range_from` | `0` |
| `detail_range_to` | `10` |

Запуск: `python -m parser`.  
URL берутся из столбца A **«сдаваемость по дням»**, первые 10 штук.

- Все ссылки: `from=0`, `to=0`
- Одна ссылка: `from=0`, `to=1`

Полное описание настроек: [docs/GOOGLE_SHEETS_AND_PARSERS.md](docs/GOOGLE_SHEETS_AND_PARSERS.md)

## Кадастр

```bash
python kadastr/run_parsers.py
python kadastr/merge_kadastr_to_sheet.py
```
