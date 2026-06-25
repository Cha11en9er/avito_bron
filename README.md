# avito_bron

Парсер забронированных домиков с Avito → Google Таблица.

## Структура

```
avito_bron/
├── google_sheets/     # Google Таблица (настройки, листы, запись)
├── parser/            # python -m parser (datepicker 2 мес.) | запасной: python -m parser.all_info_carousel
├── kadastr/           # кадастр
└── docs/              # документация
```

---

## Установка с нуля (новая машина)

Клонирования репозитория **недостаточно**: секреты и ключ Google **не лежат в git**. Их нужно восстановить вручную.

### Что должно быть на диске после настройки

```
avito_bron/
├── .env                      ← создать из .env.example (в git нет)
├── service_account.json      ← JSON ключа сервисного аккаунта (в git нет)
├── .venv/                    ← виртуальное окружение Python
├── requirements.txt
└── …
```

| Файл | Где лежит | В git? |
|------|-----------|--------|
| `service_account.json` | **корень проекта** (`avito_bron/service_account.json`) | нет |
| `.env` | **корень проекта** | нет |
| `.env.example` | корень, образец | да |

Путь к JSON задаётся в `.env`:

```env
GOOGLE_CREDENTIALS_JSON=service_account.json
```

Если указать относительный путь — он считается **от корня проекта** (каталог, из которого запускается `python -m parser`).

Допустимы и другие имена, например `google-service-account.json` — главное, чтобы путь в `.env` совпадал с реальным файлом.

---

### 1. Что установить на систему

- **Python 3.11 или 3.12** — [python.org](https://www.python.org/downloads/). При установке на Windows включите «Add python.exe to PATH».
- **Google Chrome** — парсер открывает браузер через Playwright (`channel="chrome"`). Без Chrome запуск возможен через встроенный Chromium (см. шаг 4).
- **Git** — для клонирования репозитория.

Проверка:

```powershell
python --version
git --version
```

---

### 2. Клонировать репозиторий

```powershell
cd C:\repos\YouDo
git clone <URL_репозитория> avito_bron
cd avito_bron
```

---

### 3. Виртуальное окружение (venv)

**Windows (PowerShell):**

```powershell
cd C:\repos\YouDo\avito_bron
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
```

Если PowerShell блокирует активацию:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

**Linux / macOS:**

```bash
cd avito_bron
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

После активации в начале строки терминала появится `(.venv)`.

---

### 4. Playwright и браузер

Зависимости уже ставятся через `requirements.txt`. Дополнительно — движок браузера:

```powershell
python -m playwright install chromium
```

Рекомендуется также установить **Google Chrome** — парсер по умолчанию использует его. Если Chrome нет, Playwright попробует свой Chromium.

---

### 5. Файл `.env`

Скопируйте образец и отредактируйте:

```powershell
copy .env.example .env
notepad .env
```

Минимальное содержимое:

```env
# Путь к JSON сервисного аккаунта (от корня проекта)
GOOGLE_CREDENTIALS_JSON=service_account.json

# ID таблицы из адреса браузера:
# https://docs.google.com/spreadsheets/d/ВОТ_ЭТОТ_ID/edit
AVITO_GOOGLE_SHEET_ID=ваш_id_таблицы

# Включить очередь и запись в Google Таблицу
AVITO_GOOGLE_SHEET=1
```

Если `AVITO_GOOGLE_SHEET_ID` не указать — используется таблица по умолчанию из кода (см. `google_sheets/constants.py`).

---

### 6. JSON сервисного аккаунта (`service_account.json`)

Это ключ для доступа к Google Таблице **без входа пользователя**. Файл скачивается из Google Cloud и кладётся в **корень проекта**.

#### Вариант А — восстановить со старой машины

1. Найти на старом ПК файл (обычно `service_account.json` или `google-service-account.json` в папке проекта).
2. Скопировать на новую машину в `C:\repos\YouDo\avito_bron\service_account.json`.
3. Убедиться, что в `.env` указан тот же путь: `GOOGLE_CREDENTIALS_JSON=service_account.json`.

#### Вариант Б — создать ключ заново в Google Cloud

1. Откройте [Google Cloud Console](https://console.cloud.google.com/).
2. Выберите проект (или создайте новый).
3. **APIs & Services → Library** → найдите **Google Sheets API** → **Enable**.
4. **IAM & Admin → Service Accounts** → **Create Service Account** (имя любое, роль можно не назначать).
5. Откройте созданный аккаунт → вкладка **Keys** → **Add Key → Create new key → JSON**.
6. Скачанный файл переименуйте в `service_account.json` и положите в корень проекта:

   ```
   avito_bron/service_account.json
   ```

7. Откройте JSON в блокноте и найдите поле `"client_email"`, например:

   ```json
   "client_email": "avito-parser@my-project.iam.gserviceaccount.com"
   ```

8. Откройте вашу Google Таблицу → **Настройки доступа (Share)** → добавьте этот email с правом **Редактор**.

Без шага 8 парсер получит ошибку доступа к таблице, даже при правильном JSON.

> **Важно:** JSON содержит закрытый ключ. Не коммитьте его в git, не отправляйте в мессенджеры и не выкладывайте в открытый доступ. Файл уже в `.gitignore`.

---

### 7. Проверка подключения к таблице

Активируйте venv и выполните:

```powershell
cd C:\repos\YouDo\avito_bron
.\.venv\Scripts\Activate.ps1
python -m google_sheets seed-settings
```

Если команда прошла без ошибок — `.env`, `service_account.json` и доступ к таблице настроены верно.

Типичные ошибки:

| Сообщение | Причина |
|-----------|---------|
| `Файл ключа сервисного аккаунта не найден` | Нет `service_account.json` или неверный путь в `GOOGLE_CREDENTIALS_JSON` |
| `403` / `Permission denied` | Email из JSON не добавлен в «Поделиться» таблицы |
| `Spreadsheet not found` | Неверный `AVITO_GOOGLE_SHEET_ID` |

---

### 8. Запуск парсера

```powershell
.\.venv\Scripts\Activate.ps1
python -m parser
```

Парсер откроет окно Chrome, возьмёт очередь URL из таблицы (лист «ссылки» или «сдаваемость по дням» — см. настройки) и начнёт запись результатов.

Полное описание листов и настроек: [docs/GOOGLE_SHEETS_AND_PARSERS.md](docs/GOOGLE_SHEETS_AND_PARSERS.md)

---

## Краткий чеклист

- [ ] Python 3.11+ установлен
- [ ] Репозиторий склонирован
- [ ] `python -m venv .venv` и `pip install -r requirements.txt`
- [ ] `python -m playwright install chromium`
- [ ] Google Chrome установлен
- [ ] `.env` создан из `.env.example`, указаны `GOOGLE_CREDENTIALS_JSON` и `AVITO_GOOGLE_SHEET_ID`
- [ ] `service_account.json` лежит в **корне проекта**
- [ ] Email из JSON добавлен в доступ к таблице (Редактор)
- [ ] `python -m google_sheets seed-settings` — без ошибок
- [ ] `python -m parser` — парсер стартует

---

## Тест: сдаваемость и цены, 10 ссылок, без листа «ссылки»

На листе **«настройки»** в таблице:

| ключ | значение |
|------|----------|
| `run_detail` | `0` |
| `run_calendar` | `1` |
| `sync_from_links_sheet` | `0` |
| `detail_range_from` | `1` |
| `detail_range_to` | `10` |

Запуск: `python -m parser`.  
URL берутся из столбца A **«сдаваемость по дням»**, первые 10 штук.

- Все ссылки: `from=0`, `to=0`
- Одна ссылка: `from=0`, `to=1`

---

## Кадастр

```bash
python kadastr/run_parsers.py
python kadastr/merge_kadastr_to_sheet.py
```

Использует тот же `.env` и `service_account.json` из корня проекта.
