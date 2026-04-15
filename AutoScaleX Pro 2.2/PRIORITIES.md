# Приоритеты доработок (AutoScaleX Pro 2.2)

Рекомендуемый порядок работ по влиянию на стабильность, поддерживаемость и масштабирование.

---

## Высокий приоритет

### 1. Рефакторинг trading_bot.py (разбиение на модули) ✅

**Сделано:** Обработка исполнений — **`handlers.py`** (`handle_buy_filled`, `handle_sell_filled`). Ребаланс — **`rebalance.py`** (`check_rebalancing`, `check_rebalancing_after_all_buy_filled`, `_rebalancing_apply_after_market_buy`). Сетка — **`grid_protection.py`** (`cancel_last_n_buy_orders`, `create_buy_orders_at_bottom`). В `trading_bot.py` остались обёртки, вызывающие эти модули. Все тесты проходят.

---

### 2. Типизация публичного API ✅

**Сделано:** Добавлены type hints:
- **order_manager**: `Order.__init__(amount_usdt: Optional[Decimal])`, `to_dict() -> Dict[str, str]`, `from_dict(data: Dict) -> Order`.
- **persistence**: `save_state(user_id, state: Dict[str, Any]) -> None`, `load_state(user_id) -> Optional[Dict[str, Any]]`.
- **exchange**: `_request(...) -> Any`, `symbol_info(symbol) -> Dict[str, Any]`, `open_orders -> List[Dict[str, Any]]`, `cancel_order`, `place_limit`, `place_market`, `get_order -> Optional[Dict]`.
- **trading_bot**: `__init__(telegram_notifier: Any = None, symbol: Optional[str] = None) -> None`, `load_state/save_state -> None`, `handle_buy_filled` / `handle_sell_filled(order, price) -> None`.

---

### 3. Мониторинг и алерты (опционально) ✅

**Сделано:** В `main.py` запускается фоновая задача `api_metrics_loop`: раз в **API_METRICS_LOG_INTERVAL_SEC** (по умолчанию 120 с) вызывается `get_api_metrics_last_minute()`, метрики пишутся в лог. Если число ошибок за 60 с ≥ **API_METRICS_ERROR_ALERT_THRESHOLD** (по умолчанию 10) и задан `TG_ADMIN_ID`, админу отправляется сообщение в Telegram. Параметры задаются в `config.py` и через .env.

---

## Средний приоритет

### 4. Вынос констант сценариев в config ✅

**Сделано:** В `config.py` добавлен **REBALANCE_PREP_CANCEL_BUY_COUNT = 5** (число отменяемых самых низких BUY при подготовке к ребалансу).

---

### 5. Структурированное логирование (опционально) ✅

**Сделано:** Модуль **`structured_logging.py`**: контекст через **contextvars** (`user_id`, `symbol`, `order_id`), фильтр **StructuredContextFilter** подставляет их в каждую запись. В **main.py** формат логов дополнен полями `[user_id=...] [symbol=...] [order_id=...]`. Контекст выставляется в **main_loop** (trading_bot) и в **handlers** (handle_buy_filled / handle_sell_filled). Поиск по логам: `grep "user_id=12345"` или `grep "order_id=..."`. Описание — в CONTRIBUTING.md.

---

## Низкий приоритет

### 6. Дополнительные тесты ✅

**Сделано:**

- **Полный цикл:** в `TestFullCycleIntegration` — SELL fill → отмена 5 BUY → флаг сохранён → load_state восстанавливает флаг; отдельно — check_rebalancing при 0 SELL вызывает market buy, rebuild_buy_grid_from_price и create_sell_grid_only (на моках).
- **Модули grid_protection/rebalance:** в `tests/test_grid_protection_rebalance.py` — cancel_last_n_buy_orders (0 при нет BUY; отмена по самым низким ценам и удаление из orders), create_buy_orders_at_bottom при недостаточном балансе возвращает 0, check_rebalancing при STOPPED не вызывает place_market.

**Результат:** выше уверенность при рефакторингах.

---

### 7. Документация для разработчиков ✅

**Сделано:** Добавлен **CONTRIBUTING.md**: окружение, запуск тестов, таблица «где что править» (handlers, trading_bot, exchange, persistence, ссылки на ARCHITECTURE_AND_SCENARIOS, BOT_LOGIC, GRID_PROTECTION, PRIORITIES), константы в config. В README добавлена ссылка на CONTRIBUTING.md в дереве файлов.

---

## Сводная таблица

| № | Доработка | Приоритет | Трудозатраты | Влияние |
|---|-----------|-----------|--------------|---------|
| 1 | Рефакторинг trading_bot (модули) | Высокий | Средние | Поддерживаемость, тестируемость |
| 2 | Типизация публичного API | Высокий | Небольшие–средние | Надёжность, рефакторинг |
| 3 | Мониторинг/метрики API | Высокий (опц.) | Небольшие | Эксплуатация при 150 юзерах |
| 4 | Константы в config | Средний | Небольшие | Гибкость настроек |
| 5 | Структурированные логи | Средний (опц.) ✅ | Небольшие–средние | Разбор инцидентов |
| 6 | Доп. тесты (циклы, границы) ✅ | Низкий | Средние | Уверенность при изменениях |
| 7 | CONTRIBUTING + обновление доков | Низкий | Небольшие | Онбординг |

---

## Обновление на сервере и совместимость

Перечисленные доработки **не меняют формат state** (файлы `user_<id>.json`) и не трогают логику сохранения/загрузки. Поэтому при обновлении:

- **Пользователи не сбрасываются** — после рестарта бот загрузит сохранённое состояние (bot_state, ордера, profit_bank, cancelled_buy_for_rebalance_prep и т.д.) и продолжит работу.
- **Закрывать всех и запускать заново не нужно** — достаточно остановить процесс, выкатить новый код и запустить бота снова.

Если в будущем появятся изменения формата state (новые поля, переименования), нужно либо обеспечить обратную совместимость в `load_state()` (например, `state.get("new_key", default)`), либо один раз выполнить миграцию данных.

---

*Документ можно обновлять по мере закрытия пунктов или появления новых требований.*
