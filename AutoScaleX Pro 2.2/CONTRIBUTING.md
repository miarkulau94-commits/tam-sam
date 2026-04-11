# Участие в разработке (AutoScaleX Pro 2.2)

Кратко: как поднять окружение, запускать тесты и куда смотреть при правках сценариев.

---

## Окружение

1. **Клонировать репозиторий** и перейти в папку проекта:
   ```bash
   cd "AutoScaleX Pro 2.2"
   ```

2. **Установить зависимости:**
   ```bash
   pip install -r requirements.txt
   ```
   Или через скрипт: `install.bat` / `.\install.ps1` (Windows).

3. **Настроить .env** (скопировать из `env.example`):
   - `TG_TOKEN`, `ENCRYPTION_SECRET` — обязательны для запуска бота.
   - Остальное — по необходимости (API ключи, `TG_ADMIN_ID` и т.д.).

### Git hooks (корень репозитория git)

Репозиторий на уровень выше папки `AutoScaleX Pro 2.2`. Один раз из **корня git** (где лежит `.git` и папка `.githooks`):

```bash
git config core.hooksPath .githooks
```

На Linux/macOS при необходимости: `chmod +x .githooks/commit-msg .githooks/prepare-commit-msg`

Хуки убирают из текста коммита **`Сделано с: Курсор`** (и варианты без пробела у двоеточия), строки `Co-authored-by: … Cursor`, `Generated with Cursor`; при необходимости коммит блокируется, если запрещённый текст остался.

---

## Тесты

- **Все тесты:**
  ```bash
  python -m pytest tests/ -v
  ```
- **Быстрый прогон:**
  ```bash
  python -m pytest tests/ -q --tb=line
  ```
- **Конкретный модуль:** например, `python -m pytest tests/test_trading_integration.py tests/test_exchange.py -v`.

Тесты используют моки биржи и persistence, реальные API ключи не нужны. Тестов против живого API BingX нет; в продакшене конфиг задаётся через `.env`.

---

## Где что лежит

| Что править | Файл / документ |
|-------------|------------------|
| Обработка исполнения BUY/SELL | `handlers.py` |
| Ребаланс (все SELL закрыты → market buy, перестроение BUY/SELL) | `rebalance.py` |
| Защита сетки: отмена N BUY, добавление до 5 BUY внизу при 3 BUY | `grid_protection.py` |
| Оркестрация, state, вызов rebalance/grid_protection | `trading_bot.py` |
| Запросы к BingX, лимитер, ретраи, circuit breaker (rate limit не открывает breaker) | `exchange.py` |
| Синхронизация ордеров (`sync_orders_from_exchange`), лимит `get_order` | `trading_bot.py` |
| Сохранение/загрузка state | `persistence.py`, `trading_bot.load_state` / `save_state` |
| Сценарии и архитектура | **`ARCHITECTURE_AND_SCENARIOS.md`** |
| Логика бота, сетка, пирамидинг | **`BOT_LOGIC.md`** |
| Ребаланс по шагам | **`SELL_REBALANCING.md`** |
| Защита сетки и флаг после рестарта | **`GRID_PROTECTION.md`** |
| Свободный уровень сетки (якорь, `find_next_free_*`) | **`GRID_FREE_LEVELS.md`**, `tests/test_grid_free_levels.py` |
| Приоритеты доработок | **`PRIORITIES.md`** |

При изменении сценариев (ребаланс, 1 SELL → отмена 5 BUY, восстановление 5 BUY внизу) сверяйтесь с **ARCHITECTURE_AND_SCENARIOS.md** и при необходимости обновляйте его.

---

## Конфиг и константы

- Параметры стратегии, пути, лимиты: **`config.py`** и переменные окружения (`.env`).
- Константы сценариев (число отменяемых BUY, пороги защиты): в **`config.py`** (`REBALANCE_PREP_CANCEL_BUY_COUNT`, `PROTECTION_THRESHOLD_*`).
- Синхронизация ордеров с биржей: **`SYNC_GET_ORDER_MAX_PER_CALL`**, **`SYNC_BALANCE_MAX_GET_ORDER`** (см. `DEPLOY_VPS.md`, `ARCHITECTURE_AND_SCENARIOS.md` §1.2).
- Поиск свободного уровня после fill: **`GRID_FREE_MAX_STEPS`** (см. `GRID_FREE_LEVELS.md`, `config.py`).

---

## Структурированное логирование

В логах выводятся поля **user_id**, **symbol**, **order_id** (если заданы контекстом):

- Контекст задаётся через **contextvars** в `structured_logging`: `set_log_context(user_id=..., symbol=..., order_id=...)`.
- В **main_loop** (`trading_bot`) в начале каждой итерации вызывается `set_log_context(self.user_id, self.symbol)`.
- В **handlers** в начале `handle_buy_filled` / `handle_sell_filled` вызывается `set_log_context(bot.user_id, bot.symbol, order.order_id)`.
- Фильтр `StructuredContextFilter` в `main.py` подставляет эти значения в каждую запись; если контекст не задан, пишется `-`.

Поиск по логам: `grep "user_id=12345" bot_*.log` или `grep "order_id=abc" bot_*.log`.

---

*Документ можно дополнять по мере появления новых правил или скриптов.*
